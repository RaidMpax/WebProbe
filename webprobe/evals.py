"""评测运行器：逐个启动本地 Case，并保存原始证据。

运行方式：python -m webprobe.evals
默认评估规则循环；--policy model 需要真实 API 配置。
Case 定义见 eval_dataset.py；评分公式见 eval_scoring.py。
"""

import argparse
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Literal
from uuid import uuid4

from playwright.sync_api import Browser, Error as PlaywrightError, sync_playwright

from webprobe.bad_cases import classify_bad_case
from webprobe.browser_tools import BrowserTools
from webprobe.demo_loop import DemoGoal, RunResult, run_demo
from webprobe.eval_dataset import DATASET, PROJECT_ROOT, EvalCase, load_cases
from webprobe.eval_scoring import MODEL_POLICY_NAME, score_case, summarize_results
from webprobe.llm_decider import OpenAIDecider
from webprobe.model_loop import run_model_loop
from webprobe.run_history import index_eval_summary


POLICY_NAME = "rule_demo_v5_wait_3500_time_budget"
BASELINE_POLICY_NAME = "rule_demo_baseline_v4_wait_2500_time_budget"
EvalPolicy = Literal["rule_baseline", "rule", "model"]
SEARCH_WAIT_MS = {"rule_baseline": 2_500, "rule": 3_500, "model": 2_500}


def run_case(
    browser: Browser, case: EvalCase, output_dir: Path,
    policy: EvalPolicy = "rule", decider: OpenAIDecider | None = None,
) -> dict[str, object]:
    """在全新的浏览器上下文运行一个 Case，并保存截图、Playwright Trace。"""
    if policy not in SEARCH_WAIT_MS:
        raise ValueError(f"未知评测模式: {policy}")
    if policy == "model" and decider is None:
        raise ValueError("模型评测需要决策器")
    policy_name = {
        "rule_baseline": BASELINE_POLICY_NAME,
        "rule": POLICY_NAME,
        "model": MODEL_POLICY_NAME,
    }[policy]
    search_url = (PROJECT_ROOT / "lab" / "search.html").as_uri()
    if case.bug_mode:
        search_url += f"?bug={case.bug_mode}"
    if case.load_delay_ms:
        search_url += ("&" if case.bug_mode else "?") + f"delay_ms={case.load_delay_ms}"
    detail_url = (PROJECT_ROOT / "lab" / "cup.html").as_uri()
    goal = DemoGoal(
        search_url=search_url,
        detail_url=detail_url,
        keyword=case.keyword,
        product_name=case.expected_products[0],
        search_status=case.expected_status,
        stock_status=case.stock_status,
        search_wait_ms=SEARCH_WAIT_MS[policy],
    )
    screenshot = output_dir / f"{case.case_id}.png"
    trace = output_dir / f"{case.case_id}.zip"
    context = browser.new_context()
    # 普通点击/定位给足时间；搜索加载仍由策略的 2.5/3.5 秒独立控制。
    # 压力较大时，按钮已找到但尚未稳定也可能超过 3 秒。
    context.set_default_timeout(min(8_000, case.max_seconds * 1_000))
    context.tracing.start(screenshots=True, snapshots=True, sources=True)
    page = context.new_page()
    started = perf_counter()
    actual_products: tuple[str, ...] | None = None
    run: RunResult | None = None
    model_calls: int | None = None
    decision_summaries: list[str] | None = None
    token_usage: dict[str, int] | None = None
    recoveries: list[str] | None = None
    model_error_type: str | None = None
    try:
        tools = BrowserTools(page, {search_url, detail_url})
        if policy in ("rule_baseline", "rule"):
            run = run_demo(
                tools, goal, max_steps=case.max_steps, max_seconds=case.max_seconds,
            )
            status = run.status
            run_steps = run.steps
            error = next((step.error for step in reversed(run_steps) if step.error), None)
        else:
            # 模型结论仍交给独立评分器；疑似 Bug 只有带复现证据才会映射到 RunResult。
            assert decider is not None
            model_run = run_model_loop(
                tools, decider, goal,
                max_steps=case.max_steps, max_model_calls=case.max_steps + 1,
                max_seconds=case.max_seconds,
            )
            status = model_run.status
            run_steps = model_run.steps
            model_calls = model_run.model_calls
            decision_summaries = model_run.decision_summaries
            recoveries = model_run.recoveries
            model_error_type = model_run.model_error_type
            token_usage = decider.token_usage
            error = model_run.error
            if status == "completed":
                run = RunResult("completed", run_steps)
            elif status == "suspected_bug":
                run = RunResult(
                    "suspected_bug", run_steps,
                    bug_type=model_run.bug_type,
                    reproduced_in_session=model_run.reproduced_in_session,
                )
        steps = [asdict(step) for step in run_steps]
        # 独立评测器只从已完成的搜索观察取证；加载中的空列表不是结果。
        actual_products = next(
            (step.observation.visible_products for step in reversed(run_steps)
             if step.action in ("submit", "wait_search")
             and step.observation is not None
             and step.observation.status != "搜索中…"),
            None,
        )
    except PlaywrightError as exc:
        # 浏览器环境本身报错时，不能把它算作“无 Bug”。
        status = "environment_error"
        steps = []
        error = str(exc)
    finally:
        elapsed_seconds = round(perf_counter() - started, 3)
        page.screenshot(path=str(screenshot))
        context.tracing.stop(path=str(trace))
        context.close()

    # 结论来自受评循环；商品断言由独立评测器计算。
    search_matches = (
        actual_products == case.expected_products if actual_products is not None else None
    )
    agent_conclusion, task_success = score_case(
        case, run, elapsed_seconds, search_matches
    )
    over_time_budget = (
        status == "time_budget_exhausted" or elapsed_seconds > case.max_seconds
    )
    return {
        "case_id": case.case_id,
        "task": case.task,
        "policy": policy_name,
        "model": decider.model_name if policy == "model" and decider is not None else None,
        "search_wait_ms": goal.search_wait_ms,
        "ground_truth": {"has_bug": case.has_bug, "bug_type": case.bug_type},
        "expected_products": list(case.expected_products),
        "verification_condition": case.verification_condition,
        "success_condition": case.success_condition,
        "search_assertion": {
            "actual_products": list(actual_products) if actual_products is not None else None,
            "passed": search_matches,
        },
        "run_status": status,
        "agent_conclusion": agent_conclusion,
        "reported_bug_type": run.bug_type if run is not None else None,
        "reproduced_in_session": run.reproduced_in_session if run is not None else False,
        "task_success": task_success,
        "time_budget_exceeded": over_time_budget,
        "elapsed_seconds": elapsed_seconds,
        "steps": steps,
        "model_calls": model_calls,
        "token_usage": token_usage,
        "decision_summaries": decision_summaries,
        "recoveries": recoveries,
        "error": error,
        "model_error_type": model_error_type,
        "screenshot": str(screenshot.resolve()),
        "trace": str(trace.resolve()),
    }


