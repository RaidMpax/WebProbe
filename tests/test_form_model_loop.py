"""预写模型响应 + 真实本地浏览器：验证表单工具调用接线，不算模型成绩。"""

import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from playwright.sync_api import sync_playwright

from webprobe.browser_tools import BrowserTools
from webprobe.form_dataset import FormCase, load_form_cases
from webprobe.form_loop import FormGoal
from webprobe.form_model_loop import classify_model_error, run_form_model_loop
from webprobe.llm_decider import (
    FORM_ACTION_NAMES, Decision, ModelDecisionError, OpenAIDecider,
)


ROOT = Path(__file__).resolve().parents[1]
FORM_URL = (ROOT / "lab" / "feedback.html").as_uri()
CASES = load_form_cases()


def goal_for(case: FormCase) -> FormGoal:
    return FormGoal(
        form_url=FORM_URL, name=case.name, message=case.message,
        expected_submitted=case.expected_submitted,
        expected_status=case.expected_status, task=case.task,
    )


def response_for(action: str, number: int) -> SimpleNamespace:
    return SimpleNamespace(output=[SimpleNamespace(
        type="function_call", name="choose_browser_action",
        arguments=json.dumps({"action": action, "summary": f"选择 {action}"}),
        call_id=f"form-call-{number}",
    )])


def test_scripted_form_model_handles_renamed_button() -> None:
    case = next(case for case in CASES if case.page_variant == "renamed_submit")
    actions = ["open", "fill_name", "fill_message", "submit", "stop"]
    replies = iter(response_for(action, number) for number, action in enumerate(actions))
    inputs: list[list[object]] = []
    client = Mock()

    def reply(**kwargs: object) -> SimpleNamespace:
        inputs.append(deepcopy(kwargs["input"]))
        return next(replies)

    client.responses.create.side_effect = reply
    decider = OpenAIDecider(client, "offline-test-model", task_kind="form")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            context.add_init_script("window.__WEBPROBE_RENAME_SUBMIT__ = true")
            tools = BrowserTools(context.new_page(), {FORM_URL})
            result = run_form_model_loop(tools, decider, goal_for(case), 4, 5)
        finally:
            browser.close()

    assert result.status == "completed"
    assert result.model_calls == 5
    assert [step.action for step in result.steps] == actions[:-1]
    assert result.steps[0].observation.form_submit_label == "发送反馈"
    first_payload = json.loads(inputs[0][0]["content"])
    assert first_payload["goal"]["task"] == case.task
    assert "has_bug" not in first_payload["goal"]
    outputs = [item for item in inputs[-1] if isinstance(item, dict)
               and item.get("type") == "function_call_output"]
    assert len(outputs) == 4
    assert json.loads(outputs[0]["output"])["observation"]["form_submit_label"] == "发送反馈"
    offered_actions = client.responses.create.call_args.kwargs["tools"][0]["parameters"]["properties"]["action"]["enum"]
    assert offered_actions == list(FORM_ACTION_NAMES)
    output = ROOT / "artifacts" / "form_model_scripted.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")


def test_premature_form_model_stop_needs_review() -> None:
    case = CASES[0]
    client = Mock()
    client.responses.create.return_value = response_for("stop", 0)
    result = run_form_model_loop(
        Mock(), OpenAIDecider(client, "offline-test-model", task_kind="form"),
        goal_for(case), 4, 2,
    )
    assert result.status == "needs_review"
    assert result.steps == []


def test_wrong_form_result_cannot_pass_model_stop() -> None:
    case = next(case for case in CASES if case.has_bug and not case.name and not case.message)
    actions = ["open", "submit", "stop"]
    client = Mock()
    client.responses.create.side_effect = [
        response_for(action, number) for number, action in enumerate(actions)
    ]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            context.add_init_script("window.__WEBPROBE_REQUIRED_BYPASS__ = true")
            tools = BrowserTools(context.new_page(), {FORM_URL})
            result = run_form_model_loop(
                tools, OpenAIDecider(client, "offline-test-model", task_kind="form"),
                goal_for(case), 2, 3,
            )
        finally:
            browser.close()
    assert result.status == "needs_review"
    assert result.steps[-1].observation.status == "提交成功"
    assert result.steps[-1].observation.form_submission_count == "1"


def test_model_stop_after_one_bypass_submission_triggers_safe_recheck() -> None:
    case = next(case for case in CASES if case.has_bug and not case.name and not case.message)
    actions = ["open", "submit", "stop"]
    client = Mock()
    client.responses.create.side_effect = [
        response_for(action, number) for number, action in enumerate(actions)
    ]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            context.add_init_script("window.__WEBPROBE_REQUIRED_BYPASS__ = true")
            tools = BrowserTools(context.new_page(), {FORM_URL})
            result = run_form_model_loop(
                tools, OpenAIDecider(client, "offline-test-model", task_kind="form"),
                goal_for(case), max_steps=3, max_model_calls=3,
            )
        finally:
            browser.close()

    assert result.status == "suspected_bug"
    assert result.model_calls == 3
    assert [step.action for step in result.steps] == ["open", "submit", "submit"]
    assert result.reproduced_in_session is True
    assert result.recoveries == [
        "首次空必填提交显示成功；已在本地 TestLab 同一会话重提一次核验。"
    ]


