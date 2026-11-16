"""浏览器工具层：把 Playwright 动作变成“执行后返回页面观察”的接口。

阅读顺序：先看 PageObservation（返回什么），再看 BrowserTools（怎么操作）。
这里的工具只管“浏览器实际做了什么、看到了什么”；是否符合预期由上层决定。
"""

import math
from dataclasses import dataclass
from urllib.parse import urljoin

from playwright.sync_api import Page, expect


@dataclass(frozen=True)
class PageObservation:
    """一次页面快照，类似 C 语言中保存一组相关字段的 struct。

    @dataclass 会自动生成初始化方法，因此可以写 PageObservation(url=..., ...)。
    frozen=True 表示创建后不能改字段，避免后续动作意外改掉旧记录。
    `str | None` 表示“可能是字符串，也可能没有值”；没有相应元素时用 None。
    """

    url: str
    title: str
    heading: str | None
    status: str | None
    search_value: str | None
    # 只记录当前可见的搜索结果；详情页没有列表时为空元组。
    visible_products: tuple[str, ...] = ()
    # 下面五项只在 TestLab 反馈表单页有值；其他页面保持 None。
    form_name_value: str | None = None
    form_message_value: str | None = None
    form_state: str | None = None
    form_submission_count: str | None = None
    form_submit_label: str | None = None
    # 购物车页的结构化状态；其他页面上这些字段为空。
    cart_count: str | None = None
    cart_total: str | None = None
    cart_items: tuple[tuple[str, str], ...] = ()
    # 登录观察故意不保存密码字段，避免把凭据写入常规 JSON 轨迹。
    login_username: str | None = None
    login_state: str | None = None
    login_attempt_count: str | None = None
    login_submit_label: str | None = None


class SearchWaitTimeout(AssertionError):
    """TestLab 搜索未在等待预算内就绪；这本身不是网站 Bug。"""


