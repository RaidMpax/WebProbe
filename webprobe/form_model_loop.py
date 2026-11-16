"""表单模型循环：模型提议动作，程序执行和验证，预算控制终止。

模型可提议疑似 Bug；程序要求两次真实提交证据后才放行报告。
离线测试用预写响应验证接线，不能代表真实模型自主表现。
"""

from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlparse

from openai import OpenAIError
from playwright.sync_api import Error as PlaywrightError

from webprobe.browser_tools import BrowserTools, PageObservation
from webprobe.form_loop import (
    FormAction, FormGoal, FormStepRecord, execute_form_action,
    form_completion_verified, has_first_required_bypass_evidence,
    verify_form_bug_report,
)
from webprobe.llm_decider import (
    FORM_ACTION_NAMES, Decision, ModelDecisionError, OpenAIDecider,
)
from webprobe.time_budget import TimeBudget


FormModelStatus = Literal[
    "completed", "suspected_bug", "needs_review", "execution_failed", "model_failed",
    "step_budget_exhausted", "model_call_budget_exhausted", "time_budget_exhausted",
]


def classify_model_error(error: Exception) -> str:
    """将模型调用失败分成可行动的类别，不保存或暴露密钥。"""
    message = str(error).lower()
    status_code = getattr(error, "status_code", None)
    if "timed out" in message or "timeout" in message:
        return "ModelTimeout"
    if status_code == 429 or "rate limit" in message or "resource_exhausted" in message:
        return "ModelRateLimit"
    if status_code == 400 and "location" in message and "not supported" in message:
        return "ProviderLocationUnavailable"
    if status_code is not None:
        return "ModelProviderError"
    if isinstance(error, ModelDecisionError):
        return "InvalidModelResponse"
    return "ModelCallError"


@dataclass(frozen=True)
class FormModelRunResult:
    """一次模型表单任务的状态、真实浏览器步骤与模型调用记录。"""

    status: FormModelStatus
    steps: list[FormStepRecord]
    model_calls: int
    decision_summaries: list[str]
    error: str | None = None
    bug_type: str | None = None
    reproduced_in_session: bool = False
    recoveries: list[str] = field(default_factory=list)
    error_type: str | None = None


def _correct_redundant_fill(
    action: FormAction, observation: PageObservation | None, goal: FormGoal,
) -> tuple[FormAction, str | None]:
    """若模型重复填写已符合目标的字段，就改做下一个有用动作。"""
    if (observation is None or observation.form_name_value is None
            or observation.form_message_value is None):
        return action, None
    name_matches = observation.form_name_value == goal.name
    message_matches = observation.form_message_value == goal.message
    if action == "fill_name" and name_matches:
        if not message_matches:
            return "fill_message", "重复填写姓名；观察值已正确，改为填写反馈内容。"
        return "submit", "两个输入值已符合目标；跳过重复填写并提交验证。"
    if action == "fill_message" and message_matches:
        if not name_matches:
            return "fill_name", "重复填写反馈内容；观察值已正确，改为填写姓名。"
        return "submit", "两个输入值已符合目标；跳过重复填写并提交验证。"
    return action, None


