"""表单 Evals：独立评分，以及故障标签不出现在受评 URL 中。"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import json

import pytest
from playwright.sync_api import sync_playwright

from webprobe.browser_tools import PageObservation
from webprobe.bad_cases import classify_bad_case
from webprobe.form_dataset import FORM_DATASET, load_form_cases
from webprobe.form_evals import run_form_case, run_form_suite
from webprobe.form_eval_scoring import (
    BASELINE_POLICY_NAME, MODEL_POLICY_NAME, POLICY_NAME,
    compare_form_summaries, score_form_case, summarize_form_results,
)
from webprobe.form_loop import FormRunResult, FormStepRecord
from webprobe.llm_decider import OpenAIDecider


CASES = load_form_cases()


def scripted_form_decider(
    actions: list[str], include_usage: bool = False,
) -> OpenAIDecider:
    """响应顺序由测试预写；浏览器操作仍会真实执行。"""
    client = Mock()
    client.responses.create.side_effect = [
        SimpleNamespace(
            output=[SimpleNamespace(
                type="function_call", name="choose_browser_action",
                arguments=json.dumps({"action": action, "summary": f"选择 {action}"}),
                call_id=f"eval-form-call-{number}",
            )],
            usage=SimpleNamespace(
                input_tokens=10 + number, output_tokens=2,
                total_tokens=12 + number,
            ) if include_usage else None,
        )
        for number, action in enumerate(actions)
    ]
    return OpenAIDecider(client, "offline-test-model", task_kind="form")


def test_execution_failure_stays_unclassified() -> None:
    case = CASES[2]
    run = FormRunResult(status="execution_failed", steps=[])
    conclusion, assertion, success = score_form_case(case, run, 0.1)
    assert (conclusion, assertion, success) == (None, None, False)


def test_bug_claim_without_second_submission_does_not_pass_eval() -> None:
    case = CASES[2]
    observation = PageObservation(
        url="file:///lab/feedback.html", title="反馈表单", heading="提交反馈",
        status="提交成功", search_value=None,
        form_name_value="", form_message_value="",
        form_state="submitted", form_submission_count="1",
    )
    run = FormRunResult(
        status="suspected_bug",
        steps=[FormStepRecord("submit", observation)],
        bug_type="required_field_bypass", reproduced_in_session=True,
    )
    conclusion, assertion, success = score_form_case(case, run, 0.1)
    assert (conclusion, assertion, success) == ("bug", False, False)


def test_unclassified_case_does_not_become_false_negative() -> None:
    results = [
        {"policy": POLICY_NAME, "ground_truth": {"has_bug": True},
         "agent_conclusion": None, "run_status": "execution_failed",
         "task_success": False, "reproduced_in_session": False,
         "steps": [], "elapsed_seconds": 0.1},
    ]
    summary = summarize_form_results(results)
    assert summary["unclassified_case_count"] == 1
    assert summary["confusion"] == {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    assert summary["bug_recall"] is None


def test_form_recovery_rate_uses_cases_with_explicit_recovery_records() -> None:
    base = {
        "policy": POLICY_NAME,
        "ground_truth": {"has_bug": False},
        "agent_conclusion": "no_bug",
        "run_status": "completed",
        "reproduced_in_session": False,
        "steps": [],
        "elapsed_seconds": 0.1,
        "token_usage": None,
    }
    summary = summarize_form_results([
        {**base, "task_success": True, "recoveries": ["重新定位提交按钮"]},
        {**base, "task_success": False, "recoveries": ["复查仍未成功"]},
        {**base, "task_success": True, "recoveries": None},
    ])

    assert summary["recovery_attempt_case_count"] == 2
    assert summary["recovered_case_count"] == 1
    assert summary["recovery_rate"] == 0.5


def test_model_failure_breakdown_counts_only_typed_model_failures() -> None:
    base = {
        "policy": MODEL_POLICY_NAME, "model": "offline-test-model",
        "ground_truth": {"has_bug": False}, "agent_conclusion": None,
        "task_success": False, "reproduced_in_session": False,
        "steps": [], "elapsed_seconds": 0.1,
    }
    results = [
        {**base, "run_status": "model_failed", "model_error_type": "ModelTimeout"},
        {**base, "run_status": "model_failed", "model_error_type": "ModelTimeout"},
        {**base, "run_status": "model_failed", "model_error_type": "ProviderLocationUnavailable"},
        {**base, "run_status": "time_budget_exhausted", "model_error_type": "ModelTimeout",
         "time_budget_exceeded": True},
    ]
    summary = summarize_form_results(results)
    assert summary["model_failure_count"] == 4
    assert summary["model_failure_breakdown"] == {
        "ModelTimeout": 3, "ProviderLocationUnavailable": 1,
    }
    assert summary["time_budget_exceeded_count"] == 1


def test_token_usage_summary_requires_every_case_to_have_api_usage() -> None:
    base = {
        "policy": MODEL_POLICY_NAME, "model": "offline-test-model",
        "ground_truth": {"has_bug": False}, "agent_conclusion": None,
        "run_status": "needs_review", "task_success": False,
        "reproduced_in_session": False, "steps": [], "elapsed_seconds": 0.1,
    }
    results = [
        {**base, "token_usage": {
            "input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
        }},
        {**base, "token_usage": {
            "input_tokens": 20, "output_tokens": 3, "total_tokens": 23,
        }},
    ]
    complete = summarize_form_results(results)
    assert complete["token_usage"] == {
        "input_tokens": 30, "output_tokens": 5, "total_tokens": 35,
    }
    assert complete["token_usage_covered_cases"] == 2

    results[1]["token_usage"] = None
    partial = summarize_form_results(results)
    assert partial["token_usage"] is None
    assert partial["token_usage_covered_cases"] == 1


def test_bug_case_uses_clean_url_and_real_browser_evidence(tmp_path: Path) -> None:
    case = CASES[2]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            result = run_form_case(browser, case, tmp_path)
        finally:
            browser.close()

    assert result["task_success"] is True
    assert result["form_assertion"]["passed"] is False
    assert result["run_status"] == "suspected_bug"
    assert [step["observation"]["form_submission_count"] for step in result["steps"][1:]] == ["1", "2"]
    assert all("?bug=" not in step["observation"]["url"] for step in result["steps"])
    assert Path(result["screenshot"]).is_file()
    assert Path(result["trace"]).is_file()


def test_baseline_leaves_first_suspicion_unclassified(tmp_path: Path) -> None:
    case = CASES[2]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            result = run_form_case(browser, case, tmp_path, policy="rule_baseline")
        finally:
            browser.close()

    assert result["policy"] == BASELINE_POLICY_NAME
    assert result["run_status"] == "needs_review"
    assert result["agent_conclusion"] is None
    assert result["task_success"] is False
    assert [step["action"] for step in result["steps"]] == ["open", "submit"]
    assert result["form_assertion"]["passed"] is False


def test_comparison_rejects_different_dataset_hash() -> None:
    baseline = {
        "policy": BASELINE_POLICY_NAME, "dataset": FORM_DATASET.name,
        "dataset_sha256": "old", "policy_source_sha256": "same", "case_count": len(CASES),
    }
    current = {
        "policy": POLICY_NAME, "dataset": FORM_DATASET.name,
        "dataset_sha256": "new", "policy_source_sha256": "same", "case_count": len(CASES),
    }
    with pytest.raises(ValueError, match="dataset_sha256"):
        compare_form_summaries(baseline, current)


def test_model_policy_scores_normal_renamed_case_with_browser_evidence(tmp_path: Path) -> None:
    case = next(case for case in CASES if case.page_variant == "renamed_submit")
    decider = scripted_form_decider(
        ["open", "fill_name", "fill_message", "submit", "stop"],
        include_usage=True,
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            result = run_form_case(browser, case, tmp_path, policy="model", decider=decider)
        finally:
            browser.close()

    assert result["policy"] == MODEL_POLICY_NAME
    assert result["model"] == "offline-test-model"
    assert result["run_status"] == "completed"
    assert result["agent_conclusion"] == "no_bug"
    assert result["form_assertion"]["passed"] is True
    assert result["task_success"] is True
    assert result["model_calls"] == 5
    assert result["token_usage"] == {
        "input_tokens": 60, "output_tokens": 10, "total_tokens": 70,
    }
    assert summarize_form_results([result])["token_usage"] == result["token_usage"]
    assert len(result["decision_summaries"]) == 5
    assert result["steps"][0]["observation"]["form_submit_label"] == "发送反馈"
    assert Path(result["screenshot"]).is_file()
    assert Path(result["trace"]).is_file()


def test_model_policy_rechecks_one_bug_candidate_before_reporting(tmp_path: Path) -> None:
    case = next(case for case in CASES if case.has_bug and not case.name and not case.message)
    decider = scripted_form_decider(["open", "submit", "report_bug"])
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            result = run_form_case(browser, case, tmp_path, policy="model", decider=decider)
        finally:
            browser.close()

    assert result["run_status"] == "suspected_bug"
    assert result["form_assertion"]["passed"] is False
    assert result["agent_conclusion"] == "bug"
    assert result["task_success"] is True
    assert result["reported_bug_type"] == "required_field_bypass"
    assert [step["observation"]["form_submission_count"] for step in result["steps"][-2:]] == ["1", "2"]
    assert summarize_form_results([result])["confusion"] == {"tp": 1, "fp": 0, "fn": 0, "tn": 0}
    assert classify_bad_case(result) is None


def test_model_policy_scores_verified_bug_with_real_browser_evidence(tmp_path: Path) -> None:
    case = next(case for case in CASES if case.has_bug and not case.name and not case.message)
    decider = scripted_form_decider(["open", "submit", "submit", "report_bug"])
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            result = run_form_case(browser, case, tmp_path, policy="model", decider=decider)
        finally:
            browser.close()

    assert result["policy"] == MODEL_POLICY_NAME
    assert result["run_status"] == "suspected_bug"
    assert result["reported_bug_type"] == "required_field_bypass"
    assert result["reproduced_in_session"] is True
    assert result["form_assertion"]["passed"] is False
    assert result["agent_conclusion"] == "bug"
    assert result["task_success"] is True
    assert result["model_calls"] == 4
    assert summarize_form_results([result])["confusion"] == {"tp": 1, "fp": 0, "fn": 0, "tn": 0}
    assert Path(result["screenshot"]).is_file()
    assert Path(result["trace"]).is_file()


def test_model_suite_without_credentials_creates_no_experiment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEBPROBE_PROVIDER", "openai")
    monkeypatch.delenv("WEBPROBE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("WEBPROBE_MODEL", raising=False)
    output_root = tmp_path / "form_model_evals"
    with pytest.raises(RuntimeError, match="WEBPROBE_API_KEY"):
        run_form_suite(output_root=output_root, policy="model")
    assert not output_root.exists()


def test_single_case_run_saves_trace_and_marks_scope(tmp_path: Path) -> None:
    case = next(case for case in CASES if case.case_id == "feedback_empty_001")
    output_dir = run_form_suite(
        output_root=tmp_path / "single_case", policy="rule", case_id=case.case_id,
    )
    results = [json.loads(line) for line in
               (output_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))
    assert [result["case_id"] for result in results] == [case.case_id]
    assert summary["case_count"] == 1
    assert summary["evaluation_scope"] == "single_case"
    assert summary["selected_case_ids"] == [case.case_id]
    assert "不代表整套数据集成绩" in summary["metrics_note"]
    assert Path(results[0]["screenshot"]).is_file()
    assert Path(results[0]["trace"]).is_file()


def test_unknown_case_id_creates_no_experiment(tmp_path: Path) -> None:
    output_root = tmp_path / "missing_case"
    with pytest.raises(ValueError, match="未知表单 Case ID"):
        run_form_suite(output_root=output_root, case_id="missing_case")
    assert not output_root.exists()


def test_single_case_rejects_full_suite_comparison(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="单题试运行不能"):
        run_form_suite(
            output_root=tmp_path / "comparison", case_id=CASES[0].case_id,
            baseline_summary=tmp_path / "baseline.json",
        )
    assert not (tmp_path / "comparison").exists()
