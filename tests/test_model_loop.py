"""用预写好的假模型响应测试循环；这不是模型能力评测。"""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from openai import APITimeoutError
from playwright.sync_api import sync_playwright

from webprobe.browser_tools import BrowserTools, PageObservation
from webprobe.demo_loop import DemoGoal
from webprobe.llm_decider import Decision, OpenAIDecider
from webprobe.model_loop import run_model_loop


ROOT = Path(__file__).resolve().parents[1]
GOAL = DemoGoal(
    search_url=(ROOT / "lab" / "search.html").as_uri(),
    detail_url=(ROOT / "lab" / "cup.html").as_uri(),
    keyword="茶杯",
    product_name="蓝色茶杯",
    search_status="找到 1 件商品",
    stock_status="库存：有货",
)


def response_for(action: str, number: int) -> SimpleNamespace:
    return SimpleNamespace(output=[SimpleNamespace(
        type="function_call", name="choose_browser_action",
        arguments=json.dumps({"action": action, "summary": f"选择 {action}"}),
        call_id=f"call-{number}",
    )])


def test_model_loop_completes_with_offline_script_and_real_browser() -> None:
    client = Mock()
    actions = ["open", "fill", "submit", "detail", "wait_stock", "stop"]
    replies = iter(response_for(action, number) for number, action in enumerate(actions))
    submitted_inputs: list[list[object]] = []

    def reply(**kwargs: object) -> SimpleNamespace:
        # 保存调用瞬间的副本，避免随后追加历史导致测试“事后看起来通过”。
        submitted_inputs.append(deepcopy(kwargs["input"]))
        return next(replies)

    client.responses.create.side_effect = reply
    decider = OpenAIDecider(client, "offline-test-model")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            tools = BrowserTools(browser.new_page(), {GOAL.search_url, GOAL.detail_url})
            result = run_model_loop(tools, decider, GOAL, max_steps=5, max_model_calls=6)
        finally:
            browser.close()
    assert result.status == "completed"
    assert result.model_calls == 6
    assert [step.action for step in result.steps] == actions[:-1]
    # 第六次调用的 input 包含前五次函数调用及其真实浏览器结果。
    last_input = submitted_inputs[-1]
    outputs = [item for item in last_input if isinstance(item, dict)
               and item.get("type") == "function_call_output"]
    assert len(outputs) == 5
    assert json.loads(outputs[2]["output"])["observation"]["status"] == GOAL.search_status


def test_scripted_model_can_verify_slow_search_after_waiting() -> None:
    """只验证工具接线与结束断言；动作顺序由测试预先写好。"""
    slow_goal = DemoGoal(
        GOAL.search_url + "?delay_ms=1200", GOAL.detail_url, GOAL.keyword,
        GOAL.product_name, GOAL.search_status, GOAL.stock_status,
    )
    actions = ["open", "fill", "submit", "wait_search", "detail", "wait_stock", "stop"]
    client = Mock()
    client.responses.create.side_effect = [
        response_for(action, number) for number, action in enumerate(actions)
    ]
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            tools = BrowserTools(browser.new_page(), {slow_goal.search_url, GOAL.detail_url})
            result = run_model_loop(
                tools, OpenAIDecider(client, "offline-test-model"), slow_goal,
                max_steps=6, max_model_calls=7,
            )
        finally:
            browser.close()

    assert result.status == "completed"
    assert [step.action for step in result.steps] == actions[:-1]
    assert result.steps[2].observation.status == "搜索中…"
    assert result.steps[3].observation.visible_products == (GOAL.product_name,)


def test_premature_model_stop_is_not_task_success() -> None:
    client = Mock()
    client.responses.create.return_value = response_for("stop", 0)
    result = run_model_loop(Mock(), OpenAIDecider(client, "offline-test-model"),
                            GOAL, max_steps=5, max_model_calls=2)
    assert result.status == "needs_review"
    assert result.steps == []


