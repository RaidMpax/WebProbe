"""登录 TestLab 的账号规则、注入故障和按钮文案变化校准。"""

from pathlib import Path

from playwright.sync_api import sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOGIN_URL = (PROJECT_ROOT / "lab" / "login.html").as_uri()


def test_correct_test_credentials_log_in_and_wrong_password_is_rejected() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(LOGIN_URL)
            page.get_by_label("账号").fill("qa_student")
            page.get_by_label("密码").fill("qa_demo_123")
            page.get_by_role("button", name="登录").click()
            assert page.get_by_role("status").inner_text() == "登录成功：欢迎 QA 测试用户"
            assert page.locator("#login-form").get_attribute("data-login-state") == "authenticated"

            page.reload()
            page.get_by_label("账号").fill("qa_student")
            page.get_by_label("密码").fill("wrong-password")
            page.get_by_role("button", name="登录").click()
            assert page.get_by_role("status").inner_text() == "账号或密码错误"
            assert page.locator("#login-form").get_attribute("data-login-state") == "rejected"
        finally:
            browser.close()


def test_login_failure_is_injected_only_in_its_browser_context() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            failed_context = browser.new_context()
            failed_context.add_init_script("window.__WEBPROBE_VALID_LOGIN_REJECTED__ = true")
            failed = failed_context.new_page()
            failed.goto(LOGIN_URL)
            failed.get_by_label("账号").fill("qa_student")
            failed.get_by_label("密码").fill("qa_demo_123")
            failed.get_by_role("button", name="登录").click()
            assert failed.get_by_role("status").inner_text() == "账号或密码错误"

            normal = browser.new_page()
            normal.goto(LOGIN_URL)
            normal.get_by_label("账号").fill("qa_student")
            normal.get_by_label("密码").fill("qa_demo_123")
            normal.get_by_role("button", name="登录").click()
            assert normal.get_by_role("status").inner_text() == "登录成功：欢迎 QA 测试用户"
            failed_context.close()
        finally:
            browser.close()


def test_changed_login_button_text_is_a_normal_page_variant() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            context.add_init_script("window.__WEBPROBE_RENAME_LOGIN_BUTTON__ = true")
            page = context.new_page()
            page.goto(LOGIN_URL)
            assert page.get_by_role("button", name="进入账户").count() == 1
            page.get_by_label("账号").fill("qa_student")
            page.get_by_label("密码").fill("qa_demo_123")
            page.get_by_role("button", name="进入账户").click()
            assert page.get_by_role("status").inner_text() == "登录成功：欢迎 QA 测试用户"
            context.close()
        finally:
            browser.close()
