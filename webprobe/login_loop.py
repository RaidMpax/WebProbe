"""本地登录流程 Agent：填写凭据、核对结果，疑似拒绝时只复查一次。"""

from dataclasses import dataclass
from typing import Literal

from playwright.sync_api import Error as PlaywrightError

from webprobe.browser_tools import BrowserTools, PageObservation
from webprobe.time_budget import TimeBudget


LoginAction = Literal["open", "fill_username", "fill_password", "submit"]
LoginStatus = Literal[
    "completed", "suspected_bug", "needs_review", "budget_exhausted",
    "time_budget_exhausted", "execution_failed",
]


@dataclass(frozen=True)
class LoginGoal:
    """目标状态与测试凭据；不包括数据集故障标签。"""

    login_url: str
    username: str
    password: str
    expected_authenticated: bool
    expected_status: str
    task: str = ""


@dataclass(frozen=True)
class LoginStepRecord:
    action: LoginAction
    observation: PageObservation | None
    error: str | None = None
    error_type: str | None = None


@dataclass(frozen=True)
class LoginRunResult:
    status: LoginStatus
    steps: list[LoginStepRecord]
    bug_type: str | None = None
    reproduced_in_session: bool = False


def _matches_spec(observation: PageObservation, goal: LoginGoal) -> bool:
    return (
        observation.url == goal.login_url
        and observation.login_username == goal.username
        and observation.login_state == ("authenticated" if goal.expected_authenticated else "rejected")
        and observation.status == goal.expected_status
        and observation.login_attempt_count == "1"
    )


def _is_valid_login_rejected(observation: PageObservation, goal: LoginGoal) -> bool:
    return (
        goal.expected_authenticated
        and observation.url == goal.login_url
        and observation.login_username == goal.username
        and observation.login_state == "rejected"
        and observation.status == "账号或密码错误"
    )


def run_login_loop(
    tools: BrowserTools, goal: LoginGoal, max_steps: int,
    recheck_suspected_bug: bool = True, max_seconds: float | None = None,
) -> LoginRunResult:
    """同一任务最多执行两轮；第二次有效凭据仍被拒绝才报告疑似 Bug。"""
    if max_steps < 1:
        raise ValueError("max_steps 必须至少为 1")
    records: list[LoginStepRecord] = []
    budget = TimeBudget(max_seconds) if max_seconds is not None else None
    rounds = 2 if recheck_suspected_bug and goal.expected_authenticated else 1
    for attempt in range(rounds):
        if budget is not None and budget.exhausted:
            return LoginRunResult("time_budget_exhausted", records)
        current_action: LoginAction = "open"
        try:
            if len(records) >= max_steps:
                return LoginRunResult("budget_exhausted", records)
            if budget is not None:
                tools.set_timeout_for_budget(budget.remaining_seconds())
            observation = tools.open(goal.login_url)
            records.append(LoginStepRecord("open", observation))
            if budget is not None and budget.exhausted:
                return LoginRunResult("time_budget_exhausted", records)
            for action, role, name, value in (
                ("fill_username", "textbox", "账号", goal.username),
                ("fill_password", "textbox", "密码", goal.password),
            ):
                current_action = action
                if len(records) >= max_steps:
                    return LoginRunResult("budget_exhausted", records)
                if budget is not None and budget.exhausted:
                    return LoginRunResult("time_budget_exhausted", records)
                if budget is not None:
                    tools.set_timeout_for_budget(budget.remaining_seconds())
                observation = tools.fill(role, name, value)
                records.append(LoginStepRecord(action, observation))
                if budget is not None and budget.exhausted:
                    return LoginRunResult("time_budget_exhausted", records)
            label = observation.login_submit_label
            if not label:
                return LoginRunResult("needs_review", records)
            current_action = "submit"
            if len(records) >= max_steps:
                return LoginRunResult("budget_exhausted", records)
            if budget is not None and budget.exhausted:
                return LoginRunResult("time_budget_exhausted", records)
            if budget is not None:
                tools.set_timeout_for_budget(budget.remaining_seconds())
            observation = tools.click("button", label)
            records.append(LoginStepRecord("submit", observation))
            if budget is not None and budget.exhausted:
                return LoginRunResult("time_budget_exhausted", records)
        except (PlaywrightError, AssertionError, ValueError) as error:
            records.append(LoginStepRecord(
                current_action, None, str(error), type(error).__name__,
            ))
            if budget is not None and budget.exhausted:
                return LoginRunResult("time_budget_exhausted", records)
            return LoginRunResult("execution_failed", records)

        if _matches_spec(observation, goal):
            return LoginRunResult("completed", records)
        if not _is_valid_login_rejected(observation, goal):
            return LoginRunResult("needs_review", records)
        if attempt == 0 and rounds == 2:
            continue
        if attempt == 1:
            return LoginRunResult(
                "suspected_bug", records,
                bug_type="valid_login_rejected", reproduced_in_session=True,
            )
        return LoginRunResult("needs_review", records)
    return LoginRunResult("needs_review", records)
