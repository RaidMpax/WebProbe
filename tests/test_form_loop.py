"""表单规则循环：固定案例、步骤预算、恢复与工具失败。"""

import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock

import pytest
from playwright.sync_api import sync_playwright

from webprobe.browser_tools import BrowserTools, PageObservation
from webprobe.form_dataset import FormCase, load_form_cases
from webprobe.form_loop import FormGoal, run_form_loop


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORM_URL = (PROJECT_ROOT / "lab" / "feedback.html").as_uri()
CASES = load_form_cases()


def goal_for(case: FormCase) -> FormGoal:
    """只传任务和产品预期；故障标签只留给测试断言。"""
    return FormGoal(
        form_url=FORM_URL,
        name=case.name,
        message=case.message,
        expected_submitted=case.expected_submitted,
        expected_status=case.expected_status,
    )


@pytest.mark.parametrize("case", CASES, ids=[case.case_id for case in CASES])
def test_form_rule_loop_runs_fixed_cases(case: FormCase) -> None:
    goal = goal_for(case)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            if case.bug_mode == "required_bypass":
                context.add_init_script("window.__WEBPROBE_REQUIRED_BYPASS__ = true")
            if case.page_variant == "renamed_submit":
                context.add_init_script("window.__WEBPROBE_RENAME_SUBMIT__ = true")
            tools = BrowserTools(context.new_page(), {goal.form_url})
            result = run_form_loop(tools, goal, max_steps=case.max_steps)
        finally:
            browser.close()

    output = PROJECT_ROOT / "artifacts" / "form_loops" / f"{case.case_id}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")

    expected_actions = ["open"]
    if case.name:
        expected_actions.append("fill_name")
    if case.message:
        expected_actions.append("fill_message")
    expected_actions.append("submit")
    if case.page_variant == "renamed_submit":
        assert all(step.observation.form_submit_label == "发送反馈" for step in result.steps)

    if case.has_bug:
        assert result.status == "suspected_bug"
        assert result.bug_type == case.bug_type
        assert result.reproduced_in_session is True
        assert [step.action for step in result.steps] == expected_actions + ["submit"]
        assert [step.observation.form_submission_count for step in result.steps[-2:]] == [
            "1", "2"
        ]
    else:
        assert result.status == "completed"
        assert result.bug_type is None
        assert [step.action for step in result.steps] == expected_actions


def form_observation(status: str, state: str, count: str) -> PageObservation:
    """模拟工具已经返回的表单快照，用于检查决策分支。"""
    return PageObservation(
        url=FORM_URL,
        title="反馈表单",
        heading="提交反馈",
        status=status,
        search_value=None,
        form_name_value="",
        form_message_value="",
        form_state=state,
        form_submission_count=count,
        form_submit_label="提交反馈",
    )


def test_form_loop_does_not_report_transient_wrong_result() -> None:
    """第一次异常、复查后正常时，完成任务而不报告 Bug。"""
    goal = FormGoal(FORM_URL, "", "", False, "请填写所有必填项")
    tools = Mock(spec=BrowserTools)
    tools.open.return_value = form_observation("尚未提交", "idle", "0")
    tools.click.side_effect = [
        form_observation("提交成功", "submitted", "1"),
        form_observation("请填写所有必填项", "invalid", "0"),
    ]

    result = run_form_loop(tools, goal, max_steps=3)

    assert result.status == "completed"
    assert result.bug_type is None
    assert [step.action for step in result.steps] == ["open", "submit", "submit"]


def test_form_loop_stops_at_step_budget() -> None:
    goal = FormGoal(FORM_URL, "测试用户", "测试内容", True, "提交成功")
    tools = Mock(spec=BrowserTools)
    tools.open.return_value = form_observation("尚未提交", "idle", "0")

    result = run_form_loop(tools, goal, max_steps=1)

    assert result.status == "budget_exhausted"
    assert [step.action for step in result.steps] == ["open"]
    tools.fill.assert_not_called()


def test_form_loop_enforces_time_budget_after_browser_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    goal = FormGoal(FORM_URL, "", "", False, "请填写所有必填项")
    tools = Mock(spec=BrowserTools)
    clock = [10.0]
    monkeypatch.setattr("webprobe.time_budget.time.monotonic", lambda: clock[0])

    def slow_open(url: str) -> PageObservation:
        clock[0] += 1.1
        return form_observation("尚未提交", "idle", "0")

    tools.open.side_effect = slow_open
    result = run_form_loop(tools, goal, max_steps=3, max_seconds=1.0)

    assert result.status == "time_budget_exhausted"
    assert [step.action for step in result.steps] == ["open"]
    tools.set_timeout_for_budget.assert_called_once_with(1.0)
    tools.fill.assert_not_called()


def test_one_wrong_submission_with_no_recheck_budget_is_not_a_bug_report() -> None:
    """疑点出现后若没有复查额度，保留未完成状态。"""
    bug_case = next(case for case in CASES if case.has_bug)
    goal = goal_for(bug_case)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            context.add_init_script("window.__WEBPROBE_REQUIRED_BYPASS__ = true")
            tools = BrowserTools(context.new_page(), {goal.form_url})
            result = run_form_loop(tools, goal, max_steps=2)
        finally:
            browser.close()

    assert result.status == "budget_exhausted"
    assert result.bug_type is None
    assert result.reproduced_in_session is False
    assert [step.action for step in result.steps] == ["open", "submit"]


def test_form_loop_tool_failure_is_not_bug() -> None:
    goal = FormGoal(FORM_URL, "", "", False, "请填写所有必填项")
    tools = Mock(spec=BrowserTools)
    tools.open.side_effect = ValueError("不允许访问的 URL")

    result = run_form_loop(tools, goal, max_steps=2)

    assert result.status == "execution_failed"
    assert result.bug_type is None
    assert result.reproduced_in_session is False
    assert result.steps[0].error_type == "ValueError"
