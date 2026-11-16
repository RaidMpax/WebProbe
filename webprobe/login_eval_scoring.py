"""登录 Evals：核对最终提交证据，并分开统计未完成题。"""

from typing import Literal

from webprobe.login_dataset import LOGIN_DATASET, LoginCase
from webprobe.login_loop import LoginRunResult, LoginStepRecord


BASELINE_POLICY_NAME = "login_rule_baseline_no_recheck_v1"
POLICY_NAME = "login_rule_recheck_v1"
LoginPolicy = Literal["baseline", "rule"]
POLICY_NAMES = {"baseline": BASELINE_POLICY_NAME, "rule": POLICY_NAME}


def score_login_case(
    case: LoginCase, run: LoginRunResult | None, elapsed_seconds: float,
    steps: list[LoginStepRecord],
) -> tuple[str | None, bool | None, bool]:
    """只从真实 submit 快照计算断言；没有提交证据时结果为 None。"""
    submits = [
        step.observation for step in steps
        if step.action == "submit" and step.observation is not None
    ]
    assertion: bool | None = None
    if submits:
        latest = submits[-1]
        expected_state = "authenticated" if case.expected_authenticated else "rejected"
        assertion = (
            latest.login_username == case.username
            and latest.login_state == expected_state
            and latest.status == case.expected_status
            and latest.login_attempt_count == "1"
        )
    conclusion = None
    if run is not None and run.status == "completed":
        conclusion = "no_bug"
    elif run is not None and run.status == "suspected_bug":
        conclusion = "bug"

    reproduced = (
        len(submits) == 2
        and all(
            item.login_username == case.username
            and item.login_state == "rejected"
            and item.status == "账号或密码错误"
            and item.login_attempt_count == "1"
            for item in submits
        )
    )
    if case.has_bug:
        success = (
            conclusion == "bug" and run is not None
            and run.bug_type == case.bug_type
            and run.reproduced_in_session is True
            and reproduced and assertion is False
        )
    else:
        success = conclusion == "no_bug" and assertion is True
    return conclusion, assertion, bool(success and elapsed_seconds <= case.max_seconds)


def summarize_login_results(results: list[dict[str, object]]) -> dict[str, object]:
    """混淆矩阵以一条 Case 为单位；未完成题不擅自算作无 Bug。"""
    if not results:
        raise ValueError("没有可汇总的登录评测结果")
    classified = [row for row in results if row["agent_conclusion"] is not None]
    tp = sum(row["ground_truth"]["has_bug"] and row["agent_conclusion"] == "bug" for row in classified)
    fp = sum(not row["ground_truth"]["has_bug"] and row["agent_conclusion"] == "bug" for row in classified)
    fn = sum(row["ground_truth"]["has_bug"] and row["agent_conclusion"] == "no_bug" for row in classified)
    tn = sum(not row["ground_truth"]["has_bug"] and row["agent_conclusion"] == "no_bug" for row in classified)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall is not None and precision + recall else None
    recovery_cases = [
        row for row in results
        if sum(step["action"] == "open" for step in row["steps"]) > 1
    ]
    recovered = sum(row["task_success"] is True for row in recovery_cases)
    return {
        "policy": results[0]["policy"], "dataset": LOGIN_DATASET.name,
        "classification_unit": "one_case", "case_count": len(results),
        "task_success_count": sum(row["task_success"] is True for row in results),
        "task_success_rate": sum(row["task_success"] is True for row in results) / len(results),
        "unclassified_case_count": len(results) - len(classified),
        "agent_execution_failure_count": sum(row["run_status"] == "execution_failed" for row in results),
        "model_failure_count": 0,
        "time_budget_exceeded_count": sum(row["time_budget_exceeded"] for row in results),
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "bug_precision": precision, "bug_recall": recall, "bug_f1": f1,
        "bug_reproduction_count": sum(
            row["ground_truth"]["has_bug"] and row["reproduced_in_session"]
            for row in results
        ),
        "recovery_attempt_case_count": len(recovery_cases),
        "recovered_case_count": recovered,
        "recovery_rate": recovered / len(recovery_cases) if recovery_cases else None,
        "average_steps": sum(len(row["steps"]) for row in results) / len(results),
        "average_latency_seconds": sum(row["elapsed_seconds"] for row in results) / len(results),
        "metrics_note": "按 Case 统计；未形成结论的题保持未分类，不进入 TP/FP/FN/TN。登录故障须在两个独立页面初始状态下重复提交正确测试凭据并观察到相同拒绝结果，才记录为复现。",
    }


def compare_login_summaries(
    baseline: dict[str, object], current: dict[str, object],
) -> dict[str, object]:
    """只比较同一数据集、同一源码快照的 Baseline 与复查策略。"""
    if baseline["policy"] != BASELINE_POLICY_NAME or current["policy"] != POLICY_NAME:
        raise ValueError("登录规则对照必须按 Baseline → 当前策略")
    for field in ("dataset", "dataset_sha256", "policy_source_sha256", "case_count"):
        if baseline[field] != current[field]:
            raise ValueError(f"登录对照的 {field} 不一致")
    metrics = (
        "task_success_rate", "unclassified_case_count", "bug_precision", "bug_recall",
        "bug_f1", "average_steps", "average_latency_seconds", "bug_reproduction_count",
        "time_budget_exceeded_count", "recovery_attempt_case_count",
        "recovered_case_count", "recovery_rate",
    )
    result = {
        name: {"before": baseline.get(name), "after": current.get(name)}
        for name in metrics
    }
    result.update({
        "confusion": {"before": baseline["confusion"], "after": current["confusion"]},
        "dataset": current["dataset"], "dataset_sha256": current["dataset_sha256"],
        "policy_source_sha256": current["policy_source_sha256"],
        "baseline_policy": baseline["policy"], "current_policy": current["policy"],
    })
    return result
