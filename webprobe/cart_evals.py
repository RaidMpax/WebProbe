"""运行购物车固定集，保存截图、Playwright Trace、逐题结果与摘要。"""

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
from webprobe.cart_dataset import CART_DATASET, CartCase, load_cart_cases
from webprobe.cart_eval_scoring import (
    POLICY_NAMES, CartPolicy, compare_cart_summaries, score_cart_case,
    summarize_cart_results,
)
from webprobe.cart_loop import CartGoal, CartRunResult, CartStepRecord, run_cart_loop
from webprobe.cart_model_loop import CartModelRunResult, run_cart_model_loop
from webprobe.llm_decider import OpenAIDecider
from webprobe.run_history import index_eval_summary


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_cart_case(
    browser: Browser, case: CartCase, output_dir: Path, policy: CartPolicy = "rule",
    decider: OpenAIDecider | None = None,
) -> dict[str, object]:
    """单个购物车 Case 使用新 Context；故障脚本只注入该 Context。"""
    if policy not in POLICY_NAMES:
        raise ValueError(f"未知购物车策略: {policy}")
    if policy == "model" and decider is None:
        raise ValueError("模型策略需要模型决策器")
    cart_url = (PROJECT_ROOT / "lab" / "cart.html").as_uri()
    goal = CartGoal(
        cart_url=cart_url, items=case.items,
        expected_count=case.expected_count, expected_total=case.expected_total,
        task=case.task,
    )
    screenshot = output_dir / f"{case.case_id}.png"
    trace = output_dir / f"{case.case_id}.zip"
    context = browser.new_context()
    context.set_default_timeout(min(8_000, case.max_seconds * 1_000))
    if case.bug_mode == "wrong_total":
        context.add_init_script("window.__WEBPROBE_CART_TOTAL_BUG__ = true")
    context.tracing.start(screenshots=True, snapshots=True, sources=True)
    page = context.new_page()
    started = perf_counter()
    run: CartRunResult | CartModelRunResult | None = None
    steps: list[CartStepRecord] = []
    error: str | None = None
    model_calls: int | None = None
    decision_summaries: list[str] | None = None
    recoveries: list[str] | None = None
    model_error_type: str | None = None
    token_usage: dict[str, int] | None = None
    try:
        tools = BrowserTools(page, {cart_url})
        if policy == "model":
            assert decider is not None
            model_run = run_cart_model_loop(
                tools, decider, goal, max_steps=case.max_steps,
                max_model_calls=case.max_steps + 1, max_seconds=case.max_seconds,
            )
            run = model_run
            model_calls = model_run.model_calls
            decision_summaries = model_run.decision_summaries
            recoveries = model_run.recoveries
            model_error_type = model_run.error_type
            token_usage = decider.token_usage
        else:
            run = run_cart_loop(
                tools, goal, max_steps=case.max_steps,
                recheck_suspected_bug=policy == "rule", max_seconds=case.max_seconds,
            )
        steps = run.steps
        error = next((step.error for step in reversed(steps) if step.error), None)
        if error is None:
            error = getattr(run, "error", None)
        status = run.status
    except PlaywrightError as exc:
        status, error = "environment_error", str(exc)
    finally:
        elapsed_seconds = round(perf_counter() - started, 3)
        page.screenshot(path=str(screenshot))
        context.tracing.stop(path=str(trace))
        context.close()

    conclusion, assertion, task_success = score_cart_case(
        case, run, elapsed_seconds, steps,
    )
    actual = steps[-1].observation if steps and steps[-1].observation else None
    result = {
        "case_id": case.case_id,
        "task": case.task,
        "policy": POLICY_NAMES[policy],
        "model": decider.model_name if policy == "model" and decider else None,
        "ground_truth": {"has_bug": case.has_bug, "bug_type": case.bug_type},
        "expected_behavior": {
            "count": case.expected_count, "total": case.expected_total,
            "items": [[name, quantity] for name, quantity in case.expected_items],
        },
        "verification_condition": case.verification_condition,
        "success_condition": case.success_condition,
        "cart_assertion": {
            "passed": assertion,
            "actual_count": actual.cart_count if actual else None,
            "actual_total": actual.cart_total if actual else None,
            "actual_items": list(actual.cart_items) if actual else None,
        },
        "run_status": status,
        "agent_conclusion": conclusion,
        "reported_bug_type": run.bug_type if run else None,
        "reproduced_in_session": run.reproduced_in_session if run else False,
        "task_success": task_success,
        "time_budget_exceeded": status == "time_budget_exhausted" or elapsed_seconds > case.max_seconds,
        "elapsed_seconds": elapsed_seconds,
        "steps": [asdict(step) for step in steps],
        "model_calls": model_calls,
        "decision_summaries": decision_summaries,
        "recoveries": recoveries,
        "token_usage": token_usage,
        "error": error,
        "model_error_type": model_error_type,
        "screenshot": str(screenshot.resolve()),
        "trace": str(trace.resolve()),
    }
    return result