class BrowserTools:
    """持有一个浏览器标签页，并限制本练习可访问的页面 URL。

    self 类似“当前对象”：每个方法都通过 self._page 操作同一个标签页。
    前导下划线表示这些字段约定由类内部使用，不是严格的访问控制。
    """

    def __init__(self, page: Page, allowed_urls: set[str]) -> None:
        # Page 是 Playwright 的一个浏览器标签页；调用方负责创建和关闭浏览器。
        self._page = page
        # frozenset 是不可修改的集合：运行中不能意外增添允许访问的地址。
        self._allowed_urls = frozenset(allowed_urls)

    def _check_url(self, url: str) -> None:
        # 这里是精确 URL 白名单，只覆盖当前本地练习，不是通用网络隔离。
        if url not in self._allowed_urls:
            raise ValueError(f"不允许访问的 URL: {url}")

    def set_timeout_for_budget(self, remaining_seconds: float) -> None:
        """将下一次浏览器动作的 Playwright 超时压到剩余预算内。"""
        if not math.isfinite(remaining_seconds) or remaining_seconds <= 0:
            raise ValueError("剩余浏览器时间必须大于 0")
        timeout_ms = max(1, min(8_000, math.ceil(remaining_seconds * 1_000)))
        self._page.set_default_timeout(timeout_ms)
        self._page.set_default_navigation_timeout(timeout_ms)

    def observe(self) -> PageObservation:
        """读取页面现在的样子，不判断它是否符合产品预期。

        Locator（定位器）描述要找的元素。count() 先判断元素是否存在；
        存在才读 first 的文本或输入值，否则返回 None，避免直接读取时报错。
        """
        self._check_url(self._page.url)
        heading = self._page.get_by_role("heading", level=1)
        status = self._page.get_by_role("status")
        searchbox = self._page.get_by_role("searchbox")
        visible_products = self._page.locator("#results li:not([hidden])")
        form = self._page.locator("#feedback-form")
        cart = self._page.locator("#cart-items")
        login_form = self._page.locator("#login-form")
        submit_button = form.locator('button[type="submit"]')
        name_field = self._page.get_by_role("textbox", name="姓名（必填）")
        message_field = self._page.get_by_role("textbox", name="反馈内容（必填）")
        form_present = form.count() > 0
        return PageObservation(
            url=self._page.url,
            title=self._page.title(),
            heading=heading.first.inner_text() if heading.count() else None,
            status=status.first.inner_text() if status.count() else None,
            search_value=searchbox.first.input_value() if searchbox.count() else None,
            visible_products=tuple(visible_products.all_text_contents()),
            form_name_value=name_field.input_value() if form_present and name_field.count() else None,
            form_message_value=(
                message_field.input_value() if form_present and message_field.count() else None
            ),
            form_state=form.get_attribute("data-form-state") if form_present else None,
            form_submission_count=(
                form.get_attribute("data-submission-count") if form_present else None
            ),
            form_submit_label=(
                submit_button.inner_text().strip()
                if form_present and submit_button.count() else None
            ),
            cart_count=(
                self._page.locator("#cart-count").inner_text()
                if cart.count() else None
            ),
            cart_total=(
                self._page.locator("#cart-total").inner_text()
                if cart.count() else None
            ),
            cart_items=tuple(
                (
                    item.get_attribute("data-product") or "",
                    item.get_attribute("data-quantity") or "",
                )
                for item in cart.locator("li").all()
            ),
            login_username=(
                self._page.locator("#username").input_value()
                if login_form.count() else None
            ),
            login_state=(
                login_form.get_attribute("data-login-state") if login_form.count() else None
            ),
            login_attempt_count=(
                login_form.get_attribute("data-attempt-count") if login_form.count() else None
            ),
            login_submit_label=(
                login_form.locator('button[type="submit"]').inner_text().strip()
                if login_form.count() and login_form.locator('button[type="submit"]').count()
                else None
            ),
        )

    def open(self, url: str) -> PageObservation:
        """先检查地址，再打开页面，最后返回打开后的观察。"""
        self._check_url(url)
        self._page.goto(url)
        return self.observe()

    def fill(self, role: str, name: str, value: str) -> PageObservation:
        """按角色和可访问名称找输入框，搜索框和表单 textbox 共用此方法。"""
        self._check_url(self._page.url)
        self._page.get_by_role(role, name=name).fill(value)
        return self.observe()

    def click(self, role: str, name: str) -> PageObservation:
        """点击指定元素；链接点击前额外检查 href 指向哪里。"""
        self._check_url(self._page.url)
        target = self._page.get_by_role(role, name=name)
        if role == "link":
            # 普通链接的目标可以在点击前检查，避免直接打开未列入白名单的页面。
            href = target.get_attribute("href")
            if href is None:
                raise ValueError("只允许点击有 href 的链接")
            self._check_url(urljoin(self._page.url, href))
        target.click()
        return self.observe()

    def back(self) -> PageObservation:
        """调用浏览器的返回功能，然后读取返回后的页面。"""
        self._check_url(self._page.url)
        self._page.go_back()
        return self.observe()

    def wait_for_status(self, expected: str, timeout_ms: int) -> PageObservation:
        """等待页面状态达到预期，timeout_ms 的单位是毫秒。

        expect(...).to_have_text(...) 会反复检查直到成功或超时；
        它比固定 sleep 几秒更适合动态加载页面。
        """
        self._check_url(self._page.url)
        expect(self._page.get_by_role("status")).to_have_text(
            expected, timeout=timeout_ms
        )
        return self.observe()

    def wait_for_search_ready(self, timeout_ms: int) -> PageObservation:
        """等 TestLab 搜索结束，再读取结果；不预先要求结果正确。"""
        self._check_url(self._page.url)
        try:
            expect(self._page.locator("#results")).to_have_attribute(
                "data-search-state", "ready", timeout=timeout_ms
            )
        except AssertionError as error:
            raise SearchWaitTimeout(f"搜索在 {timeout_ms} 毫秒内未就绪") from error
        return self.observe()