def test_model_loop_passes_remaining_time_and_stops_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [30.0]
    monkeypatch.setattr("webprobe.time_budget.time.monotonic", lambda: clock[0])
    decider = Mock()

    def slow_decision(*args: object, **kwargs: object) -> Decision:
        assert kwargs["timeout_seconds"] == 1.0
        clock[0] += 1.1
        return Decision("open", "Open the search page.", "call-1")

    decider.choose.side_effect = slow_decision
    tools = Mock()
    result = run_model_loop(
        tools, decider, GOAL, max_steps=5, max_model_calls=6, max_seconds=1.0,
    )

    assert result.status == "time_budget_exhausted"
    assert result.model_calls == 1
    assert result.steps == []
    tools.open.assert_not_called()


def test_model_request_timeout_is_kept_when_case_budget_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [30.0]
    monkeypatch.setattr("webprobe.time_budget.time.monotonic", lambda: clock[0])
    decider = Mock()

    def timeout(*args: object, **kwargs: object) -> None:
        clock[0] += 1.1
        raise APITimeoutError(request=Mock())

    decider.choose.side_effect = timeout
    result = run_model_loop(
        Mock(), decider, GOAL, max_steps=5, max_model_calls=6, max_seconds=1.0,
    )

    assert result.status == "time_budget_exhausted"
    assert result.model_calls == 1
    assert result.model_error_type == "request_timeout"
    assert result.error == "Request timed out."


def test_wrong_search_product_cannot_pass_model_completion_check() -> None:
    """最近一次提交错配时，较早的正确结果和后续详情页都不能放行。"""
    client = Mock()
    actions = ["open", "fill", "submit", "submit", "detail", "wait_stock", "stop"]
    client.responses.create.side_effect = [
        response_for(action, number) for number, action in enumerate(actions)
    ]
    tools = Mock()
    tools.open.return_value = PageObservation(GOAL.search_url, "搜索", None, None, None)
    tools.fill.return_value = PageObservation(
        GOAL.search_url, "搜索", None, "找到 2 件商品", GOAL.keyword,
        ("蓝色茶杯", "帆布包"),
    )
    tools.click.side_effect = [
        PageObservation(
            GOAL.search_url, "搜索", None, GOAL.search_status,
            GOAL.keyword, (GOAL.product_name,),
        ),
        PageObservation(
            GOAL.search_url, "搜索", None, GOAL.search_status,
            GOAL.keyword, ("帆布包",),
        ),
        PageObservation(GOAL.detail_url, "详情", GOAL.product_name, "加载中", None),
    ]
    tools.wait_for_status.return_value = PageObservation(
        GOAL.detail_url, "详情", GOAL.product_name, GOAL.stock_status, None,
    )

    result = run_model_loop(
        tools, OpenAIDecider(client, "offline-test-model"), GOAL,
        max_steps=6, max_model_calls=7,
    )

    assert result.status == "needs_review"
    assert result.steps[2].observation.visible_products == (GOAL.product_name,)
    assert result.steps[3].observation.visible_products == ("帆布包",)


def test_model_reports_search_bug_after_two_valid_mismatches_in_one_session() -> None:
    bug_goal = DemoGoal(
        GOAL.search_url + "?bug=search_mismatch", GOAL.detail_url, GOAL.keyword,
        GOAL.product_name, GOAL.search_status, GOAL.stock_status,
    )
    actions = ["open", "fill", "submit", "submit", "report_bug"]
    client = Mock()
    client.responses.create.side_effect = [
        response_for(action, number) for number, action in enumerate(actions)
    ]
    tools = Mock()
    tools.open.return_value = PageObservation(
        bug_goal.search_url, "搜索", None, None, None,
    )
    tools.fill.return_value = PageObservation(
        bug_goal.search_url, "搜索", None, GOAL.search_status,
        bug_goal.keyword, ("帆布包",),
    )
    mismatch = PageObservation(
        bug_goal.search_url, "搜索", None, GOAL.search_status,
        bug_goal.keyword, ("帆布包",),
    )
    tools.click.side_effect = [mismatch, mismatch]
    result = run_model_loop(
        tools, OpenAIDecider(client, "offline-test-model"), bug_goal,
        max_steps=4, max_model_calls=5,
    )

    assert result.status == "suspected_bug"
    assert result.bug_type == "search_result_mismatch"
    assert result.reproduced_in_session is True
    mismatched_submissions = [
        step for step in result.steps
        if step.action == "submit" and step.observation is not None
        and step.observation.status == GOAL.search_status
        and step.observation.visible_products != (GOAL.product_name,)
    ]
    assert len(mismatched_submissions) == 2