def _is_local_testlab_form(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "file" and parsed.path.replace("\\", "/").endswith(
        "/lab/feedback.html"
    )


def _recheck_required_bypass(
    tools: BrowserTools,
    goal: FormGoal,
    steps: list[FormStepRecord],
    observation: PageObservation | None,
    model_calls: int,
    summaries: list[str],
    recoveries: list[str],
    max_steps: int,
    time_budget: TimeBudget | None = None,
) -> FormModelRunResult | None:
    """只在本地 TestLab 有一次异常提交证据时，安全地再提交一次复现。"""
    if (not _is_local_testlab_form(goal.form_url)
            or not has_first_required_bypass_evidence(steps, goal)):
        return None
    if time_budget is not None and time_budget.exhausted:
        return FormModelRunResult(
            "time_budget_exhausted", steps, model_calls, summaries,
            recoveries=recoveries,
        )
    if observation is None or len(steps) >= max_steps:
        return FormModelRunResult(
            "needs_review", steps, model_calls, summaries, recoveries=recoveries,
        )
    try:
        if time_budget is not None:
            tools.set_timeout_for_budget(time_budget.remaining_seconds())
        repeated_observation = execute_form_action(tools, "submit", goal, observation)
    except (PlaywrightError, AssertionError, ValueError) as error:
        steps.append(FormStepRecord("submit", None, str(error), type(error).__name__))
        if time_budget is not None and time_budget.exhausted:
            return FormModelRunResult(
                "time_budget_exhausted", steps, model_calls, summaries,
                str(error), recoveries=recoveries,
            )
        return FormModelRunResult(
            "execution_failed", steps, model_calls, summaries, str(error),
            recoveries=recoveries,
        )
    steps.append(FormStepRecord("submit", repeated_observation))
    if time_budget is not None and time_budget.exhausted:
        return FormModelRunResult(
            "time_budget_exhausted", steps, model_calls, summaries,
            recoveries=recoveries,
        )
    recovery = "首次空必填提交显示成功；已在本地 TestLab 同一会话重提一次核验。"
    recoveries.append(recovery)
    summaries.append(f"系统复查：{recovery}")
    verified = verify_form_bug_report(steps, goal)
    if verified.status == "suspected_bug":
        return FormModelRunResult(
            "suspected_bug", steps, model_calls, summaries,
            bug_type=verified.bug_type, reproduced_in_session=True,
            recoveries=recoveries,
        )
    return FormModelRunResult(
        "needs_review", steps, model_calls, summaries, recoveries=recoveries,
    )


def run_form_model_loop(
    tools: BrowserTools, decider: OpenAIDecider, goal: FormGoal,
    max_steps: int, max_model_calls: int, max_seconds: float | None = None,
) -> FormModelRunResult:
    """每轮只执行一个经校验的动作；模型说 stop 后仍由程序核对规格。"""
    if max_steps < 1 or max_model_calls < 1:
        raise ValueError("两个预算都必须至少为 1")
    time_budget = TimeBudget(max_seconds) if max_seconds is not None else None
    decider.reset()
    steps: list[FormStepRecord] = []
    summaries: list[str] = []
    recoveries: list[str] = []
    performed: set[FormAction] = set()
    observation: PageObservation | None = None
    model_calls = 0

    while model_calls < max_model_calls:
        if time_budget is not None and time_budget.exhausted:
            return FormModelRunResult(
                "time_budget_exhausted", steps, model_calls, summaries,
                recoveries=recoveries,
            )
        try:
            decision = decider.choose(
                goal, observation, performed,
                timeout_seconds=(
                    time_budget.remaining_seconds() if time_budget is not None else None
                ),
            )
        except (ModelDecisionError, OpenAIError, ValueError) as error:
            if time_budget is not None and time_budget.exhausted:
                return FormModelRunResult(
                    "time_budget_exhausted", steps, model_calls + 1, summaries,
                    str(error), recoveries=recoveries,
                )
            return FormModelRunResult(
                "model_failed", steps, model_calls + 1, summaries, str(error),
                recoveries=recoveries, error_type=classify_model_error(error),
            )
        model_calls += 1
        summaries.append(decision.summary)
        if time_budget is not None and time_budget.exhausted:
            return FormModelRunResult(
                "time_budget_exhausted", steps, model_calls, summaries,
                recoveries=recoveries,
            )

        if decision.action not in FORM_ACTION_NAMES:
            return FormModelRunResult(
                "model_failed", steps, model_calls, summaries,
                f"表单任务不允许动作: {decision.action}",
                recoveries=recoveries, error_type="InvalidModelAction",
            )
        repaired_action, recovery = _correct_redundant_fill(
            decision.action, observation, goal,
        )
        if recovery is not None:
            recoveries.append(recovery)
            decision = Decision(repaired_action, decision.summary, decision.call_id)
        if decision.action == "stop":
            if form_completion_verified(steps, goal):
                return FormModelRunResult(
                    "completed", steps, model_calls, summaries, recoveries=recoveries,
                )
            recheck = _recheck_required_bypass(
                tools, goal, steps, observation, model_calls, summaries,
                recoveries, max_steps, time_budget,
            )
            return recheck or FormModelRunResult(
                "needs_review", steps, model_calls, summaries, recoveries=recoveries,
            )
        if decision.action == "halt":
            return FormModelRunResult(
                "needs_review", steps, model_calls, summaries, recoveries=recoveries,
            )
        if decision.action == "report_bug":
            verified = verify_form_bug_report(steps, goal)
            if verified.status == "suspected_bug":
                return FormModelRunResult(
                    "suspected_bug", steps, model_calls, summaries,
                    bug_type=verified.bug_type, reproduced_in_session=True,
                    recoveries=recoveries,
                )
            recheck = _recheck_required_bypass(
                tools, goal, steps, observation, model_calls, summaries,
                recoveries, max_steps, time_budget,
            )
            return recheck or FormModelRunResult(
                "needs_review", steps, model_calls, summaries, recoveries=recoveries,
            )
        if len(steps) >= max_steps:
            return FormModelRunResult(
                "step_budget_exhausted", steps, model_calls, summaries,
                recoveries=recoveries,
            )

        try:
            if time_budget is not None:
                tools.set_timeout_for_budget(time_budget.remaining_seconds())
            observation = execute_form_action(tools, decision.action, goal, observation)
        except (PlaywrightError, AssertionError, ValueError) as error:
            steps.append(FormStepRecord(
                decision.action, None, str(error), type(error).__name__,
            ))
            decider.record_tool_result(decision, None, str(error))
            if time_budget is not None and time_budget.exhausted:
                return FormModelRunResult(
                    "time_budget_exhausted", steps, model_calls, summaries,
                    str(error), recoveries=recoveries,
                )
            return FormModelRunResult(
                "execution_failed", steps, model_calls, summaries, str(error),
                recoveries=recoveries,
            )
        steps.append(FormStepRecord(decision.action, observation))
        performed.add(decision.action)
        decider.record_tool_result(decision, observation)
        if time_budget is not None and time_budget.exhausted:
            return FormModelRunResult(
                "time_budget_exhausted", steps, model_calls, summaries,
                recoveries=recoveries,
            )

    return FormModelRunResult(
        "model_call_budget_exhausted", steps, model_calls, summaries,
        recoveries=recoveries,
    )
