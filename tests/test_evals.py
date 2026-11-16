"""验证评测数据与评分边界；不需要 API Key 或浏览器。"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from playwright.sync_api import sync_playwright

from webprobe.bad_cases import classify_bad_case
from webprobe.browser_tools import PageObservation
from webprobe.demo_loop import RunResult, StepRecord
from webprobe.evals import (
    DATASET, MODEL_POLICY_NAME, POLICY_NAME, load_cases, run_case, run_suite,
    score_case, summarize_results,
)
from webprobe.llm_decider import OpenAIDecider
from webprobe.model_loop import ModelRunResult


def test_fixed_dataset_has_normal_and_injected_bug_cases() -> None:
    cases = load_cases(DATASET)
    assert [case.case_id for case in cases] == [
        "search_normal_001", "search_mismatch_001", "search_slow_001",
        "search_over_wait_001",
    ]
    assert [case.has_bug for case in cases] == [False, True, False, False]
    assert [case.success_condition for case in cases] == [
        "complete_without_bug", "report_search_result_mismatch",
        "complete_without_bug", "complete_without_bug",
    ]
    # 四案共享相同的正确产品规格；故障页面不能自行改写标准答案。
    assert all(case.expected_products == ("蓝色茶杯",) for case in cases)
    assert cases[2].load_delay_ms == 1200
    assert cases[3].load_delay_ms == 3000


def test_execution_failure_has_no_bug_conclusion() -> None:
    normal, injected_bug = load_cases()[:2]
    completed = RunResult("completed", [])
    failed = RunResult("execution_failed", [])
    reported = RunResult("suspected_bug", [], "search_result_mismatch", True)
    assert score_case(normal, completed, 2.0, True) == ("no_bug", True)
    assert score_case(injected_bug, failed, 2.0, False) == (None, False)
    # 即使在故障页完成流程却声称正常，也不能算任务成功。
    assert score_case(injected_bug, completed, 2.0, False) == ("no_bug", False)
    assert score_case(normal, completed, 2.0, None) == ("no_bug", False)
    assert score_case(injected_bug, reported, 2.0, False) == ("bug", True)
    assert score_case(injected_bug, reported, 2.0, True) == ("bug", False)


def test_confusion_matrix_counts_cases_with_conclusions_only() -> None:
    def result(has_bug: bool, conclusion: str | None) -> dict[str, object]:
        return {
            "policy": POLICY_NAME,
            "ground_truth": {"has_bug": has_bug},
            "agent_conclusion": conclusion,
            "task_success": False,
            "run_status": "needs_review" if conclusion is None else "completed",
            "search_assertion": {"passed": not has_bug},
            "reproduced_in_session": False,
            "steps": [],
            "elapsed_seconds": 1.0,
            "time_budget_exceeded": conclusion is None,
        }

    summary = summarize_results([
        result(True, "bug"), result(False, "bug"),
        result(True, "no_bug"), result(False, "no_bug"),
        result(True, None),
    ])
    assert summary["confusion"] == {"tp": 1, "fp": 1, "fn": 1, "tn": 1}
    assert summary["unclassified_case_count"] == 1
    assert summary["bug_precision"] == 0.5
    assert summary["bug_recall"] == 0.5
    assert summary["bug_f1"] == 0.5
    assert summary["time_budget_exceeded_count"] == 1


def test_provider_timeout_is_counted_as_model_failure_and_not_bug_conclusion() -> None:
    timed_out = {
        "policy": MODEL_POLICY_NAME,
        "ground_truth": {"has_bug": True},
        "agent_conclusion": None,
        "task_success": False,
        "run_status": "time_budget_exhausted",
        "model_error_type": "request_timeout",
        "search_assertion": {"passed": False},
        "reproduced_in_session": False,
        "steps": [],
        "elapsed_seconds": 20.0,
        "time_budget_exceeded": True,
    }
    summary = summarize_results([timed_out])

    assert summary["model_failure_count"] == 1
    assert summary["model_failure_breakdown"] == {"request_timeout": 1}
    assert summary["time_budget_exceeded_count"] == 1
    assert summary["confusion"] == {"tp": 0, "fp": 0, "fn": 0, "tn": 0}


def test_model_api_timeout_is_classified_for_manual_review() -> None:
    timed_out = {
        "case_id": "search_mismatch_001",
        "run_status": "time_budget_exhausted",
        "agent_conclusion": None,
        "ground_truth": {"has_bug": True},
        "task_success": False,
        "model_error_type": "request_timeout",
        "steps": [],
    }
    bad_case = classify_bad_case(timed_out)

    assert bad_case["category"] == "Model Failure"
    assert bad_case["model_error_type"] == "request_timeout"
    assert bad_case["requires_manual_review"] is True


def test_model_summary_sums_tokens_only_when_every_case_has_complete_usage() -> None:
    base = {
        "policy": MODEL_POLICY_NAME,
        "model": "test-model",
        "search_wait_ms": 2500,
        "ground_truth": {"has_bug": False},
        "agent_conclusion": "no_bug",
        "task_success": True,
        "run_status": "completed",
        "search_assertion": {"passed": True},
        "reproduced_in_session": False,
        "steps": [],
        "elapsed_seconds": 1.0,
    }
    complete = summarize_results([
        {**base, "token_usage": {
            "input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
        }},
        {**base, "token_usage": {
            "input_tokens": 20, "output_tokens": 3, "total_tokens": 23,
        }},
    ])
    incomplete = summarize_results([
        {**base, "token_usage": {
            "input_tokens": 10, "output_tokens": 2, "total_tokens": 12,
        }},
        {**base, "token_usage": None},
    ])

    assert complete["token_usage"] == {
        "input_tokens": 30, "output_tokens": 5, "total_tokens": 35,
    }
    assert complete["token_usage_covered_cases"] == 2
    assert complete["api_cost"] is None
    assert complete["model"] == "test-model"
    assert incomplete["token_usage"] is None
    assert incomplete["token_usage_covered_cases"] == 1


def test_recovery_rate_uses_only_cases_with_a_recovery_record() -> None:
    base = {
        "policy": MODEL_POLICY_NAME,
        "search_wait_ms": 2500,
        "ground_truth": {"has_bug": False},
        "agent_conclusion": "no_bug",
        "run_status": "completed",
        "search_assertion": {"passed": True},
        "reproduced_in_session": False,
        "steps": [],
        "elapsed_seconds": 1.0,
        "token_usage": None,
    }
    summary = summarize_results([
        {**base, "task_success": True, "recoveries": ["重试后完成"]},
        {**base, "task_success": False, "recoveries": ["重试后仍失败"]},
        {**base, "task_success": True, "recoveries": []},
    ])

    assert summary["recovery_attempt_case_count"] == 2
    assert summary["recovered_case_count"] == 1
    assert summary["recovery_rate"] == 0.5


def test_recovery_rate_uses_only_cases_with_a_recovery_record() -> None:
    base = {
        "policy": MODEL_POLICY_NAME,
        "search_wait_ms": 2500,
        "ground_truth": {"has_bug": False},
        "agent_conclusion": "no_bug",
        "run_status": "completed",
        "search_assertion": {"passed": True},
        "reproduced_in_session": False,
        "steps": [],
        "elapsed_seconds": 1.0,
        "token_usage": None,
    }
    summary = summarize_results([
        {**base, "task_success": True, "recoveries": ["重试后完成"]},
        {**base, "task_success": False, "recoveries": ["重试后仍失败"]},
        {**base, "task_success": True, "recoveries": []},
    ])

    assert summary["recovery_attempt_case_count"] == 2
    assert summary["recovered_case_count"] == 1
    assert summary["recovery_rate"] == 0.5


def test_summary_rejects_mixed_search_wait_budgets() -> None:
    base = {
        "policy": POLICY_NAME,
        "ground_truth": {"has_bug": False},
        "agent_conclusion": "no_bug",
        "task_success": True,
        "run_status": "completed",
        "search_assertion": {"passed": True},
        "reproduced_in_session": False,
        "steps": [],
        "elapsed_seconds": 1.0,
    }
    with pytest.raises(ValueError, match="等待预算"):
        summarize_results([
            {**base, "search_wait_ms": 2500},
            {**base, "search_wait_ms": 3500},
        ])


def test_dataset_rejects_truth_that_conflicts_with_bug_switch(tmp_path: Path) -> None:
    case = json.loads(DATASET.read_text(encoding="utf-8").splitlines()[0])
    case["ground_truth"]["has_bug"] = True
    invalid = tmp_path / "invalid.jsonl"
    invalid.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="矛盾"):
        load_cases(invalid)


def test_comparison_rejects_changed_dataset_before_browser_starts(tmp_path: Path) -> None:
    baseline = tmp_path / "summary.json"
    baseline.write_text(json.dumps({
        "dataset": DATASET.name, "case_count": 4, "dataset_sha256": "wrong"
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="内容不一致"):
        run_suite(baseline)


def test_model_policy_scores_only_a_verified_same_session_bug_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """测试评测适配层；假循环输出已核验的两次提交，不调用模型或浏览器。"""
    cases = load_cases()[:2]

    def fake_run_model_loop(tools, decider, goal, **kwargs):
        if goal.search_url.endswith("?bug=search_mismatch"):
            wrong = PageObservation(
                goal.search_url, "搜索", None, goal.search_status,
                goal.keyword, ("帆布包",),
            )
            steps = [StepRecord("submit", wrong), StepRecord("submit", wrong)]
            return ModelRunResult(
                "suspected_bug", steps, 5, ["复查到相同错配"],
                bug_type="search_result_mismatch", reproduced_in_session=True,
            )
        search = PageObservation(
            goal.search_url, "搜索", None, goal.search_status,
            goal.keyword, (goal.product_name,),
        )
        detail = PageObservation(
            goal.detail_url, "详情", goal.product_name, "加载中", None,
        )
        stock = PageObservation(
            goal.detail_url, "详情", goal.product_name, goal.stock_status, None,
        )
        return ModelRunResult(
            "completed", [
                StepRecord("submit", search), StepRecord("detail", detail),
                StepRecord("wait_stock", stock),
            ], 6, ["完成搜索和详情核验"],
        )

    monkeypatch.setattr("webprobe.evals.run_model_loop", fake_run_model_loop)
    contexts = []
    for _ in cases:
        page = Mock()
        page.screenshot.side_effect = lambda path: Path(path).write_bytes(b"png")
        context = Mock()
        context.new_page.return_value = page
        context.tracing.stop.side_effect = lambda path: Path(path).write_bytes(b"trace")
        contexts.append(context)
    browser = Mock()
    browser.new_context.side_effect = contexts
    decider = OpenAIDecider(Mock(), "scripted-fixture")

    results = [
        run_case(browser, case, tmp_path, policy="model", decider=decider)
        for case in cases
    ]

    normal, bug = results
    assert normal["policy"] == bug["policy"] == MODEL_POLICY_NAME
    assert normal["model"] == bug["model"] == "scripted-fixture"
    assert normal["task_success"] is True
    assert normal["model_calls"] == 6
    assert bug["run_status"] == "suspected_bug"
    assert bug["agent_conclusion"] == "bug"
    assert bug["reproduced_in_session"] is True
    assert bug["search_assertion"]["actual_products"] == ["帆布包"]
    assert bug["task_success"] is True
    assert Path(normal["screenshot"]).is_file()
    assert Path(bug["trace"]).is_file()
    summary = summarize_results(results)
    assert summary["unclassified_case_count"] == 0
    assert summary["confusion"] == {"tp": 1, "fp": 0, "fn": 0, "tn": 1}


def test_slow_case_waits_and_is_not_reported_as_bug(tmp_path: Path) -> None:
    slow_case = load_cases()[2]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            result = run_case(browser, slow_case, tmp_path)
        finally:
            browser.close()

    assert result["run_status"] == "completed"
    assert result["agent_conclusion"] == "no_bug"
    assert result["task_success"] is True
    assert result["search_assertion"]["actual_products"] == ["蓝色茶杯"]
    assert [step["action"] for step in result["steps"]] == [
        "open", "fill", "submit", "wait_search", "detail", "wait_stock"
    ]
    assert result["steps"][2]["observation"]["status"] == "搜索中…"
    assert Path(result["screenshot"]).is_file()
    assert Path(result["trace"]).is_file()


def test_baseline_over_wait_case_is_a_reviewable_execution_failure(tmp_path: Path) -> None:
    over_wait_case = load_cases()[3]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            result = run_case(browser, over_wait_case, tmp_path, policy="rule_baseline")
        finally:
            browser.close()

    assert result["run_status"] == "execution_failed"
    assert result["agent_conclusion"] is None
    assert result["task_success"] is False
    assert result["search_assertion"]["passed"] is None
    assert result["steps"][-1]["action"] == "wait_search"
    assert result["steps"][-1]["error_type"] == "SearchWaitTimeout"
    bad_case = classify_bad_case(result)
    assert bad_case["category"] == "Timeout / Loading Issue"
    assert bad_case["ground_truth_has_bug"] is False
    assert bad_case["requires_manual_review"] is True
    assert Path(result["screenshot"]).is_file()
    assert Path(result["trace"]).is_file()


def test_longer_wait_completes_the_same_case_without_bug_report(tmp_path: Path) -> None:
    over_wait_case = load_cases()[3]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            result = run_case(browser, over_wait_case, tmp_path, policy="rule")
        finally:
            browser.close()

    assert result["policy"] == POLICY_NAME
    assert result["search_wait_ms"] == 3500
    assert result["run_status"] == "completed"
    assert result["agent_conclusion"] == "no_bug"
    assert result["task_success"] is True
    assert result["search_assertion"]["actual_products"] == ["蓝色茶杯"]
    assert [step["action"] for step in result["steps"]] == [
        "open", "fill", "submit", "wait_search", "detail", "wait_stock"
    ]


def test_single_case_search_run_saves_scope_and_evidence(tmp_path: Path) -> None:
    output_dir = run_suite(
        policy="rule", case_id="search_mismatch_001", output_root=tmp_path,
    )
    results = [json.loads(line) for line in
               (output_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()]
    summary = json.loads((output_dir / "summary.json").read_text(encoding="utf-8"))

    assert [result["case_id"] for result in results] == ["search_mismatch_001"]
    assert results[0]["agent_conclusion"] == "bug"
    assert summary["case_count"] == 1
    assert summary["evaluation_scope"] == "single_case"
    assert summary["selected_case_ids"] == ["search_mismatch_001"]
    assert "不代表整套数据集成绩" in summary["metrics_note"]
    assert Path(results[0]["screenshot"]).is_file()
    assert Path(results[0]["trace"]).is_file()


def test_unknown_or_comparison_single_case_is_rejected_before_output(tmp_path: Path) -> None:
    output_root = tmp_path / "search_evals"
    with pytest.raises(ValueError, match="未知搜索 Case ID"):
        run_suite(case_id="missing_case", output_root=output_root)
    assert not output_root.exists()

    baseline = tmp_path / "baseline.json"
    baseline.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="单题试运行不能"):
        run_suite(baseline_summary=baseline, case_id="search_normal_001")
    assert not output_root.exists()


def test_live_model_suite_needs_configuration_before_browser_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("WEBPROBE_API_KEY", "OPENAI_API_KEY", "WEBPROBE_MODEL"):
        monkeypatch.delenv(name, raising=False)
    with patch("webprobe.llm_decider.load_project_env"):
        with pytest.raises(RuntimeError, match="请设置"):
            run_suite(policy="model")