def run_suite(
    baseline_summary: Path | None = None, policy: EvalPolicy = "rule",
    case_id: str | None = None, output_root: Path | None = None,
) -> Path:
    """运行固定搜索案例并保存证据；case_id 可用于单题试运行。"""
    if policy not in SEARCH_WAIT_MS:
        raise ValueError(f"未知评测模式: {policy}")
    if baseline_summary is not None and case_id is not None:
        raise ValueError("单题试运行不能生成整套数据集的 Baseline 对照")
    all_cases = load_cases()
    cases = all_cases
    if case_id is not None:
        cases = [case for case in all_cases if case.case_id == case_id]
        if not cases:
            raise ValueError(f"未知搜索 Case ID: {case_id}")
    dataset_bytes = DATASET.read_bytes()
    dataset_sha256 = hashlib.sha256(dataset_bytes).hexdigest()
    baseline = None
    if baseline_summary is not None:
        baseline = json.loads(baseline_summary.read_text(encoding="utf-8"))
        if baseline["dataset"] != DATASET.name or baseline["case_count"] != len(all_cases):
            raise ValueError("对照实验的数据集或 Case 数量不一致")
        if ("dataset_sha256" in baseline
                and baseline["dataset_sha256"] != dataset_sha256):
            raise ValueError("对照实验的数据集内容不一致")
    # 缺少密钥时先报错，不留下伪装成模型实验的空目录。
    decider = OpenAIDecider.from_env() if policy == "model" else None
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    output_dir = (output_root or PROJECT_ROOT / "artifacts" / "evals") / run_id
    output_dir.mkdir(parents=True)
    (output_dir / DATASET.name).write_bytes(dataset_bytes)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            results: list[dict[str, object]] = []
            for case in cases:
                wait_seconds = (
                    decider.wait_for_request_capacity(case.max_steps + 1)
                    if decider is not None else 0.0
                )
                result = run_case(browser, case, output_dir, policy, decider)
                result["rate_limit_wait_seconds"] = wait_seconds
                results.append(result)
        finally:
            browser.close()

    results_path = output_dir / "results.jsonl"
    results_path.write_text(
        "\n".join(json.dumps(result, ensure_ascii=False) for result in results) + "\n",
        encoding="utf-8",
    )
    bad_cases = []
    for result in results:
        record = classify_bad_case(result)
        if record is not None:
            bad_cases.append(record)
    (output_dir / "bad_cases.jsonl").write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in bad_cases)
        + ("\n" if bad_cases else ""),
        encoding="utf-8",
    )
    summary = summarize_results(results)
    summary["evaluation_scope"] = "single_case" if case_id is not None else "full_suite"
    summary["selected_case_ids"] = [case.case_id for case in cases]
    if case_id is not None:
        summary["metrics_note"] += " 单题试运行的比例只描述这一题，不代表整套数据集成绩。"
    summary["model_rate_limit_wait_seconds"] = round(
        sum(float(result.get("rate_limit_wait_seconds", 0.0)) for result in results), 3,
    )
    summary["bad_case_count"] = len(bad_cases)
    summary["bad_case_categories"] = {
        category: sum(record["category"] == category for record in bad_cases)
        for category in sorted({record["category"] for record in bad_cases})
    }
    summary["dataset_sha256"] = dataset_sha256
    source_files = [
        "webprobe/demo_loop.py", "webprobe/browser_tools.py", "webprobe/evals.py",
        "webprobe/eval_dataset.py", "webprobe/eval_scoring.py",
        "webprobe/bad_cases.py", "webprobe/time_budget.py", "lab/search.html", "lab/cup.html",
    ]
    if policy == "model":
        source_files.extend(("webprobe/model_loop.py", "webprobe/llm_decider.py"))
    summary["policy_source_sha256"] = hashlib.sha256(
        b"".join((PROJECT_ROOT / name).read_bytes() for name in source_files)
    ).hexdigest()
    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if baseline is not None and baseline_summary is not None:
        comparison = {
            "dataset": DATASET.name,
            "baseline_policy": baseline["policy"],
            "current_policy": summary["policy"],
            "baseline_summary": str(baseline_summary.resolve()),
            "current_summary": str(summary_path.resolve()),
            "search_wait_ms": {
                "before": baseline.get("search_wait_ms"),
                "after": summary["search_wait_ms"],
            },
            "dataset_hash_match": (
                baseline["dataset_sha256"] == dataset_sha256
                if "dataset_sha256" in baseline else None
            ),
            "policy_source_hash_match": (
                baseline["policy_source_sha256"] == summary["policy_source_sha256"]
                if "policy_source_sha256" in baseline else None
            ),
            "dataset_identity_note": (
                "旧版结果没有数据集哈希；只核对了文件名和 Case 数量。"
                if "dataset_sha256" not in baseline else "已核对数据集哈希。"
            ),
            "task_success_rate": {
                "before": baseline["task_success_rate"],
                "after": summary["task_success_rate"],
                "delta": summary["task_success_rate"] - baseline["task_success_rate"],
            },
            "agent_execution_failure_count": {
                "before": baseline["agent_execution_failure_count"],
                "after": summary["agent_execution_failure_count"],
            },
            "model_failure_count": {
                "before": baseline.get("model_failure_count"),
                "after": summary.get("model_failure_count"),
            },
            "bad_case_count": {
                "before": baseline.get("bad_case_count"),
                "after": summary["bad_case_count"],
            },
            "average_latency_seconds": {
                "before": baseline["average_latency_seconds"],
                "after": summary["average_latency_seconds"],
                "delta": round(
                    summary["average_latency_seconds"] - baseline["average_latency_seconds"], 3
                ),
            },
            "confusion": {
                "before": baseline["confusion"],
                "after": summary["confusion"],
            },
            "recovery_attempt_case_count": {
                "before": baseline.get("recovery_attempt_case_count", 0),
                "after": summary.get("recovery_attempt_case_count", 0),
            },
            "recovered_case_count": {
                "before": baseline.get("recovered_case_count", 0),
                "after": summary.get("recovered_case_count", 0),
            },
            "recovery_rate": {
                "before": baseline.get("recovery_rate"),
                "after": summary.get("recovery_rate"),
            },
            "bug_f1": {"before": baseline.get("bug_f1"), "after": summary["bug_f1"]},
        }
        (output_dir / "comparison.json").write_text(
            json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if output_root is None:
        index_eval_summary(summary_path)
    return output_dir


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="运行本地搜索测试集")
    parser.add_argument("--baseline-summary", type=Path)
    parser.add_argument("--policy", choices=tuple(SEARCH_WAIT_MS), default="rule")
    parser.add_argument("--case-id", help="只运行指定 Case，便于低成本检查模型和证据")
    args = parser.parse_args()
    print(run_suite(args.baseline_summary, args.policy, args.case_id))
