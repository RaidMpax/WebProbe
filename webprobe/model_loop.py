"""B4：有限的模型决策循环；这里只处理本地搜索演示任务。"""

from dataclasses import dataclass, field
from typing import Literal

from openai import APIConnectionError, APITimeoutError, OpenAIError, RateLimitError
from playwright.sync_api import Error as PlaywrightError

from webprobe.browser_tools import BrowserTools, PageObservation
from webprobe.demo_loop import Action, DemoGoal, StepRecord, execute_action
from webprobe.llm_decider import ModelDecisionError, OpenAIDecider
from webprobe.time_budget import TimeBudget


ModelRunStatus = Literal[
    "completed", "suspected_bug", "needs_review", "execution_failed", "model_failed",
    "step_budget_exhausted", "model_call_budget_exhausted", "time_budget_exhausted",
]


@dataclass(frozen=True)
class ModelRunResult:
    """程序最终状态、浏览器动作记录，以及实际模型调用次数。"""

    status: ModelRunStatus
    steps: list[StepRecord]
    model_calls: int
    decision_summaries: list[str]
    error: str | None = None
    bug_type: str | None = None
    reproduced_in_session: bool = False
    recoveries: list[str] = field(default_factory=list)
    model_error_type: str | None = None


def _classify_model_error(error: Exception) -> str:
    """Keep provider timeouts distinct from invalid model tool responses."""
    if isinstance(error, APITimeoutError):
        return "request_timeout"
    if isinstance(error, RateLimitError):
        return "rate_limit"
    if isinstance(error, APIConnectionError):
        return "connection_error"
    if isinstance(error, ModelDecisionError):
        return "invalid_model_response"
    if isinstance(error, OpenAIError):
        return "provider_error"
    return "invalid_decision"


def _goal_verified(goal: DemoGoal, observation: PageObservation | None,
                   steps: list[StepRecord]) -> bool:
    """模型说 stop 时，由程序用固定规格核对已观察到的证据。"""
    # reversed 从最新步骤往前找；旧的正确结果不能掩盖后来一次错误搜索。
    # next(..., None) 表示没有提交记录时返回 None，而不是抛出异常。
    search_observation = next(
        (step.observation for step in reversed(steps)
         if step.action in ("submit", "wait_search")),
        None,
    )
    search_verified = (
        search_observation is not None
        and search_observation.url == goal.search_url
        and search_observation.search_value == goal.keyword
        and search_observation.status == goal.search_status
        and search_observation.visible_products == (goal.product_name,)
    )
    return (
        search_verified
        and observation is not None
        and observation.url == goal.detail_url
        and observation.heading == goal.product_name
        and observation.status == goal.stock_status
    )


def _verified_search_bug_report(
    goal: DemoGoal, steps: list[StepRecord],
) -> tuple[bool, str, bool]:
    """只接受同会话最近两次完整搜索都偏离固定商品规格的报告。"""
    submissions: list[PageObservation | None] = []
    for index, step in enumerate(steps):
        if step.action != "submit":
            continue
        result = step.observation
        if result is not None and result.status == "搜索中…":
            # 将提交后的显式等待结果归给该次搜索；报错或下一次提交前仍未完成则留作证据不足。
            for following in steps[index + 1:]:
                if following.action == "submit":
                    break
                if following.action == "wait_search":
                    result = following.observation
                    break
        submissions.append(result)

    if not submissions:
        return False, "还没有搜索提交；不能据此报告网站故障。", False
    if len(submissions) == 1:
        return False, "只观察到一次搜索提交；请再次提交同一关键词复现后再报告。", True
    recent = submissions[-2:]
    if any(result is None for result in recent):
        return False, "最近两次搜索中有一次没有得到浏览器结果；不能据此报告网站故障。", False
    for result in recent:
        assert result is not None
        if result.url != goal.search_url or result.search_value != goal.keyword:
            return False, "最近搜索的页面或关键词与测试目标不一致；请先恢复正确搜索状态。", False
        if result.status != goal.search_status:
            return False, "搜索尚未到达预期完成状态；加载中或异常状态不能作为 Bug 证据。", False
        if result.visible_products == (goal.product_name,):
            return False, "最近两次搜索并非都违反固定商品规格；暂不报告 Bug。", False
    return True, "", False


