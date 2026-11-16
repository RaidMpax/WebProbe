"""登录 TestLab Evals：隔离每题状态并保存可复查轨迹与摘要。"""

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
from webprobe.login_dataset import LOGIN_DATASET, LoginCase, load_login_cases
from webprobe.login_eval_scoring import (
    POLICY_NAMES, LoginPolicy, compare_login_summaries,
    score_login_case, summarize_login_results,
)
from webprobe.login_loop import LoginGoal, LoginRunResult, LoginStepRecord, run_login_loop
from webprobe.run_history import index_eval_summary


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def run_login_case(
    browser: Browser, case: LoginCase, output_dir: Path, policy: LoginPolicy = "rule",
) -> dict[str, object]:
    """运行单题；注入式故障和页面变化只影响当前浏览器上下文。"""
    if policy not in POLICY_NAMES:
        raise ValueError(f"未知登录策略: {policy}")
    login_url = (PROJECT_ROOT / "lab" / "login.html").as_uri()
    goal = LoginGoal(
        login_url=login_url, username=case.username, password=case.password,
        expected_authenticated=case.expected_authenticated,
        expected_status=case.expected_status, task=case.task,
    )
    screenshot = output_dir / f"{case.case_id}.png"
    trace = output_dir / f"{case.case_id}.zip"
    context = browser.new_context()
    context.set_default_timeout(min(8_000, case.max_seconds * 1_000))
    if case.bug_mode == "valid_login_rejected":
        context.add_init_script("window.__WEBPROBE_VALID_LOGIN_REJECTED__ = true")
    if case.page_variant == "renamed_button":
        context.add_init_script("window.__WEBPROBE_RENAME_LOGIN_BUTTON__ = true")
    context.tracing.start(screenshots=True, snapshots=True, sources=True)
    page = context.new_page()
    started = perf_counter()
    run: LoginRunResult | None = None
    steps: list[LoginStepRecord] = []
    error: str | None = None
    try:
        tools = BrowserTools(page, {login_url})
        run = run_login_loop(
            tools, goal, max_steps=case.max_steps,
            recheck_suspected_bug=policy == "rule", max_seconds=case.max_seconds,
        )
        steps = run.steps
        status = run.status
        error = next((step.error for step in reversed(steps) if step.error), None)
    except PlaywrightError as exc:
        status, error = "environment_error", str(exc)
    finally:
        elapsed = round(perf_counter() - started, 3)
        page.screenshot(path=str(screenshot))
        context.tracing.stop(path=str(trace))
        context.close()

    conclusion, assertion, success = score_login_case(case, run, elapsed, steps)
    actual = next(
        (step.observation for step in reversed(steps)
         if step.action == "submit" and step.observation is not None), None,
    )
    return {
        "case_id": case.case_id, "task": case.task, "policy": POLICY_NAMES[policy],
        "ground_truth": {"has_bug": case.has_bug, "bug_type": case.bug_type},
        "expected_behavior": {
            "authenticated": case.expected_authenticated, "status": case.expected_status,
        },
        "verification_condition": case.verification_condition,
        "success_condition": case.success_condition,
        "login_assertion": {
            "passed": assertion,
            "actual_state": actual.login_state if actual else None,
            "actual_status": actual.status if actual else None,
            "attempt_count": actual.login_attempt_count if actual else None,
        },
        "run_status": status, "agent_conclusion": conclusion,
        "reported_bug_type": run.bug_type if run else None,
        "reproduced_in_session": run.reproduced_in_session if run else False,
        "task_success": success,
        "time_budget_exceeded": status == "time_budget_exhausted" or elapsed > case.max_seconds,
        "elapsed_seconds": elapsed,
        "steps": [asdict(step) for step in steps], "error": error,
        "screenshot": str(screenshot.resolve()), "trace": str(trace.resolve()),
    }


def run_login_suite(
    output_root: Path | None = None, policy: LoginPolicy = "rule",
    case_id: str | None = None, baseline_summary: Path | None = None,
) -> Path:
    """运行登录固定集并保存输入数据副本、逐题证据和聚合指标。"""
    if policy not in POLICY_NAMES:
        raise ValueError(f"未知登录策略: {policy}")
    if baseline_summary is not None and (policy != "rule" or case_id is not None):
        raise ValueError("只有完整当前策略运行可以读取 Baseline")
    all_cases = load_login_cases()
    cases = [case for case in all_cases if case.case_id == case_id] if case_id else all_cases
    if not cases:
        raise ValueError(f"未知登录 Case ID: {case_id}")
    dataset_bytes = LOGIN_DATASET.read_bytes()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    output_dir = (output_root or PROJECT_ROOT / "artifacts" / "login_evals") / run_id
    output_dir.mkdir(parents=True)
    (output_dir / LOGIN_DATASET.name).write_bytes(dataset_bytes)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            results = [run_login_case(browser, case, output_dir, policy) for case in cases]
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
    source_files = (
        "webprobe/login_loop.py", "webprobe/login_evals.py",
        "webprobe/login_dataset.py", "webprobe/login_eval_scoring.py",
        "webprobe/browser_tools.py", "webprobe/time_budget.py",
        "webprobe/bad_cases.py", "lab/login.html",
    )
    summary = summarize_login_results(results)
    summary.update({
        "evaluation_scope": "single_case" if case_id else "full_suite",
        "selected_case_ids": [case.case_id for case in cases],
        "bad_case_count": len(bad_cases),
        "dataset_sha256": hashlib.sha256(dataset_bytes).hexdigest(),
        "policy_source_sha256": hashlib.sha256(
            b"".join((PROJECT_ROOT / name).read_bytes() for name in source_files)
        ).hexdigest(),
    })
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    if baseline_summary is not None:
        baseline = json.loads(baseline_summary.read_text(encoding="utf-8"))
        comparison = compare_login_summaries(baseline, summary)
        comparison["baseline_summary"] = str(baseline_summary.resolve())
        comparison["current_summary"] = str((output_dir / "summary.json").resolve())
        (output_dir / "comparison.json").write_text(
            json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    if output_root is None:
        index_eval_summary(output_dir / "summary.json")
    return output_dir


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="运行本地登录固定评测")
    parser.add_argument("--policy", choices=tuple(POLICY_NAMES), default="rule")
    parser.add_argument("--case-id")
    parser.add_argument("--baseline-summary", type=Path)
    args = parser.parse_args()
    print(run_login_suite(
        policy=args.policy, case_id=args.case_id,
        baseline_summary=args.baseline_summary,
    ))
