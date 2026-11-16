"""表单 Evals 的独立评分：单题证据、整套指标、同版本对照。"""

from typing import Literal

from webprobe.browser_tools import PageObservation
from webprobe.form_dataset import FORM_DATASET, FormCase
from webprobe.form_loop import FormRunResult, FormStepRecord


POLICY_NAME = "form_rule_v2_time_budget"
BASELINE_POLICY_NAME = "form_rule_baseline_v2_time_budget"
MODEL_POLICY_NAME = "form_model_loop_v4_time_budget_recovery_rate_limited"
FormPolicy = Literal["rule_baseline", "rule", "model"]
POLICY_NAMES = {
    "rule_baseline": BASELINE_POLICY_NAME, "rule": POLICY_NAME,
    "model": MODEL_POLICY_NAME,
}


def score_form_case(
    case: FormCase, run: FormRunResult | None, elapsed_seconds: float,
    evidence_steps: list[FormStepRecord] | None = None,
) -> tuple[str | None, bool | None, bool]:
    """返回 (Agent 结论, 独立表单断言, 任务成功)。

    断言只读取真实提交后的页面快照，不相信 Agent 自报的完成状态。
    没有提交快照时保留 None，不能擅自算成“无 Bug”。
    """
    steps = evidence_steps if evidence_steps is not None else (run.steps if run else [])
    submits: list[PageObservation] = [
        step.observation for step in steps
        if step.action == "submit" and step.observation is not None
    ]
    assertion_passed: bool | None = None
    if submits:
        last = submits[-1]
        assertion_passed = (
            last.form_name_value == case.name
            and last.form_message_value == case.message
            and last.status == case.expected_status
            and last.form_state == ("submitted" if case.expected_submitted else "invalid")
            and last.form_submission_count == ("1" if case.expected_submitted else "0")
        )

    conclusion = None
    if run is not None and run.status == "completed":
        conclusion = "no_bug"
    elif run is not None and run.status == "suspected_bug" and run.bug_type:
        conclusion = "bug"

    if case.has_bug:
        # 复现由评测器再查一次步骤证据，不能只相信循环写的布尔值。
        reproduced_from_steps = (
            len(submits) == 2
            and all(
                observation.form_name_value == case.name
                and observation.form_message_value == case.message
                and observation.status == "提交成功"
                and observation.form_state == "submitted"
                and observation.form_submission_count == str(index)
                for index, observation in enumerate(submits, 1)
            )
        )
        success = (
            conclusion == "bug" and run is not None
            and run.bug_type == case.bug_type
            and run.reproduced_in_session
            and reproduced_from_steps
            and assertion_passed is False
        )
    else:
        success = conclusion == "no_bug" and assertion_passed is True
    return conclusion, assertion_passed, success and elapsed_seconds <= case.max_seconds