def run_cart_suite(
    output_root: Path | None = None, policy: CartPolicy = "rule",
    case_id: str | None = None, baseline_summary: Path | None = None,
) -> Path:
    """运行固定购物车 Case 并保存可复查实验产物。"""
    if policy not in POLICY_NAMES:
        raise ValueError(f"未知购物车策略: {policy}")
    if baseline_summary is not None and (policy != "rule" or case_id is not None):
        raise ValueError("只有完整当前策略运行可以读取 Baseline")
    all_cases = load_cart_cases()
    cases = [case for case in all_cases if case.case_id == case_id] if case_id else all_cases
    if not cases:
        raise ValueError(f"未知购物车 Case ID: {case_id}")
    dataset_bytes = CART_DATASET.read_bytes()
    decider = OpenAIDecider.from_env(task_kind="cart") if policy == "model" else None
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    output_dir = (output_root or PROJECT_ROOT / "artifacts" / "cart_evals") / run_id
    output_dir.mkdir(parents=True)
    (output_dir / CART_DATASET.name).write_bytes(dataset_bytes)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            results = []
            for case in cases:
                wait_seconds = (
                    decider.wait_for_request_capacity(case.max_steps + 1)
                    if decider is not None else 0.0
                )
                result = run_cart_case(browser, case, output_dir, policy, decider)
                result["rate_limit_wait_seconds"] = wait_seconds
                results.append(result)
        finally:
            browser.close()
    (output_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in results) + "\n",
        encoding="utf-8",
    )
    bad_cases = [record for row in results if (record := classify_bad_case(row)) is not None]
    (output_dir / "bad_cases.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in bad_cases)
        + ("\n" if bad_cases else ""), encoding="utf-8",
    )
    summary = summarize_cart_results(results)
    source_files = [
        "webprobe/cart_loop.py", "webprobe/cart_evals.py",
        "webprobe/cart_dataset.py", "webprobe/cart_eval_scoring.py",
        "webprobe/browser_tools.py", "webprobe/time_budget.py",
        "webprobe/bad_cases.py", "lab/cart.html",
    ]
    if policy == "model":
        source_files.extend(("webprobe/cart_model_loop.py", "webprobe/llm_decider.py"))
    summary.update({
        "evaluation_scope": "single_case" if case_id else "full_suite",
        "selected_case_ids": [case.case_id for case in cases],
        "bad_case_count": len(bad_cases),
        "dataset_sha256": hashlib.sha256(dataset_bytes).hexdigest(),
        "policy_source_sha256": hashlib.sha256(b"".join(
            (PROJECT_ROOT / name).read_bytes() for name in source_files
        )).hexdigest(),
    })
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    if baseline_summary is not None:
        baseline = json.loads(baseline_summary.read_text(encoding="utf-8"))
        comparison = compare_cart_summaries(baseline, summary)
        comparison["baseline_summary"] = str(baseline_summary.resolve())
        comparison["current_summary"] = str((output_dir / "summary.json").resolve())
        (output_dir / "comparison.json").write_text(
            json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    if output_root is None:
        index_eval_summary(output_dir / "summary.json")
    return output_dir


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="运行本地购物车固定评测")
    parser.add_argument("--policy", choices=tuple(POLICY_NAMES), default="rule")
    parser.add_argument("--case-id", help="只运行一个购物车 Case")
    parser.add_argument("--baseline-summary", type=Path)
    args = parser.parse_args()
    print(run_cart_suite(
        policy=args.policy, case_id=args.case_id,
        baseline_summary=args.baseline_summary,
    ))
