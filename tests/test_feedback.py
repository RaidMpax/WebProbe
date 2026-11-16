"""反馈表单的固定产品规格：空必填项不能成功提交。"""

from pathlib import Path

from playwright.sync_api import expect, sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def feedback_url() -> str:
    """每次打开本地 HTML 都从提交次数为零的初始状态开始。"""
    return (PROJECT_ROOT / "lab" / "feedback.html").as_uri()


def test_empty_required_fields_block_submission() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(feedback_url())
            form = page.locator("#feedback-form")

            page.get_by_role("button", name="提交反馈").click()

            expect(page.get_by_role("status")).to_have_text("请填写所有必填项")
            expect(form).to_have_attribute("data-form-state", "invalid")
            expect(form).to_have_attribute("data-submission-count", "0")
            # 填了姓名但仍缺反馈内容，也不应提交成功。
            page.get_by_label("姓名（必填）").fill("测试用户")
            page.get_by_role("button", name="提交反馈").click()
            expect(form).to_have_attribute("data-submission-count", "0")
        finally:
            browser.close()


def test_valid_feedback_submission_succeeds() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(feedback_url())
            page.get_by_label("姓名（必填）").fill("测试用户")
            page.get_by_label("反馈内容（必填）").fill("搜索页面可以正常使用")
            page.get_by_role("button", name="提交反馈").click()

            expect(page.get_by_role("status")).to_have_text("提交成功")
            expect(page.locator("#feedback-form")).to_have_attribute(
                "data-submission-count", "1"
            )
        finally:
            browser.close()


def test_required_bypass_bug_accepts_empty_feedback() -> None:
    """确认故障开关稳定违反固定规格；这不是 Agent 发现 Bug 的成绩。"""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            context.add_init_script("window.__WEBPROBE_REQUIRED_BYPASS__ = true")
            page = context.new_page()
            page.goto(feedback_url())
            form = page.locator("#feedback-form")
            expect(form).to_have_attribute("data-submission-count", "0")
            assert form.evaluate("element => element.checkValidity()") is False

            page.get_by_role("button", name="提交反馈").click()

            expect(page.get_by_role("status")).to_have_text("提交成功")
            expect(form).to_have_attribute("data-submission-count", "1")
            # 重新加载后初始状态恢复，故障开关依然能稳定复现。
            page.reload()
            expect(form).to_have_attribute("data-submission-count", "0")
            page.get_by_role("button", name="提交反馈").click()
            expect(form).to_have_attribute("data-submission-count", "1")
        finally:
            browser.close()