def summarize_form_results(results: list[dict[str, object]]) -> dict[str, object]:
    """混淆矩阵按 Case 统计；未完成的 Case 单独计数。"""
    if not results:
        raise ValueError("没有可汇总的评测结果")
    policy_name = results[0]["policy"]
    if any(result["policy"] != policy_name for result in results):
        raise ValueError("不能混合不同表单策略的 Case")
    model_name = results[0].get("model")
    if any(result.get("model") != model_name for result in results):
        raise ValueError("不能混合不同模型的 Case")
    classified = [result for result in results if result["agent_conclusion"] is not None]
    tp = sum(r["ground_truth"]["has_bug"] and r["agent_conclusion"] == "bug" for r in classified)
    fp = sum(not r["ground_truth"]["has_bug"] and r["agent_conclusion"] == "bug" for r in classified)
    fn = sum(r["ground_truth"]["has_bug"] and r["agent_conclusion"] == "no_bug" for r in classified)
    tn = sum(not r["ground_truth"]["has_bug"] and r["agent_conclusion"] == "no_bug" for r in classified)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall is not None and precision + recall else None
    usages = [result.get("token_usage") for result in results]
    token_fields = ("input_tokens", "output_tokens", "total_tokens")
    covered_usage = [
        usage for usage in usages
        if isinstance(usage, dict) and all(
            type(usage.get(name)) is int and usage[name] >= 0 for name in token_fields
        )
    ]
    token_usage = None
    if policy_name == MODEL_POLICY_NAME and len(covered_usage) == len(results):
        token_usage = {
            name: sum(usage[name] for usage in covered_usage)
            for name in token_fields
        }
    recovery_cases = [
        result for result in results
        if isinstance(result.get("recoveries"), list) and result["recoveries"]
    ]
    recovered_cases = sum(result["task_success"] is True for result in recovery_cases)
    return {
        "policy": policy_name,
        "model": model_name,
        "dataset": FORM_DATASET.name,
        "classification_unit": "one_case",
        "case_count": len(results),
        "task_success_count": sum(r["task_success"] is True for r in results),
        "task_success_rate": sum(r["task_success"] is True for r in results) / len(results),
        "time_budget_exceeded_count": sum(
            r.get("time_budget_exceeded") is True for r in results
        ),
        "agent_execution_failure_count": sum(r["run_status"] == "execution_failed" for r in results),
        "model_failure_count": sum(
            r["run_status"] == "model_failed" or r.get("model_error_type") is not None
            for r in results
        ),
        "model_failure_breakdown": {
            error_type: sum(
                r.get("model_error_type") == error_type
                for r in results
            )
            for error_type in sorted({
                r.get("model_error_type") for r in results
                if r.get("model_error_type")
            })
        },
        "unclassified_case_count": len(results) - len(classified),
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "bug_precision": precision,
        "bug_recall": recall,
        "bug_f1": f1,
        "in_session_bug_reproduction_rate": sum(
            r["ground_truth"]["has_bug"] and r["agent_conclusion"] == "bug"
            and r["reproduced_in_session"] is True for r in classified
        ) / tp if tp else None,
        "recovery_attempt_case_count": len(recovery_cases),
        "recovered_case_count": recovered_cases,
        "recovery_rate": recovered_cases / len(recovery_cases) if recovery_cases else None,
        "average_steps": sum(len(r["steps"]) for r in results) / len(results),
        "average_latency_seconds": sum(r["elapsed_seconds"] for r in results) / len(results),
        "token_usage": token_usage,
        "token_usage_covered_cases": len(covered_usage),
        "api_cost": None,
        "metrics_note": "本地表单固定集；未形成结论不计入混淆矩阵，超时另行统计。模型服务错误数可与时间预算超限数重叠。疑似 Bug 须有两次提交证据。Recovery Rate = 至少记录一次显式恢复且最终成功的 Case / 有恢复记录的 Case。Token 用量仅在每题完整时汇总；费用未计算。",
    }


def compare_form_summaries(
    baseline: dict[str, object], current: dict[str, object],
) -> dict[str, object]:
    """只比较同一数据集、同一源码版本的一对结果。"""
    if baseline["policy"] != BASELINE_POLICY_NAME or current["policy"] != POLICY_NAME:
        raise ValueError("必须按 Baseline → 当前策略的顺序比较")
    for field in ("dataset", "dataset_sha256", "policy_source_sha256", "case_count"):
        if baseline[field] != current[field]:
            raise ValueError(f"对照实验的 {field} 不一致")
    return {
        "dataset": current["dataset"],
        "dataset_sha256": current["dataset_sha256"],
        "policy_source_sha256": current["policy_source_sha256"],
        "baseline_policy": baseline["policy"],
        "current_policy": current["policy"],
        "task_success_rate": {
            "before": baseline["task_success_rate"],
            "after": current["task_success_rate"],
        },
        "model_failure_count": {
            "before": baseline.get("model_failure_count"),
            "after": current.get("model_failure_count"),
        },
        "recovery_attempt_case_count": {
            "before": baseline.get("recovery_attempt_case_count", 0),
            "after": current.get("recovery_attempt_case_count", 0),
        },
        "recovered_case_count": {
            "before": baseline.get("recovered_case_count", 0),
            "after": current.get("recovered_case_count", 0),
        },
        "recovery_rate": {
            "before": baseline.get("recovery_rate"),
            "after": current.get("recovery_rate"),
        },
        "unclassified_case_count": {
            "before": baseline["unclassified_case_count"],
            "after": current["unclassified_case_count"],
        },
        "confusion": {"before": baseline["confusion"], "after": current["confusion"]},
        "average_steps": {"before": baseline["average_steps"], "after": current["average_steps"]},
        "average_latency_seconds": {
            "before": baseline["average_latency_seconds"],
            "after": current["average_latency_seconds"],
        },
        "note": "只比较同一版本的本地表单固定集；Baseline 的待审结果不是漏报分类。",
    }
