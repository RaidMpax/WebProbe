"""反馈表单的有限规则循环：观察、选动作、执行，再用页面证据验证。

这是人写的 if 规则，不是 LLM 决策。阅读顺序：数据类 → choose_form_action
→ execute_form_action → run_form_loop。Ground Truth 的 has_bug 不传给循环。
"""

from dataclasses import dataclass
from typing import Literal

from playwright.sync_api import Error as PlaywrightError

from webprobe.browser_tools import BrowserTools, PageObservation
from webprobe.time_budget import TimeBudget


FormAction = Literal["open", "fill_name", "fill_message", "submit", "stop", "halt", "report_bug"]
FormRunStatus = Literal[
    "completed", "suspected_bug", "budget_exhausted", "time_budget_exhausted",
    "needs_review", "execution_failed",
]


@dataclass(frozen=True)
class FormGoal:
    """只包含任务输入与正确产品规格，不包含故障开关或 Bug 标签。"""

    form_url: str
    name: str
    message: str
    expected_submitted: bool
    expected_status: str
    task: str = ""


@dataclass(frozen=True)
class FormStepRecord:
    """一次实际浏览器动作及其后观察；失败时 observation 为 None。"""

    action: FormAction
    observation: PageObservation | None
    error: str | None = None
    error_type: str | None = None


@dataclass(frozen=True)
class FormRunResult:
    """循环的最终状态；疑似 Bug 需要两次成功提交空表单的证据。"""

    status: FormRunStatus
    steps: list[FormStepRecord]
    bug_type: str | None = None
    reproduced_in_session: bool = False


def _matches_spec(observation: PageObservation, goal: FormGoal) -> bool:
    """同时核对可见提示、输入值和提交次数，避免只看“提交成功”。"""
    if (observation.form_name_value != goal.name
            or observation.form_message_value != goal.message
            or observation.status != goal.expected_status):
        return False
    if goal.expected_submitted:
        return observation.form_state == "submitted" and observation.form_submission_count == "1"
    return observation.form_state == "invalid" and observation.form_submission_count == "0"


def form_completion_verified(
    records: list[FormStepRecord], goal: FormGoal,
) -> bool:
    """结束前只从最近一次真实提交的页面观察核对产品规格。"""
    last_submit = next(
        (step.observation for step in reversed(records) if step.action == "submit"), None,
    )
    return (
        last_submit is not None
        and last_submit.url == goal.form_url
        and _matches_spec(last_submit, goal)
    )


def _looks_like_bypass(
    observation: PageObservation, goal: FormGoal, submit_count: int,
) -> bool:
    """只把“空必填项被成功提交”视为这一个明确的疑似故障。"""
    return (
        not goal.expected_submitted
        and (not goal.name.strip() or not goal.message.strip())
        and observation.url == goal.form_url
        and observation.form_name_value == goal.name
        and observation.form_message_value == goal.message
        and observation.status == "提交成功"
        and observation.form_state == "submitted"
        and observation.form_submission_count == str(submit_count)
    )


def choose_form_action(
    observation: PageObservation | None, performed: list[FormAction], goal: FormGoal,
) -> FormAction:
    """只选下一步，不操作浏览器；第二次相同错配后才建议报告。"""
    if observation is None:
        return "open"
    if observation.url != goal.form_url:
        return "halt"
    if (observation.form_name_value is None or observation.form_message_value is None
            or not observation.form_submit_label):
        return "halt"

    submit_count = performed.count("submit")
    if submit_count:
        if _matches_spec(observation, goal):
            return "stop"
        if _looks_like_bypass(observation, goal, submit_count):
            return "report_bug" if submit_count >= 2 else "submit"
        return "halt"

    if observation.form_name_value != goal.name:
        return "halt" if "fill_name" in performed else "fill_name"
    if observation.form_message_value != goal.message:
        return "halt" if "fill_message" in performed else "fill_message"
    return "submit"


def execute_form_action(
    tools: BrowserTools, action: FormAction, goal: FormGoal,
    observation: PageObservation | None,
) -> PageObservation:
    """把动作名交给通用浏览器工具，返回执行后的真实页面快照。"""
    if action == "open":
        return tools.open(goal.form_url)
    if action == "fill_name":
        return tools.fill("textbox", "姓名（必填）", goal.name)
    if action == "fill_message":
        return tools.fill("textbox", "反馈内容（必填）", goal.message)
    if action == "submit":
        if observation is None or not observation.form_submit_label:
            raise ValueError("当前页面没有可定位的提交按钮")
        return tools.click("button", observation.form_submit_label)
    raise ValueError(f"不是浏览器动作: {action}")


def verify_form_bug_report(records: list[FormStepRecord], goal: FormGoal) -> FormRunResult:
    """规则或模型提议报告时，独立复核两次真实提交记录。"""
    submits = [step.observation for step in records if step.action == "submit"]
    if (len(submits) != 2
            or submits[0] is None or submits[1] is None
            or not _looks_like_bypass(submits[0], goal, 1)
            or not _looks_like_bypass(submits[1], goal, 2)):
        return FormRunResult("needs_review", records)
    return FormRunResult(
        "suspected_bug", records,
        bug_type="required_field_bypass", reproduced_in_session=True,
    )


def has_first_required_bypass_evidence(
    records: list[FormStepRecord], goal: FormGoal,
) -> bool:
    """判断是否有一次可复查的空必填项提交异常成功证据。"""
    submits = [step.observation for step in records if step.action == "submit"]
    return (
        len(submits) == 1
        and submits[0] is not None
        and _looks_like_bypass(submits[0], goal, submit_count=1)
    )


def run_form_loop(
    tools: BrowserTools, goal: FormGoal, max_steps: int,
    recheck_suspected_bug: bool = True, max_seconds: float | None = None,
) -> FormRunResult:
    """一步一观察；Baseline 关闭复查时，疑点只进入待审，不报告 Bug。"""
    if max_steps < 1:
        raise ValueError("max_steps 必须至少为 1")
    records: list[FormStepRecord] = []
    time_budget = TimeBudget(max_seconds) if max_seconds is not None else None
    performed: list[FormAction] = []
    observation: PageObservation | None = None

    while True:
        if time_budget is not None and time_budget.exhausted:
            return FormRunResult("time_budget_exhausted", records)
        action = choose_form_action(observation, performed, goal)
        if time_budget is not None and time_budget.exhausted:
            return FormRunResult("time_budget_exhausted", records)
        if action == "stop":
            return FormRunResult("completed", records)
        if action == "halt":
            return FormRunResult("needs_review", records)
        if action == "report_bug":
            return verify_form_bug_report(records, goal)
        if action == "submit" and performed.count("submit") == 1 and not recheck_suspected_bug:
            return FormRunResult("needs_review", records)
        if len(records) >= max_steps:
            return FormRunResult("budget_exhausted", records)
        try:
            if time_budget is not None:
                tools.set_timeout_for_budget(time_budget.remaining_seconds())
            observation = execute_form_action(tools, action, goal, observation)
            records.append(FormStepRecord(action, observation))
            if time_budget is not None and time_budget.exhausted:
                return FormRunResult("time_budget_exhausted", records)
        except (PlaywrightError, AssertionError, ValueError) as error:
            records.append(FormStepRecord(action, None, str(error), type(error).__name__))
            if time_budget is not None and time_budget.exhausted:
                return FormRunResult("time_budget_exhausted", records)
            return FormRunResult("execution_failed", records)
        performed.append(action)
