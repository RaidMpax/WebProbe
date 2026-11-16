"""独立运行反馈表单固定案例：隔离环境、留证、按规格评分。

运行：python -m webprobe.form_evals。规则策略可离线评测；模型策略需要
真实 API 配置。Case 的 Ground Truth 只由评测器读取，不传给受评循环。
"""

import argparse
import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from playwright.sync_api import Browser, Error as PlaywrightError, sync_playwright

from webprobe.bad_cases import classify_bad_case
from webprobe.browser_tools import BrowserTools
from webprobe.form_dataset import FORM_DATASET, FormCase, load_form_cases
from webprobe.form_loop import FormGoal, FormRunResult, FormStepRecord, run_form_loop
from webprobe.form_model_loop import run_form_model_loop
from webprobe.run_history import index_eval_summary
from webprobe.llm_decider import OpenAIDecider
from webprobe.form_eval_scoring import (
    POLICY_NAMES, FormPolicy,
    compare_form_summaries, score_form_case, summarize_form_results,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_form_case(
    browser: Browser, case: FormCase, output_dir: Path, policy: FormPolicy = "rule",
    decider: OpenAIDecider | None = None,
) -> dict[str, object]:
    """每个 Case 使用全新上下文；故障开关只配置在环境，不写入 URL。"""
    if policy not in POLICY_NAMES:
        raise ValueError(f"未知表单策略: {policy}")
    if policy == "model" and decider is None:
        raise ValueError("模型评测需要决策器")
    form_url = (PROJECT_ROOT / "lab" / "feedback.html").as_uri()
    goal = FormGoal(
        form_url=form_url, name=case.name, message=case.message,
        expected_submitted=case.expected_submitted, expected_status=case.expected_status,
        task=case.task,
    )
    screenshot = output_dir / f"{case.case_id}.png"
    trace = output_dir / f"{case.case_id}.zip"
    context = browser.new_context()
    context.set_default_timeout(min(8_000, case.max_seconds * 1_000))
    # 初始化脚本比页面自己的脚本先运行；正常和故障 Case 的 URL 完全相同。
    if case.bug_mode == "required_bypass":
        context.add_init_script("window.__WEBPROBE_REQUIRED_BYPASS__ = true")
    if case.page_variant == "renamed_submit":
        context.add_init_script("window.__WEBPROBE_RENAME_SUBMIT__ = true")
    context.tracing.start(screenshots=True, snapshots=True, sources=True)
    page = context.new_page()
    started = perf_counter()
    run: FormRunResult | None = None
    run_steps: list[FormStepRecord] = []
    model_calls: int | None = None
    decision_summaries: list[str] | None = None
    recoveries: list[str] | None = None
    model_error_type: str | None = None
    token_usage: dict[str, int] | None = None
    try:
        tools = BrowserTools(page, {form_url})
        if policy in ("rule_baseline", "rule"):
            run = run_form_loop(
                tools, goal, case.max_steps,
                recheck_suspected_bug=policy == "rule",
                max_seconds=case.max_seconds,
            )
            status = run.status
            run_steps = run.steps
            error = next((step.error for step in reversed(run_steps) if step.error), None)
        else:
            assert decider is not None
            model_run = run_form_model_loop(
                tools, decider, goal,
                max_steps=case.max_steps, max_model_calls=case.max_steps + 1,
                max_seconds=case.max_seconds,
            )
            status = model_run.status
            run_steps = model_run.steps
            model_calls = model_run.model_calls
            decision_summaries = model_run.decision_summaries
            recoveries = model_run.recoveries
            model_error_type = model_run.error_type
            token_usage = decider.token_usage
            error = model_run.error
            if status == "completed":
                run = FormRunResult("completed", run_steps)
            elif status == "suspected_bug":
                run = FormRunResult(
                    "suspected_bug", run_steps,
                    bug_type=model_run.bug_type,
                    reproduced_in_session=model_run.reproduced_in_session,
                )
        steps = [asdict(step) for step in run_steps]
    except PlaywrightError as exc:
        status, steps, error = "environment_error", [], str(exc)
    finally:
        elapsed_seconds = round(perf_counter() - started, 3)
        page.screenshot(path=str(screenshot))
        context.tracing.stop(path=str(trace))
        context.close()

    conclusion, assertion_passed, task_success = score_form_case(
        case, run, elapsed_seconds, evidence_steps=run_steps,
    )
    return {
        "case_id": case.case_id,
        "task": case.task,
        "policy": POLICY_NAMES[policy],
        "model": decider.model_name if policy == "model" and decider else None,
        "ground_truth": {"has_bug": case.has_bug, "bug_type": case.bug_type},
        "expected_behavior": {
            "submitted": case.expected_submitted, "status": case.expected_status,
        },
        "verification_condition": case.verification_condition,
        "success_condition": case.success_condition,
        "form_assertion": {"passed": assertion_passed},
        "run_status": status,
        "agent_conclusion": conclusion,
        "reported_bug_type": run.bug_type if run else None,
        "reproduced_in_session": run.reproduced_in_session if run else False,
        "task_success": task_success,
        "time_budget_exceeded": (
            status == "time_budget_exhausted" or elapsed_seconds > case.max_seconds
        ),
        "elapsed_seconds": elapsed_seconds,
        "steps": steps,
        "model_calls": model_calls,
        "decision_summaries": decision_summaries,
        "recoveries": recoveries,
        "token_usage": token_usage,
        "error": error,
        "model_error_type": model_error_type,
        "screenshot": str(screenshot.resolve()),
        "trace": str(trace.resolve()),
    }


def run_form_suite(
    output_root: Path | None = None, policy: FormPolicy = "rule",
    baseline_summary: Path | None = None,
    case_id: str | None = None,
) -> Path:
    """运行固定表单案例；指定 case_id 时只做一次单题试运行。"""
    if policy not in POLICY_NAMES:
        raise ValueError(f"未知表单策略: {policy}")
    if baseline_summary is not None and policy != "rule":
        raise ValueError("只有当前策略运行可以读取 Baseline")
    if baseline_summary is not None and case_id is not None:
        raise ValueError("单题试运行不能生成整套数据集的 Baseline 对照")
    cases = load_form_cases()
    if case_id is not None:
        cases = [case for case in cases if case.case_id == case_id]
        if not cases:
            raise ValueError(f"未知表单 Case ID: {case_id}")
    dataset_bytes = FORM_DATASET.read_bytes()
    # 没有配置时先报错，不留下伪装成模型实验的空运行目录。
    decider = OpenAIDecider.from_env(task_kind="form") if policy == "model" else None
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    output_dir = (output_root or PROJECT_ROOT / "artifacts" / "form_evals") / run_id
    output_dir.mkdir(parents=True)
    (output_dir / FORM_DATASET.name).write_bytes(dataset_bytes)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            results: list[dict[str, object]] = []
            for case in cases:
                wait_seconds = (
                    decider.wait_for_request_capacity(case.max_steps + 1)
                    if decider is not None else 0.0
                )
                result = run_form_case(browser, case, output_dir, policy, decider)
                result["rate_limit_wait_seconds"] = wait_seconds
                results.append(result)
        finally:
            browser.close()
    (output_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n",
        encoding="utf-8",
    )
    bad_cases = [record for r in results if (record := classify_bad_case(r)) is not None]
    (output_dir / "bad_cases.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in bad_cases)
        + ("\n" if bad_cases else ""), encoding="utf-8",
    )
    summary = summarize_form_results(results)
    summary["evaluation_scope"] = "single_case" if case_id is not None else "full_suite"
    summary["selected_case_ids"] = [case.case_id for case in cases]
    if case_id is not None:
        summary["metrics_note"] += " 单题试运行的比例只描述这一题，不代表整套数据集成绩。"
    summary["bad_case_count"] = len(bad_cases)
    summary["model_rate_limit_wait_seconds"] = round(
        sum(float(result.get("rate_limit_wait_seconds", 0.0)) for result in results), 3,
    )
    summary["dataset_sha256"] = hashlib.sha256(dataset_bytes).hexdigest()
    source_files = (
        "webprobe/form_loop.py", "webprobe/form_evals.py", "webprobe/browser_tools.py",
        "webprobe/form_dataset.py", "webprobe/form_eval_scoring.py",
        "webprobe/time_budget.py", "lab/feedback.html",
    )
    if policy == "model":
        source_files += ("webprobe/form_model_loop.py", "webprobe/llm_decider.py")
    summary["policy_source_sha256"] = hashlib.sha256(
        b"".join((PROJECT_ROOT / name).read_bytes() for name in source_files)
    ).hexdigest()
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    if baseline_summary is not None:
        baseline = json.loads(baseline_summary.read_text(encoding="utf-8"))
        comparison = compare_form_summaries(baseline, summary)
        comparison["baseline_summary"] = str(baseline_summary.resolve())
        comparison["current_summary"] = str((output_dir / "summary.json").resolve())
        (output_dir / "comparison.json").write_text(
            json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    if output_root is None:
        index_eval_summary(output_dir / "summary.json")
    return output_dir


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="运行本地反馈表单固定评测")
    parser.add_argument("--policy", choices=tuple(POLICY_NAMES), default="rule")
    parser.add_argument("--baseline-summary", type=Path)
    parser.add_argument("--case-id", help="只运行指定 Case，先检查模型接线与证据")
    args = parser.parse_args()
    print(run_form_suite(
        policy=args.policy, baseline_summary=args.baseline_summary, case_id=args.case_id,
    ))
