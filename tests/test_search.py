"""固定规格驱动的第一条浏览器测试；这里没有 LLM 判定。"""

from pathlib import Path

from playwright.sync_api import expect, sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_SEARCH_PRODUCTS = ["蓝色茶杯"]


def search_page_url(bug: str | None = None) -> str:
    """返回正常页面，或带指定故障开关的同一测试页面地址。"""
    url = (PROJECT_ROOT / "lab" / "search.html").as_uri()
    return f"{url}?bug={bug}" if bug else url


def test_search_finds_only_matching_product() -> None:
    screenshot = PROJECT_ROOT / "artifacts" / "search-success.png"
    screenshot.parent.mkdir(exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(search_page_url())

            page.get_by_role("searchbox", name="商品关键词").fill("茶杯")
            page.get_by_role("button", name="搜索").click()

            expect(page.get_by_role("status")).to_have_text("找到 1 件商品")
            expect(page.get_by_role("listitem")).to_have_count(1)
            visible_products = page.locator("#results li:not([hidden])").all_text_contents()
            assert visible_products == EXPECTED_SEARCH_PRODUCTS
            page.screenshot(path=str(screenshot))
        finally:
            browser.close()


def test_search_mismatch_bug_is_switchable_and_breaks_fixed_expectation() -> None:
    """确认故障开关会产生已知错误，且不能只看“找到 1 件”就判定通过。"""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(search_page_url("search_mismatch"))
            page.get_by_role("searchbox", name="商品关键词").fill("茶杯")
            page.get_by_role("button", name="搜索").click()

            # Ground Truth 仍是固定规格中的“蓝色茶杯”；不会从页面结果生成。
            actual_products = page.locator("#results li:not([hidden])").all_text_contents()
            assert EXPECTED_SEARCH_PRODUCTS == ["蓝色茶杯"]
            assert actual_products == ["帆布包"]  # 注入开关的确定性故障表现
            assert actual_products != EXPECTED_SEARCH_PRODUCTS
            # 状态数量碰巧正确，商品内容却错；只断言数量会漏掉这个 Bug。
            expect(page.get_by_role("status")).to_have_text("找到 1 件商品")
        finally:
            browser.close()


def test_slow_search_is_loading_before_it_returns_correct_products() -> None:
    """慢加载是正常环境状态，不能把短暂的空列表当成搜索失败。"""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(search_page_url() + "?delay_ms=1200")
            page.get_by_role("searchbox", name="商品关键词").fill("茶杯")
            page.get_by_role("button", name="搜索").click()
            expect(page.get_by_role("status")).to_have_text("搜索中…")
            assert page.locator("#results li:not([hidden])").all_text_contents() == []
            expect(page.locator("#results")).to_have_attribute(
                "data-search-state", "ready", timeout=2_500
            )
            assert page.locator("#results li:not([hidden])").all_text_contents() == EXPECTED_SEARCH_PRODUCTS
        finally:
            browser.close()


def test_three_second_search_eventually_returns_correct_products() -> None:
    """独立核对四案数据集的 Ground Truth：三秒延迟并非网站故障。"""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(search_page_url() + "?delay_ms=3000")
            page.get_by_role("searchbox", name="商品关键词").fill("茶杯")
            page.get_by_role("button", name="搜索").click()
            expect(page.locator("#results")).to_have_attribute(
                "data-search-state", "ready", timeout=4_000
            )
            expect(page.get_by_role("status")).to_have_text("找到 1 件商品")
            assert page.locator("#results li:not([hidden])").all_text_contents() == EXPECTED_SEARCH_PRODUCTS
        finally:
            browser.close()
