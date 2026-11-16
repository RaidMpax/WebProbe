"""B2：用固定规则演示观察、决策、操作和步骤预算。

先读三个数据类，再按 choose_action → execute_action → run_demo 的顺序读。
本文件的决策规则由人写在 if 分支里；它还不是 LLM Agent。
"""

from dataclasses import dataclass
from typing import Literal

from playwright.sync_api import Error as PlaywrightError

from webprobe.browser_tools import BrowserTools, PageObservation
from webprobe.time_budget import TimeBudget


# Literal 是类型提示：Action 只能取列出的字符串。Python 运行时不会自动强制校验。
# stop = 目标完成；halt = 证据不足；report_bug = 两次搜索观察均不符合规格。
Action = Literal["open", "fill", "submit", "wait_search", "detail", "wait_stock", "stop", "halt", "report_bug"]
RunStatus = Literal[
    "completed", "suspected_bug", "budget_exhausted", "time_budget_exhausted",
    "needs_review", "execution_failed",
]


@dataclass(frozen=True)
class DemoGoal:
    """这一个演示任务的目标和预期；目前由测试代码明确给出。

    例如 keyword 是要输入的词，search_status 是期望网页显示的结果文本。
    预期来自测试规格，不能在运行中照抄网页实际显示的内容。
    """

    search_url: str
    detail_url: str
    keyword: str
    product_name: str
    search_status: str
    stock_status: str
    search_wait_ms: int = 2_500


@dataclass(frozen=True)
class StepRecord:
    """一条动作记录：做了什么、做完看到了什么、是否报错。

    工具失败时 observation 为 None，error 保存错误消息。
    `error: str | None = None` 的等号给这个字段设置默认值。
    """

    action: Action
    observation: PageObservation | None
    error: str | None = None
    error_type: str | None = None


@dataclass(frozen=True)
class RunResult:
    """一次循环的状态、步骤，以及有证据时的疑似 Bug 类型。

    steps 是列表，按执行顺序保存 StepRecord，之后可写成 JSON 供复查。
    """

    status: RunStatus
    steps: list[StepRecord]
    bug_type: str | None = None
    reproduced_in_session: bool = False


def choose_action(
    observation: PageObservation | None, performed: list[Action], goal: DemoGoal
) -> Action:
    """只根据目标、当前观察和已执行动作做决定，不操作浏览器。

    observation=None 代表还没有打开页面。performed 是已执行动作列表，
    保留重复次数，才能判断搜索是否已经复查过一次。
    """
    if observation is None:
        return "open"
    if observation.url == goal.search_url:
        # 当前还在搜索页：先确认关键词，再确认搜索结果，最后进入详情页。
        if observation.search_value != goal.keyword:
            # 同一个动作做过仍未达到预期，就停下来检查，避免重复点击或填写。
            return "halt" if "fill" in performed else "fill"
        if observation.status == "搜索中…":
            # 一次提交最多等一次；等待失败会作为工具失败记录，不上报网站 Bug。
            return "wait_search" if performed.count("wait_search") < performed.count("submit") else "halt"
        if observation.status != goal.search_status:
            return "halt" if "submit" in performed else "submit"
        if "submit" not in performed:
            return "submit"
        if observation.visible_products != (goal.product_name,):
            # 第一次不匹配时再提交一次；第二次仍不匹配才提议报告疑似 Bug。
            return "report_bug" if performed.count("submit") >= 2 else "submit"
        return "detail"
    if observation.url == goal.detail_url:
        # 当前已到详情页：先核对商品名，再处理可能仍在加载的库存。
        if observation.heading != goal.product_name:
            return "halt"
        if observation.status != goal.stock_status:
            return "halt" if "wait_stock" in performed else "wait_stock"
        return "stop"
    return "halt"


