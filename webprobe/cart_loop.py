"""购物车的有限 Observe → Act → Verify 循环，含一次安全复查。"""

from dataclasses import dataclass
from typing import Literal

from playwright.sync_api import Error as PlaywrightError

from webprobe.browser_tools import BrowserTools, PageObservation
from webprobe.time_budget import TimeBudget


CartRunStatus = Literal[
    "completed", "suspected_bug", "needs_review", "budget_exhausted",
    "time_budget_exhausted", "execution_failed",
]
CartAction = Literal[
    "open", "add_product", "add_cup", "add_bag", "report_bug", "stop", "halt",
]


@dataclass(frozen=True)
class CartGoal:
    """只放任务目标和固定产品规格，不把故障标签交给循环。"""

    cart_url: str
    items: tuple[tuple[str, int], ...]
    expected_count: int
    expected_total: int
    task: str = ""


@dataclass(frozen=True)
class CartStepRecord:
    action: CartAction
    target: str | None
    observation: PageObservation | None
    error: str | None = None
    error_type: str | None = None


@dataclass(frozen=True)
class CartRunResult:
    status: CartRunStatus
    steps: list[CartStepRecord]
    bug_type: str | None = None
    reproduced_in_session: bool = False


def _matches_cart(observation: PageObservation, goal: CartGoal) -> bool:
    expected_items = tuple((name, str(quantity)) for name, quantity in goal.items)
    return (
        observation.url == goal.cart_url
        and observation.cart_count == str(goal.expected_count)
        and observation.cart_total == str(goal.expected_total)
        and observation.cart_items == expected_items
    )


def _is_repeatable_total_mismatch(observation: PageObservation, goal: CartGoal) -> bool:
    """只把商品和件数正确、总价不同的现象作为可复查的总价疑点。"""
    expected_items = tuple((name, str(quantity)) for name, quantity in goal.items)
    return (
        observation.url == goal.cart_url
        and observation.cart_count == str(goal.expected_count)
        and observation.cart_items == expected_items
        and observation.cart_total is not None
        and observation.cart_total != str(goal.expected_total)
    )


def run_cart_loop(
    tools: BrowserTools, goal: CartGoal, max_steps: int,
    recheck_suspected_bug: bool = True, max_seconds: float | None = None,
) -> CartRunResult:
    """每轮重新打开页面以恢复干净状态；只有相同错价重复出现才报告 Bug。"""
    if max_steps < 1:
        raise ValueError("max_steps 必须至少为 1")
    records: list[CartStepRecord] = []
    budget = TimeBudget(max_seconds) if max_seconds is not None else None
    last_observation: PageObservation | None = None
    mismatch_repeated = False

    for attempt in range(2 if recheck_suspected_bug else 1):
        actions: list[tuple[CartAction, str | None]] = [("open", None)]
        actions.extend(
            ("add_product", name)
            for name, quantity in goal.items
            for _ in range(quantity)
        )
        for action, target in actions:
            if budget is not None and budget.exhausted:
                return CartRunResult("time_budget_exhausted", records)
            if len(records) >= max_steps:
                return CartRunResult("budget_exhausted", records)
            try:
                if budget is not None:
                    tools.set_timeout_for_budget(budget.remaining_seconds())
                if action == "open":
                    last_observation = tools.open(goal.cart_url)
                else:
                    assert target is not None
                    last_observation = tools.click("button", f"加入购物车：{target}")
                records.append(CartStepRecord(action, target, last_observation))
            except (PlaywrightError, AssertionError, ValueError) as error:
                records.append(CartStepRecord(
                    action, target, None, str(error), type(error).__name__,
                ))
                return CartRunResult("execution_failed", records)

        assert last_observation is not None
        if _matches_cart(last_observation, goal):
            return CartRunResult("completed", records)
        if not _is_repeatable_total_mismatch(last_observation, goal):
            return CartRunResult("needs_review", records)
        if attempt == 0 and recheck_suspected_bug:
            continue
        mismatch_repeated = attempt == 1
        break

    if mismatch_repeated:
        return CartRunResult(
            "suspected_bug", records,
            bug_type="wrong_cart_total", reproduced_in_session=True,
        )
    # Baseline observes one mismatch but deliberately does not convert it into a Bug.
    return CartRunResult("needs_review", records)
