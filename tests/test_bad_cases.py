"""Bad Case 分类只看结构化结果，不修改任务评分。"""

from webprobe.bad_cases import classify_bad_case


def sample_result(**changes: object) -> dict[str, object]:
    result: dict[str, object] = {
        "case_id": "example_001",
        "task_success": False,
        "run_status": "needs_review",
        "agent_conclusion": None,
        "ground_truth": {"has_bug": False},
        "steps": [],
        "screenshot": "example.png",
        "trace": "example.zip",
    }
    result.update(changes)
    return result


def test_successful_case_is_not_a_bad_case() -> None:
    assert classify_bad_case(sample_result(task_success=True)) is None


def test_false_positive_and_missed_bug_use_fixed_truth() -> None:
    false_positive = sample_result(run_status="suspected_bug", agent_conclusion="bug")
    missed_bug = sample_result(
        run_status="completed", agent_conclusion="no_bug",
        ground_truth={"has_bug": True},
    )
    assert classify_bad_case(false_positive)["category"] == "False Positive"
    assert classify_bad_case(missed_bug)["category"] == "Missed Bug"


def test_timeout_category_needs_structured_error() -> None:
    timeout = sample_result(
        run_status="execution_failed",
        steps=[{"action": "wait_search", "error_type": "SearchWaitTimeout"}],
    )
    other_error = sample_result(
        run_status="execution_failed",
        steps=[{"action": "wait_search", "error_type": "ValueError"}],
    )
    assert classify_bad_case(timeout)["category"] == "Timeout / Loading Issue"
    assert classify_bad_case(other_error)["category"] == "Tool Failure"


def test_inconclusive_is_not_assumed_to_be_no_bug() -> None:
    record = classify_bad_case(sample_result())
    assert record["category"] == "Inconclusive"
    assert record["agent_conclusion"] is None
    assert record["requires_manual_review"] is True


def test_model_failure_keeps_provider_error_detail() -> None:
    record = classify_bad_case(sample_result(
        run_status="model_failed", model_error_type="ProviderLocationUnavailable",
    ))
    assert record["category"] == "Model Failure"
    assert record["model_error_type"] == "ProviderLocationUnavailable"
    assert "ProviderLocationUnavailable" in record["basis"]


def test_time_budget_exhaustion_has_its_own_bad_case_category() -> None:
    record = classify_bad_case(sample_result(run_status="time_budget_exhausted"))
    assert record["category"] == "Budget Exhausted"