def execute_action(
    tools: BrowserTools, action: Action, goal: DemoGoal,
    remaining_seconds: float | None = None,
) -> PageObservation:
    """把动作名称翻译为具体的浏览器工具调用，并返回新的页面观察。

    例如 action='fill' 时，不让决策器直接碰 Playwright，而是调用
    BrowserTools.fill(...)。这样“选动作”和“执行动作”分开了。
    """
    if action == "open":
        return tools.open(goal.search_url)
    if action == "fill":
        return tools.fill("searchbox", "商品关键词", goal.keyword)
    if action == "submit":
        return tools.click("button", "搜索")
    if action == "wait_search":
        timeout_ms = goal.search_wait_ms
        if remaining_seconds is not None:
            timeout_ms = min(timeout_ms, max(1, int(remaining_seconds * 1_000)))
        return tools.wait_for_search_ready(timeout_ms=timeout_ms)
    if action == "detail":
        return tools.click("link", goal.product_name)
    if action == "wait_stock":
        timeout_ms = 5_000
        if remaining_seconds is not None:
            timeout_ms = min(timeout_ms, max(1, int(remaining_seconds * 1_000)))
        return tools.wait_for_status(goal.stock_status, timeout_ms=timeout_ms)
    raise ValueError(f"不是浏览器动作: {action}")


def _bug_result(records: list[StepRecord], goal: DemoGoal) -> RunResult:
    """报告前再次核对两条真实提交记录，避免把定位失败当作 Bug。"""
    mismatches = [
        step.observation for step in records
        if step.action == "submit"
        and step.observation is not None
        and step.observation.url == goal.search_url
        and step.observation.search_value == goal.keyword
        and step.observation.status == goal.search_status
        and step.observation.visible_products != (goal.product_name,)
    ]
    if len(mismatches) < 2:
        return RunResult("needs_review", records)
    return RunResult(
        "suspected_bug", records,
        bug_type="search_result_mismatch", reproduced_in_session=True,
    )


def run_demo(
    tools: BrowserTools, goal: DemoGoal, max_steps: int,
    max_seconds: float | None = None,
) -> RunResult:
    """每轮选择并执行一个动作；max_steps 只统计实际浏览器动作。

    状态流转：旧 observation → choose_action → execute_action → 新 observation。
    stop/halt/report_bug 是结束决策，不消耗浏览器动作步数。
    """
    if max_steps < 1:
        raise ValueError("max_steps 必须至少为 1")
    records: list[StepRecord] = []
    time_budget = TimeBudget(max_seconds) if max_seconds is not None else None
    performed: list[Action] = []
    # 首轮还没访问网页，所以没有页面观察；第一次决策会选择 open。
    observation: PageObservation | None = None

    while len(records) < max_steps:
        if time_budget is not None and time_budget.exhausted:
            return RunResult("time_budget_exhausted", records)
        # 决策使用上一轮的观察；动作完成后会得到下一轮的观察。
        action = choose_action(observation, performed, goal)
        if time_budget is not None and time_budget.exhausted:
            return RunResult("time_budget_exhausted", records)
        if action == "stop":
            return RunResult("completed", records)
        if action == "halt":
            return RunResult("needs_review", records)
        if action == "report_bug":
            return _bug_result(records, goal)
        try:
            remaining = time_budget.remaining_seconds() if time_budget is not None else None
            if remaining is not None:
                tools.set_timeout_for_budget(remaining)
            observation = execute_action(tools, action, goal, remaining)
            records.append(StepRecord(action, observation))
            if time_budget is not None and time_budget.exhausted:
                return RunResult("time_budget_exhausted", records)
        except (PlaywrightError, AssertionError, ValueError) as error:
            # try/except 类似错误处理分支：把可预见的工具错误记入记录并停止。
            # 操作失败先归为执行失败，不能直接报告网站存在 Bug。
            records.append(StepRecord(action, None, str(error), type(error).__name__))
            if time_budget is not None and time_budget.exhausted:
                return RunResult("time_budget_exhausted", records)
            return RunResult("execution_failed", records)
        performed.append(action)

    # 最后一个动作可能刚好完成目标，因此耗尽预算前再判断一次。
    if time_budget is not None and time_budget.exhausted:
        return RunResult("time_budget_exhausted", records)
    final_action = choose_action(observation, performed, goal)
    if time_budget is not None and time_budget.exhausted:
        return RunResult("time_budget_exhausted", records)
    if final_action == "stop":
        return RunResult("completed", records)
    if final_action == "halt":
        return RunResult("needs_review", records)
    if final_action == "report_bug":
        return _bug_result(records, goal)
    return RunResult("budget_exhausted", records)
