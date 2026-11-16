"""一条可完成任务和一条预算不足任务；都不作网站 Bug 结论。"""

import json
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from playwright.sync_api import sync_playwright

from webprobe.browser_tools import BrowserTools, PageObservation
from webprobe.demo_loop import DemoGoal, execute_action, run_demo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GOAL = DemoGoal(
    search_url=(PROJECT_ROOT / "lab" / "search.html").as_uri(),
    detail_url=(PROJECT_ROOT / "lab" / "cup.html").as_uri(),
    keyword="茶杯",
    product_name="蓝色茶杯",
    search_status="找到 1 件商品",
    stock_status="库存：有货",
)


def test_demo_loop_completes_at_exact_step_budget() -> None:
    output = PROJECT_ROOT / "artifacts" / "demo_run.json"
    output.parent.mkdir(exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            tools = BrowserTools(browser.new_page(), {GOAL.search_url, GOAL.detail_url})
            result = run_demo(tools, GOAL, max_steps=5)
            output.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")
            assert result.status == "completed"
            assert [step.action for step in result.steps] == [
                "open", "fill", "submit", "detail", "wait_stock"
            ]
        finally:
            browser.close()


def test_demo_loop_stops_when_step_budget_is_too_small() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            tools = BrowserTools(browser.new_page(), {GOAL.search_url, GOAL.detail_url})
            result = run_demo(tools, GOAL, max_steps=2)
            assert result.status == "budget_exhausted"
            assert len(result.steps) == 2
        finally:
            browser.close()


def test_demo_loop_enforces_time_budget_after_browser_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [20.0]
    monkeypatch.setattr("webprobe.time_budget.time.monotonic", lambda: clock[0])
    tools = Mock(spec=BrowserTools)

    def slow_open(url: str) -> PageObservation:
        clock[0] += 1.1
        return PageObservation(url, "搜索", "商品搜索", None, None)

    tools.open.side_effect = slow_open
    result = run_demo(tools, GOAL, max_steps=5, max_seconds=1.0)

    assert result.status == "time_budget_exhausted"
    assert [step.action for step in result.steps] == ["open"]
    tools.set_timeout_for_budget.assert_called_once_with(1.0)
    tools.fill.assert_not_called()


def test_explicit_search_wait_is_capped_by_remaining_time() -> None:
    tools = Mock(spec=BrowserTools)
    execute_action(tools, "wait_search", GOAL, remaining_seconds=0.4)
    tools.wait_for_search_ready.assert_called_once_with(timeout_ms=400)


def test_unexpected_result_requires_review_not_bug_label() -> None:
    wrong_expectation = replace(GOAL, search_status="找到 9 件商品")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            tools = BrowserTools(browser.new_page(), {GOAL.search_url, GOAL.detail_url})
            result = run_demo(tools, wrong_expectation, max_steps=5)
            assert result.status == "needs_review"
            assert [step.action for step in result.steps] == ["open", "fill", "submit"]
        finally:
            browser.close()


def test_injected_search_mismatch_is_reported_after_second_observation() -> None:
    bug_goal = replace(GOAL, search_url=GOAL.search_url + "?bug=search_mismatch")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            tools = BrowserTools(browser.new_page(), {bug_goal.search_url, GOAL.detail_url})
            result = run_demo(tools, bug_goal, max_steps=5)
            assert result.status == "suspected_bug"
            assert result.bug_type == "search_result_mismatch"
            assert result.reproduced_in_session is True
            assert [step.action for step in result.steps] == [
                "open", "fill", "submit", "submit"
            ]
            assert [step.observation.visible_products for step in result.steps[-2:]] == [
                ("帆布包",), ("帆布包",)
            ]
        finally:
            browser.close()


def test_transient_wrong_result_does_not_become_bug_report() -> None:
    """第一次结果错误、复查后正常时，继续完成任务。"""
    def search_observation(value: str, products: tuple[str, ...], status: str) -> PageObservation:
        return PageObservation(GOAL.search_url, "搜索", "商品搜索", status, value, products)

    tools = Mock()
    tools.open.return_value = search_observation("", ("蓝色茶杯", "帆布包"), "找到 2 件商品")
    tools.fill.return_value = search_observation("茶杯", ("蓝色茶杯", "帆布包"), "找到 2 件商品")
    tools.click.side_effect = [
        search_observation("茶杯", ("帆布包",), GOAL.search_status),
        search_observation("茶杯", ("蓝色茶杯",), GOAL.search_status),
        PageObservation(GOAL.detail_url, "详情", "蓝色茶杯", "库存：加载中…", None),
    ]
    tools.wait_for_status.return_value = PageObservation(
        GOAL.detail_url, "详情", "蓝色茶杯", GOAL.stock_status, None
    )
    result = run_demo(tools, GOAL, max_steps=6)
    assert result.status == "completed"
    assert result.bug_type is None
    assert [step.action for step in result.steps] == [
        "open", "fill", "submit", "submit", "detail", "wait_stock"
    ]


def test_search_wait_timeout_is_execution_failure_not_bug() -> None:
    slow_goal = replace(GOAL, search_url=GOAL.search_url + "?delay_ms=3000")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            tools = BrowserTools(browser.new_page(), {slow_goal.search_url, GOAL.detail_url})
            result = run_demo(tools, slow_goal, max_steps=6)
            assert result.status == "execution_failed"
            assert result.bug_type is None
            assert [step.action for step in result.steps] == [
                "open", "fill", "submit", "wait_search"
            ]
            assert result.steps[-1].error is not None
            assert result.steps[-1].error_type == "SearchWaitTimeout"
        finally:
            browser.close()