def run_model_loop(
    tools: BrowserTools, decider: OpenAIDecider, goal: DemoGoal,
    max_steps: int, max_model_calls: int, max_seconds: float | None = None,
) -> ModelRunResult:
    """模型选动作，工具执行，结果交回模型；两种预算都限制循环。

    max_steps 只数浏览器动作；stop/halt 也需要一次模型调用。
    即使模型选择 stop，也必须通过 _goal_verified 才算任务完成。
    """
    if max_steps < 1 or max_model_calls < 1:
        raise ValueError("两个预算都必须至少为 1")
    time_budget = TimeBudget(max_seconds) if max_seconds is not None else None
    decider.reset()
    steps: list[StepRecord] = []
    summaries: list[str] = []
    # set 用来记录做过哪些动作；本批模型可以参考它，但程序仍独立核对结果。
    performed: set[Action] = set()
    observation: PageObservation | None = None
    model_calls = 0
    recoveries: list[str] = []

    def finish(
        status: ModelRunStatus, *, error: str | None = None,
        bug_type: str | None = None, reproduced_in_session: bool = False,
        calls: int | None = None, model_error_type: str | None = None,
    ) -> ModelRunResult:
        """在所有退出路径保留已发生的受控恢复记录。"""
        return ModelRunResult(
            status, steps, model_calls if calls is None else calls, summaries,
            error, bug_type, reproduced_in_session, list(recoveries), model_error_type,
        )

    while model_calls < max_model_calls:
        if time_budget is not None and time_budget.exhausted:
            return finish("time_budget_exhausted")
        # choose 只提出动作，不会点击页面。异常归为模型/接口失败。
        try:
            decision = decider.choose(
                goal, observation, performed,
                timeout_seconds=(
                    time_budget.remaining_seconds() if time_budget is not None else None
                ),
            )
        except (ModelDecisionError, OpenAIError, ValueError) as error:
            error_type = _classify_model_error(error)
            if time_budget is not None and time_budget.exhausted:
                return finish(
                    "time_budget_exhausted", calls=model_calls + 1, error=str(error),
                    model_error_type=error_type,
                )
            return finish(
                "model_failed", calls=model_calls + 1, error=str(error),
                model_error_type=error_type,
            )
        model_calls += 1
        summaries.append(decision.summary)
        if time_budget is not None and time_budget.exhausted:
            return finish("time_budget_exhausted")

        if decision.action == "stop":
            status: ModelRunStatus = (
                "completed" if _goal_verified(goal, observation, steps) else "needs_review"
            )
            return finish(status)
        if decision.action == "halt":
            return finish("needs_review")
        if decision.action == "report_bug":
            verified, reason, needs_reproduction = _verified_search_bug_report(goal, steps)
            if verified:
                return finish(
                    "suspected_bug",
                    bug_type="search_result_mismatch", reproduced_in_session=True,
                )
            # 让模型看到拒绝原因并有机会继续复现；该控制动作不计浏览器步骤。
            recoveries.append(f"拒绝疑似 Bug 报告：{reason}")
            # 搜索是无副作用的读取操作；若模型只查过一次且页面仍处于正确搜索状态，
            # 由程序安全地重复同一关键词一次，避免再花一个模型请求来决定“重新搜索”。
            can_repeat_search = (
                needs_reproduction
                and observation is not None
                and observation.url == goal.search_url
                and observation.search_value == goal.keyword
                and observation.status == goal.search_status
                and len(steps) < max_steps
            )
            if can_repeat_search:
                remaining = time_budget.remaining_seconds() if time_budget is not None else None
                if remaining is not None and remaining <= 0:
                    return finish("time_budget_exhausted")
                try:
                    if remaining is not None:
                        tools.set_timeout_for_budget(remaining)
                    observation = execute_action(tools, "submit", goal, remaining)
                except (PlaywrightError, AssertionError, ValueError) as error:
                    steps.append(StepRecord(
                        "submit", None, str(error), type(error).__name__,
                    ))
                    if time_budget is not None and time_budget.exhausted:
                        return finish("time_budget_exhausted", error=str(error))
                    return finish("execution_failed", error=str(error))

                steps.append(StepRecord("submit", observation))
                performed.add("submit")
                verified, retry_reason, _ = _verified_search_bug_report(goal, steps)
                if verified:
                    recoveries.append("程序自动重复同一关键词搜索；两次完整结果均违反固定规格。")
                    return finish(
                        "suspected_bug",
                        bug_type="search_result_mismatch", reproduced_in_session=True,
                    )
                reason = f"{reason} 自动复查后仍未确认：{retry_reason}"
                recoveries.append(f"搜索复现未确认：{retry_reason}")
                decider.record_tool_result(decision, observation, reason)
                continue
            decider.record_tool_result(decision, None, reason)
            continue
        if len(steps) >= max_steps:
            # 已无浏览器动作额度，不能让下一条动作越过预算。
            return finish("step_budget_exhausted")

        try:
            remaining = time_budget.remaining_seconds() if time_budget is not None else None
            if remaining is not None:
                tools.set_timeout_for_budget(remaining)
            observation = execute_action(tools, decision.action, goal, remaining)
        except (PlaywrightError, AssertionError, ValueError) as error:
            steps.append(StepRecord(
                decision.action, None, str(error), type(error).__name__
            ))
            decider.record_tool_result(decision, None, str(error))
            if time_budget is not None and time_budget.exhausted:
                return finish("time_budget_exhausted", error=str(error))
            return finish("execution_failed", error=str(error))
        steps.append(StepRecord(decision.action, observation))
        performed.add(decision.action)
        # 这里传的是工具执行后的观察，不是模型在 summary 里写的描述。
        decider.record_tool_result(decision, observation)
        if time_budget is not None and time_budget.exhausted:
            return finish("time_budget_exhausted")

    return finish("model_call_budget_exhausted")
