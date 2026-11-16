"""LLM-driven cart loop with deterministic limits and a Python-owned recheck."""

from dataclasses import dataclass, field
from typing import Literal

from openai import OpenAIError
from playwright.sync_api import Error as PlaywrightError

from webprobe.browser_tools import BrowserTools, PageObservation
from webprobe.cart_loop import CartAction, CartGoal, CartStepRecord
from webprobe.llm_decider import (
    CART_ACTION_NAMES, ModelDecisionError, OpenAIDecider,
)
from webprobe.time_budget import TimeBudget


CartModelStatus = Literal[
    "completed", "suspected_bug", "needs_review", "execution_failed",
    "model_failed", "step_budget_exhausted", "model_call_budget_exhausted",
    "time_budget_exhausted",
]
PRODUCT_ACTIONS = {"add_cup": "蓝色茶杯", "add_bag": "帆布包"}


@dataclass(frozen=True)
class CartModelRunResult:
    status: CartModelStatus
    steps: list[CartStepRecord]
    model_calls: int
    decision_summaries: list[str]
    error: str | None = None
    bug_type: str | None = None
    reproduced_in_session: bool = False
    recoveries: list[str] = field(default_factory=list)
    error_type: str | None = None


def _expected_items(goal: CartGoal) -> tuple[tuple[str, str], ...]:
    return tuple((name, str(quantity)) for name, quantity in goal.items)


def _items_match(observation: PageObservation, goal: CartGoal) -> bool:
    return (
        observation.url == goal.cart_url
        and observation.cart_count == str(goal.expected_count)
        and observation.cart_items == _expected_items(goal)
    )


def _cart_matches(observation: PageObservation, goal: CartGoal) -> bool:
    return _items_match(observation, goal) and observation.cart_total == str(goal.expected_total)


def _wrong_total_evidence(observation: PageObservation, goal: CartGoal) -> bool:
    return (
        _items_match(observation, goal)
        and observation.cart_total is not None
        and observation.cart_total != str(goal.expected_total)
    )


def _classify_model_error(error: Exception) -> str:
    message = str(error).lower()
    status_code = getattr(error, "status_code", None)
    if "timeout" in message or "timed out" in message:
        return "request_timeout"
    if status_code == 429 or "rate limit" in message or "resource_exhausted" in message:
        return "rate_limit"
    if isinstance(error, ModelDecisionError):
        return "invalid_model_response"
    if isinstance(error, OpenAIError):
        return "provider_error"
    return "invalid_decision"


def _record_action(
    tools: BrowserTools, action: CartAction, goal: CartGoal,
) -> PageObservation:
    if action == "open":
        return tools.open(goal.cart_url)
    product = PRODUCT_ACTIONS.get(action)
    if product is None:
        raise ValueError(f"不是可执行的购物车动作: {action}")
    return tools.click("button", f"加入购物车：{product}")


def _recheck_total_mismatch(
    tools: BrowserTools, goal: CartGoal, steps: list[CartStepRecord],
    max_steps: int, budget: TimeBudget | None, recoveries: list[str],
) -> tuple[CartModelStatus | None, PageObservation | None, str | None]:
    """Restart the local cart once and replay the requested products exactly."""
    if not steps or steps[-1].observation is None:
        return "needs_review", None, None
    first = steps[-1].observation
    if not _wrong_total_evidence(first, goal):
        return "needs_review", first, None
    replay_actions: list[tuple[CartAction, str | None]] = [("open", None)]
    replay_actions.extend(
        ("add_cup" if name == "蓝色茶杯" else "add_bag", name)
        for name, quantity in goal.items
        for _ in range(quantity)
    )
    observation: PageObservation | None = None
    for action, product in replay_actions:
        if budget is not None and budget.exhausted:
            return "time_budget_exhausted", observation, None
        if len(steps) >= max_steps:
            return "step_budget_exhausted", observation, None
        try:
            if budget is not None:
                tools.set_timeout_for_budget(budget.remaining_seconds())
            observation = _record_action(tools, action, goal)
        except (PlaywrightError, AssertionError, ValueError) as error:
            steps.append(CartStepRecord(action, product, None, str(error), type(error).__name__))
            if budget is not None and budget.exhausted:
                return "time_budget_exhausted", None, str(error)
            return "execution_failed", None, str(error)
        steps.append(CartStepRecord(action, product, observation))
        if budget is not None and budget.exhausted:
            return "time_budget_exhausted", observation, None
    if observation is None or not _wrong_total_evidence(observation, goal):
        recoveries.append("购物车疑似错价已重开页面复查，但第二次没有复现相同错价。")
        return "needs_review", observation, None
    recoveries.append("购物车错价已在重开页面、重新添加相同商品后再次观察到。")
    return "suspected_bug", observation, None