def test_repeated_fill_is_redirected_to_the_other_unfilled_field() -> None:
    case = next(case for case in CASES if case.case_id == "feedback_valid_002")
    actions = ["open", "fill_name", "fill_name", "submit", "stop"]
    client = Mock()
    client.responses.create.side_effect = [
        response_for(action, number) for number, action in enumerate(actions)
    ]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            tools = BrowserTools(browser.new_page(), {FORM_URL})
            result = run_form_model_loop(
                tools, OpenAIDecider(client, "offline-test-model", task_kind="form"),
                goal_for(case), max_steps=4, max_model_calls=5,
            )
        finally:
            browser.close()

    assert result.status == "completed"
    assert [step.action for step in result.steps] == [
        "open", "fill_name", "fill_message", "submit",
    ]
    assert result.recoveries == [
        "重复填写姓名；观察值已正确，改为填写反馈内容。"
    ]


def test_model_bug_report_requires_two_real_submissions() -> None:
    case = next(case for case in CASES if case.has_bug and not case.name and not case.message)
    actions = ["open", "submit", "submit", "report_bug"]
    client = Mock()
    client.responses.create.side_effect = [
        response_for(action, number) for number, action in enumerate(actions)
    ]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            context.add_init_script("window.__WEBPROBE_REQUIRED_BYPASS__ = true")
            tools = BrowserTools(context.new_page(), {FORM_URL})
            result = run_form_model_loop(
                tools, OpenAIDecider(client, "offline-test-model", task_kind="form"),
                goal_for(case), max_steps=3, max_model_calls=4,
            )
        finally:
            browser.close()
    assert result.status == "suspected_bug"
    assert result.bug_type == "required_field_bypass"
    assert result.reproduced_in_session is True
    assert [step.action for step in result.steps] == actions[:-1]
    assert [step.observation.form_submission_count for step in result.steps[-2:]] == ["1", "2"]


def test_model_bug_report_after_one_candidate_rechecks_before_reporting() -> None:
    case = next(case for case in CASES if case.has_bug and not case.name and not case.message)
    actions = ["open", "submit", "report_bug"]
    client = Mock()
    client.responses.create.side_effect = [
        response_for(action, number) for number, action in enumerate(actions)
    ]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            context.add_init_script("window.__WEBPROBE_REQUIRED_BYPASS__ = true")
            tools = BrowserTools(context.new_page(), {FORM_URL})
            result = run_form_model_loop(
                tools, OpenAIDecider(client, "offline-test-model", task_kind="form"),
                goal_for(case), max_steps=3, max_model_calls=3,
            )
        finally:
            browser.close()
    assert result.status == "suspected_bug"
    assert result.bug_type == "required_field_bypass"
    assert result.reproduced_in_session is True
    assert [step.observation.form_submission_count for step in result.steps[-2:]] == ["1", "2"]
    assert result.recoveries


def test_model_bug_report_on_normal_form_is_not_accepted() -> None:
    case = CASES[0]
    actions = ["open", "fill_name", "fill_message", "submit", "report_bug"]
    client = Mock()
    client.responses.create.side_effect = [
        response_for(action, number) for number, action in enumerate(actions)
    ]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            tools = BrowserTools(browser.new_page(), {FORM_URL})
            result = run_form_model_loop(
                tools, OpenAIDecider(client, "offline-test-model", task_kind="form"),
                goal_for(case), max_steps=4, max_model_calls=5,
            )
        finally:
            browser.close()
    assert result.status == "needs_review"
    assert result.bug_type is None
    assert result.reproduced_in_session is False
    assert result.steps[-1].observation.status == "提交成功"


def test_invalid_form_model_action_is_rejected() -> None:
    client = Mock()
    client.responses.create.return_value = response_for("delete_data", 0)
    result = run_form_model_loop(
        Mock(), OpenAIDecider(client, "offline-test-model", task_kind="form"),
        goal_for(CASES[0]), 4, 2,
    )
    assert result.status == "model_failed"
    assert result.steps == []


def test_model_errors_are_classified_without_calling_provider() -> None:
    class ProviderError(Exception):
        def __init__(self, status_code: int, message: str) -> None:
            super().__init__(message)
            self.status_code = status_code

    assert classify_model_error(TimeoutError("Request timed out.")) == "ModelTimeout"
    assert classify_model_error(
        ProviderError(400, "User location is not supported for the API use.")
    ) == "ProviderLocationUnavailable"
    assert classify_model_error(ProviderError(429, "quota exceeded")) == "ModelRateLimit"
    assert classify_model_error(ModelDecisionError("invalid tool response")) == "InvalidModelResponse"


def test_model_loop_passes_remaining_time_to_decider_and_stops_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [5.0]
    monkeypatch.setattr("webprobe.time_budget.time.monotonic", lambda: clock[0])
    decider = Mock()

    def slow_decision(*args: object, **kwargs: object) -> Decision:
        assert kwargs["timeout_seconds"] == 1.0
        clock[0] += 1.1
        return Decision("open", "Open feedback form.", "call-1")

    decider.choose.side_effect = slow_decision
    tools = Mock(spec=BrowserTools)
    result = run_form_model_loop(
        tools, decider, goal_for(CASES[0]), max_steps=3,
        max_model_calls=4, max_seconds=1.0,
    )

    assert result.status == "time_budget_exhausted"
    assert result.model_calls == 1
    assert result.steps == []
    tools.open.assert_not_called()
