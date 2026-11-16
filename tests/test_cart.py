"""购物车 TestLab 的固定规格和预设总价故障。"""

from pathlib import Path

from playwright.sync_api import sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CART_URL = (PROJECT_ROOT / "lab" / "cart.html").as_uri()


def test_cart_starts_empty_and_reload_resets_state() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(CART_URL)
            assert page.get_by_role("status").inner_text() == "购物车为空"
            assert page.locator("#cart-count").inner_text() == "0"
            assert page.locator("#cart-total").inner_text() == "0"
            assert page.locator("#cart-items li").count() == 0

            page.get_by_role("button", name="加入购物车：蓝色茶杯").click()
            page.reload()
            assert page.get_by_role("status").inner_text() == "购物车为空"
            assert page.locator("#cart-count").inner_text() == "0"
            assert page.locator("#cart-total").inner_text() == "0"
        finally:
            browser.close()


def test_cart_total_matches_fixed_product_prices() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(CART_URL)
            page.get_by_role("button", name="加入购物车：蓝色茶杯").click()
            page.get_by_role("button", name="加入购物车：帆布包").click()
            assert page.locator("#cart-count").inner_text() == "2"
            assert page.locator("#cart-total").inner_text() == "98"
            assert page.locator('#cart-items li[data-product="蓝色茶杯"]').get_attribute(
                "data-quantity"
            ) == "1"
            assert page.locator('#cart-items li[data-product="帆布包"]').get_attribute(
                "data-quantity"
            ) == "1"
        finally:
            browser.close()


def test_preconfigured_total_bug_is_deterministic_and_isolated() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            bug_context = browser.new_context()
            bug_context.add_init_script("window.__WEBPROBE_CART_TOTAL_BUG__ = true")
            bug_page = bug_context.new_page()
            bug_page.goto(CART_URL)
            bug_page.get_by_role("button", name="加入购物车：蓝色茶杯").click()
            bug_page.get_by_role("button", name="加入购物车：帆布包").click()
            assert bug_page.locator("#cart-count").inner_text() == "2"
            assert bug_page.locator("#cart-total").inner_text() == "78"
            assert bug_page.get_by_role("status").inner_text() == "购物车共 2 件商品"

            normal_page = browser.new_page()
            normal_page.goto(CART_URL)
            normal_page.get_by_role("button", name="加入购物车：蓝色茶杯").click()
            normal_page.get_by_role("button", name="加入购物车：帆布包").click()
            assert normal_page.locator("#cart-total").inner_text() == "98"
            bug_context.close()
        finally:
            browser.close()