def run_cart_model_loop(
    tools: BrowserTools, decider: OpenAIDecider, goal: CartGoal,
    max_steps: int, max_model_calls: int, max_seconds: float | None = None,
) -> CartModelRunResult:
    """Let the model choose each cart action; Python verifies, limits and rechecks."""
    if max_steps < 1 or max_model_calls < 1:
        raise ValueError("两个预算都必须至少为 1")
    budget = TimeBudget(max_seconds) if max_seconds is not None else None
    decider.reset()
    steps: list[CartStepRecord] = []
    summaries: list[str] = []
    recoveries: list[str] = []
    performed: set[CartAction] = set()
    observation: PageObservation | None = None
    model_calls = 0
    added_counts = {"蓝色茶杯": 0, "帆布包": 0}
    required_counts = dict(goal.items)

    while model_calls < max_model_calls:
        if budget is not None and budget.exhausted:
            return CartModelRunResult(
                "time_budget_exhausted", steps, model_calls, summaries,
                recoveries=recoveries,
            )
        try:
            decision = decider.choose(
                goal, observation, performed,
                timeout_seconds=budget.remaining_seconds() if budget else None,
            )
        except (ModelDecisionError, OpenAIError, ValueError) as error:
            if budget is not None and budget.exhausted:
                return CartModelRunResult(
                    "time_budget_exhausted", steps, model_calls + 1, summaries,
                    str(error), recoveries=recoveries,
                )
            return CartModelRunResult(
                "model_failed", steps, model_calls + 1, summaries, str(error),
                recoveries=recoveries, error_type=_classify_model_error(error),
            )
        model_calls += 1
        summaries.append(decision.summary)
        if budget is not None and budget.exhausted:
            return CartModelRunResult(
                "time_budget_exhausted", steps, model_calls, summaries,
                recoveries=recoveries,
            )
        if decision.action not in CART_ACTION_NAMES:
            return CartModelRunResult(
                "model_failed", steps, model_calls, summaries,
                f"购物车任务不允许动作: {decision.action}",
                recoveries=recoveries, error_type="invalid_model_action",
            )
        action: CartAction = decision.action
        if action == "stop":
            if observation is not None and _cart_matches(observation, goal):
                return CartModelRunResult(
                    "completed", steps, model_calls, summaries,
                    recoveries=recoveries,
                )
            if observation is not None and _wrong_total_evidence(observation, goal):
                status, checked, error = _recheck_total_mismatch(
                    tools, goal, steps, max_steps, budget, recoveries,
                )
                if status == "suspected_bug":
                    return CartModelRunResult(
                        status, steps, model_calls, summaries, error,
                        bug_type="wrong_cart_total", reproduced_in_session=True,
                        recoveries=recoveries,
                    )
                return CartModelRunResult(
                    status or "needs_review", steps, model_calls, summaries,
                    error, recoveries=recoveries,
                )
            return CartModelRunResult(
                "needs_review", steps, model_calls, summaries,
                recoveries=recoveries,
            )
        if action == "halt":
            return CartModelRunResult(
                "needs_review", steps, model_calls, summaries,
                recoveries=recoveries,
            )
        if action == "report_bug":
            if observation is not None and _cart_matches(observation, goal):
                summaries.append(
                    "系统校验：模型提出报告故障，但商品、数量和总价均符合规格；已拒绝误报。"
                )
                return CartModelRunResult(
                    "completed", steps, model_calls, summaries,
                    recoveries=recoveries,
                )
            status, checked, error = _recheck_total_mismatch(
                tools, goal, steps, max_steps, budget, recoveries,
            )
            if status == "suspected_bug":
                return CartModelRunResult(
                    status, steps, model_calls, summaries, error,
                    bug_type="wrong_cart_total", reproduced_in_session=True,
                    recoveries=recoveries,
                )
            return CartModelRunResult(
                status or "needs_review", steps, model_calls, summaries,
                error, recoveries=recoveries,
            )
        if len(steps) >= max_steps:
            return CartModelRunResult(
                "step_budget_exhausted", steps, model_calls, summaries,
                recoveries=recoveries,
            )
        if action in PRODUCT_ACTIONS and added_counts[PRODUCT_ACTIONS[action]] >= required_counts.get(
            PRODUCT_ACTIONS[action], 0
        ):
            error = f"任务不需要继续添加商品：{PRODUCT_ACTIONS[action]}"
            decider.record_tool_result(decision, observation, error)
            summaries.append(f"系统校验：{error}")
            continue
        try:
            if budget is not None:
                tools.set_timeout_for_budget(budget.remaining_seconds())
            observation = _record_action(tools, action, goal)
            product = PRODUCT_ACTIONS.get(action)
            steps.append(CartStepRecord(action, product, observation))
            if action == "open":
                added_counts = {"蓝色茶杯": 0, "帆布包": 0}
            elif product is not None:
                added_counts[product] += 1
            decider.record_tool_result(decision, observation)
            performed.add(action)
        except (PlaywrightError, AssertionError, ValueError) as error:
            steps.append(CartStepRecord(action, PRODUCT_ACTIONS.get(action), None, str(error), type(error).__name__))
            decider.record_tool_result(decision, None, str(error))
            if budget is not None and budget.exhausted:
                return CartModelRunResult(
                    "time_budget_exhausted", steps, model_calls, summaries,
                    str(error), recoveries=recoveries,
                )
            return CartModelRunResult(
                "execution_failed", steps, model_calls, summaries,
                str(error), recoveries=recoveries,
            )
        if budget is not None and budget.exhausted:
            return CartModelRunResult(
                "time_budget_exhausted", steps, model_calls, summaries,
                recoveries=recoveries,
            )

    return CartModelRunResult(
        "model_call_budget_exhausted", steps, model_calls, summaries,
        recoveries=recoveries,
    )
