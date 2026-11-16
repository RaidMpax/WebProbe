"""固定脚本调用工具，确认每次动作后的页面观察真实且可区分。"""

import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock

import pytest
from playwright.sync_api import sync_playwright

from webprobe.browser_tools import BrowserTools, PageObservation


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_page_action_timeouts_are_capped_by_remaining_budget() -> None:
    page = Mock()
    tools = BrowserTools(page, {"file:///lab/search.html"})

    tools.set_timeout_for_budget(0.25)

    page.set_default_timeout.assert_called_once_with(250)
    page.set_default_navigation_timeout.assert_called_once_with(250)


def test_page_action_timeout_rejects_empty_remaining_budget() -> None:
    tools = BrowserTools(Mock(), {"file:///lab/search.html"})
    with pytest.raises(ValueError, match="剩余浏览器时间"):
        tools.set_timeout_for_budget(0)


def test_tools_return_observations_and_reject_unlisted_url() -> None:
    search_url = (PROJECT_ROOT / "lab" / "search.html").as_uri()
    detail_url = (PROJECT_ROOT / "lab" / "cup.html").as_uri()
    output = PROJECT_ROOT / "artifacts" / "tool_steps.json"
    output.parent.mkdir(exist_ok=True)
    steps: list[PageObservation] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            tools = BrowserTools(browser.new_page(), {search_url, detail_url})
            steps.append(tools.open(search_url))
            assert steps[-1].form_name_value is None
            assert steps[-1].form_submission_count is None
            steps.append(tools.fill("searchbox", "商品关键词", "茶杯"))
            assert steps[-1].search_value == "茶杯"
            steps.append(tools.click("button", "搜索"))
            assert steps[-1].status == "找到 1 件商品"

            steps.append(tools.click("link", "蓝色茶杯"))
            assert steps[-1].url == detail_url
            assert steps[-1].heading == "蓝色茶杯"

            steps.append(tools.back())
            assert steps[-1].url == search_url
            with pytest.raises(ValueError, match="不允许访问"):
                tools.open("https://example.com")
            assert tools.observe().url == search_url
        finally:
            output.write_text(
                json.dumps([asdict(step) for step in steps], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            browser.close()


def test_form_tools_observe_invalid_then_valid_submission() -> None:
    """工具只返回事实；这里的固定脚本负责选择填写与点击顺序。"""
    form_url = (PROJECT_ROOT / "lab" / "feedback.html").as_uri()
    output = PROJECT_ROOT / "artifacts" / "form_tool_steps.json"
    output.parent.mkdir(exist_ok=True)
    steps: list[PageObservation] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            tools = BrowserTools(browser.new_page(), {form_url})
            steps.append(tools.open(form_url))
            assert steps[-1].form_name_value == ""
            assert steps[-1].form_message_value == ""
            assert steps[-1].form_state == "idle"
            assert steps[-1].form_submission_count == "0"

            steps.append(tools.click("button", "提交反馈"))
            assert steps[-1].status == "请填写所有必填项"
            assert steps[-1].form_state == "invalid"
            assert steps[-1].form_submission_count == "0"

            steps.append(tools.fill("textbox", "姓名（必填）", "测试用户"))
            assert steps[-1].form_name_value == "测试用户"
            steps.append(tools.fill("textbox", "反馈内容（必填）", "测试内容"))
            assert steps[-1].form_message_value == "测试内容"
            steps.append(tools.click("button", "提交反馈"))
            assert steps[-1].status == "提交成功"
            assert steps[-1].form_state == "submitted"
            assert steps[-1].form_submission_count == "1"
        finally:
            output.write_text(
                json.dumps([asdict(step) for step in steps], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            browser.close()


def test_form_tools_report_bug_page_as_observation_only() -> None:
    """故障页成功提交空表单，但 BrowserTools 不自行宣称网站有 Bug。"""
    form_url = (PROJECT_ROOT / "lab" / "feedback.html").as_uri()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            context.add_init_script("window.__WEBPROBE_REQUIRED_BYPASS__ = true")
            tools = BrowserTools(context.new_page(), {form_url})
            initial = tools.open(form_url)
            assert initial.form_name_value == ""
            assert initial.form_message_value == ""
            assert initial.form_submission_count == "0"

            after_click = tools.click("button", "提交反馈")
            assert after_click.status == "提交成功"
            assert after_click.form_state == "submitted"
            assert after_click.form_submission_count == "1"
            assert after_click.url == form_url
        finally:
            browser.close()


def test_form_tools_observe_renamed_submit_button() -> None:
    """工具读取按钮当前文字，供上层选择定位器；工具本身不决定任务。"""
    form_url = (PROJECT_ROOT / "lab" / "feedback.html").as_uri()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            context.add_init_script("window.__WEBPROBE_RENAME_SUBMIT__ = true")
            tools = BrowserTools(context.new_page(), {form_url})
            initial = tools.open(form_url)
            assert initial.form_submit_label == "发送反馈"
            assert initial.url == form_url
            after_click = tools.click("button", initial.form_submit_label)
            assert after_click.status == "请填写所有必填项"
        finally:
            browser.close()