def test_premature_bug_report_triggers_one_safe_search_recheck() -> None:
    actions = ["open", "fill", "submit", "report_bug"]
    client = Mock()
    client.responses.create.side_effect = [
        response_for(action, number) for number, action in enumerate(actions)
    ]
    tools = Mock()
    tools.open.return_value = PageObservation(GOAL.search_url, "搜索", None, None, None)
    tools.fill.return_value = PageObservation(
        GOAL.search_url, "搜索", None, GOAL.search_status, GOAL.keyword, ("帆布包",),
    )
    tools.click.return_value = PageObservation(
        GOAL.search_url, "搜索", None, GOAL.search_status,
        GOAL.keyword, ("帆布包",),
    )
    decider = OpenAIDecider(client, "offline-test-model")

    result = run_model_loop(
        tools, decider, GOAL, max_steps=4, max_model_calls=5,
    )

    assert result.status == "suspected_bug"
    assert result.bug_type == "search_result_mismatch"
    assert result.reproduced_in_session is True
    assert [step.action for step in result.steps] == ["open", "fill", "submit", "submit"]
    assert len([step for step in result.steps if step.action == "submit"]) == 2
    assert result.model_calls == 4
    assert any("程序自动重复同一关键词搜索" in item for item in result.recoveries)


def test_premature_report_with_matching_search_is_not_accepted_as_bug() -> None:
    actions = ["open", "fill", "submit", "report_bug", "halt"]
    client = Mock()
    client.responses.create.side_effect = [
        response_for(action, number) for number, action in enumerate(actions)
    ]
    tools = Mock()
    tools.open.return_value = PageObservation(GOAL.search_url, "搜索", None, None, None)
    tools.fill.return_value = PageObservation(
        GOAL.search_url, "搜索", None, GOAL.search_status, GOAL.keyword, (GOAL.product_name,),
    )
    tools.click.return_value = PageObservation(
        GOAL.search_url, "搜索", None, GOAL.search_status,
        GOAL.keyword, (GOAL.product_name,),
    )

    result = run_model_loop(
        tools, OpenAIDecider(client, "offline-test-model"), GOAL,
        max_steps=4, max_model_calls=5,
    )

    assert result.status == "needs_review"
    assert result.bug_type is None
    assert result.reproduced_in_session is False
    assert [step.action for step in result.steps] == ["open", "fill", "submit", "submit"]


def test_model_call_budget_stops_after_one_action() -> None:
    client = Mock()
    client.responses.create.return_value = response_for("open", 0)
    observation = PageObservation(GOAL.search_url, "搜索", None, None, None)
    tools = Mock()
    tools.open.return_value = observation
    result = run_model_loop(tools, OpenAIDecider(client, "offline-test-model"),
                            GOAL, max_steps=5, max_model_calls=1)
    assert result.status == "model_call_budget_exhausted"
    assert result.model_calls == 1
    assert [step.action for step in result.steps] == ["open"]


def test_step_budget_blocks_second_browser_action() -> None:
    client = Mock()
    client.responses.create.side_effect = [response_for("open", 0), response_for("fill", 1)]
    tools = Mock()
    tools.open.return_value = PageObservation(GOAL.search_url, "搜索", None, None, None)
    result = run_model_loop(tools, OpenAIDecider(client, "offline-test-model"),
                            GOAL, max_steps=1, max_model_calls=2)
    assert result.status == "step_budget_exhausted"
    assert [step.action for step in result.steps] == ["open"]
    tools.fill.assert_not_called()
