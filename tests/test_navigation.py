"""观察实际页面状态，并根据预先写好的规格验证导航。"""

import json
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def observe(page: Page, step: str) -> dict[str, str]:
    """只记录浏览器可见的事实，不在这里判断通过或失败。"""
    return {
        "step": step,
        "url": page.url,
        "title": page.title(),
        "heading": page.get_by_role("heading", level=1).inner_text(),
        "status": page.get_by_role("status").inner_text(),
    }


def test_open_detail_and_return_to_search() -> None:
    search_url = (PROJECT_ROOT / "lab" / "search.html").as_uri()
    detail_url = (PROJECT_ROOT / "lab" / "cup.html").as_uri()
    output = PROJECT_ROOT / "artifacts" / "navigation.json"
    output.parent.mkdir(exist_ok=True)
    observations: list[dict[str, str]] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(search_url)
            observations.append(observe(page, "搜索页"))

            page.get_by_role("link", name="蓝色茶杯").click()
            expect(page).to_have_url(detail_url)
            observations.append(observe(page, "商品详情页"))
            expect(page).to_have_title("蓝色茶杯 - 商品详情")
            expect(page.get_by_role("heading", name="蓝色茶杯")).to_be_visible()
            expect(page.get_by_text("价格：¥39")).to_be_visible()
            expect(page.get_by_role("status")).to_have_text("库存：有货", timeout=5_000)
            observations.append(observe(page, "库存加载完成"))
            page.screenshot(path=str(output.parent / "detail-loaded.png"))

            page.go_back()
            expect(page).to_have_url(search_url)
            observations.append(observe(page, "返回搜索页"))
            expect(page.get_by_role("searchbox", name="商品关键词")).to_be_visible()
        finally:
            output.write_text(
                json.dumps(observations, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            browser.close()
