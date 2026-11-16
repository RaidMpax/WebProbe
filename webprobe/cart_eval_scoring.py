"""购物车评测器：从真实步骤证据评分，并按 Case 统计指标。"""

from typing import Literal, Protocol

from webprobe.cart_dataset import CART_DATASET, CartCase
from webprobe.cart_loop import CartStepRecord


BASELINE_POLICY_NAME = "cart_rule_baseline_no_recheck_v1"
POLICY_NAME = "cart_rule_recheck_v1"
MODEL_POLICY_NAME = "cart_model_loop_v1_recheck"
CartPolicy = Literal["baseline", "rule", "model"]


class CartRunLike(Protocol):
    """两种 Agent 循环都会提供的评分字段。"""

    status: str
    bug_type: str | None
    reproduced_in_session: bool


POLICY_NAMES = {
    "baseline": BASELINE_POLICY_NAME, "rule": POLICY_NAME,
    "model": MODEL_POLICY_NAME,
}


def score_cart_case(
    case: CartCase, run: CartRunLike | None, elapsed_seconds: float,
    evidence_steps: list[CartStepRecord],
) -> tuple[str | None, bool | None, bool]:
    """返回结论、独立购物车断言、任务是否成功；未观察到购物车时断言为 None。"""
    add_observations = [
        step.observation for step in evidence_steps
        if step.action in ("add_product", "add_cup", "add_bag")
        and step.observation is not None
    ]
    assertion: bool | None = None
    if add_observations:
        last = add_observations[-1]
        expected_items = tuple((name, str(quantity)) for name, quantity in case.expected_items)
        assertion = (
            last.cart_count == str(case.expected_count)
            and last.cart_total == str(case.expected_total)
            and last.cart_items == expected_items
        )

    conclusion = None
    if run is not None and run.status == "completed":
        conclusion = "no_bug"
    elif run is not None and run.status == "suspected_bug" and run.bug_type:
        conclusion = "bug"

    repeated_total_mismatch = len(add_observations) >= 2 * sum(
        quantity for _, quantity in case.items
    )
    # 每轮末尾是一次真实商品点击后的快照；要求商品、数量正确且两轮总价都错。
    items_per_round = sum(quantity for _, quantity in case.items)
    mismatch_evidence = False
    if len(add_observations) >= items_per_round * 2:
        first = add_observations[items_per_round - 1]
        second = add_observations[items_per_round * 2 - 1]
        expected_items = tuple((name, str(quantity)) for name, quantity in case.expected_items)
        mismatch_evidence = all(
            observation.cart_count == str(case.expected_count)
            and observation.cart_items == expected_items
            and observation.cart_total is not None
            and observation.cart_total != str(case.expected_total)
            for observation in (first, second)
        )
        repeated_total_mismatch = mismatch_evidence

    if case.has_bug:
        success = (
            conclusion == "bug" and run is not None
            and run.bug_type == case.bug_type
            and run.reproduced_in_session is True
            and repeated_total_mismatch and assertion is False
        )
    else:
        success = conclusion == "no_bug" and assertion is True
    return conclusion, assertion, bool(success and elapsed_seconds <= case.max_seconds)


def summarize_cart_results(results: list[dict[str, object]]) -> dict[str, object]:
    """混淆矩阵以 Case 为单位；没有结论的 Case 另行记录，不当作无 Bug。"""
    if not results:
        raise ValueError("没有可汇总的购物车评测结果")
    classified = [row for row in results if row["agent_conclusion"] is not None]
    tp = sum(row["ground_truth"]["has_bug"] and row["agent_conclusion"] == "bug" for row in classified)
    fp = sum(not row["ground_truth"]["has_bug"] and row["agent_conclusion"] == "bug" for row in classified)
    fn = sum(row["ground_truth"]["has_bug"] and row["agent_conclusion"] == "no_bug" for row in classified)
    tn = sum(not row["ground_truth"]["has_bug"] and row["agent_conclusion"] == "no_bug" for row in classified)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall else None
    )
    policy = results[0]["policy"]
    recovery_cases = [
        row for row in results
        if sum(step["action"] == "open" for step in row["steps"]) > 1
    ]
    recovered_cases = sum(row["task_success"] is True for row in recovery_cases)
    model_name = results[0].get("model")
    token_fields = ("input_tokens", "output_tokens", "total_tokens")
    token_rows = [
        row["token_usage"] for row in results
        if isinstance(row.get("token_usage"), dict)
        and all(type(row["token_usage"].get(key)) is int for key in token_fields)
    ]
    token_usage = None
    if policy == MODEL_POLICY_NAME and len(token_rows) == len(results):
        token_usage = {
            key: sum(usage[key] for usage in token_rows) for key in token_fields
        }
    return {
        "policy": policy,
        "model": model_name,
        "dataset": CART_DATASET.name,
        "classification_unit": "one_case",
        "case_count": len(results),
        "task_success_count": sum(row["task_success"] is True for row in results),
        "task_success_rate": sum(row["task_success"] is True for row in results) / len(results),
        "unclassified_case_count": len(results) - len(classified),
        "agent_execution_failure_count": sum(row["run_status"] == "execution_failed" for row in results),
        "model_failure_count": sum(
            row["run_status"] == "model_failed" or row.get("model_error_type") is not None
            for row in results
        ),
        "time_budget_exceeded_count": sum(row["time_budget_exceeded"] is True for row in results),
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "bug_precision": precision,
        "bug_recall": recall,
        "bug_f1": f1,
        "bug_reproduction_count": sum(
            row["ground_truth"]["has_bug"] and row["reproduced_in_session"] is True
            for row in results
        ),
        "recovery_attempt_case_count": len(recovery_cases),
        "recovered_case_count": recovered_cases,
        "recovery_rate": recovered_cases / len(recovery_cases) if recovery_cases else None,
        "average_steps": sum(len(row["steps"]) for row in results) / len(results),
        "average_latency_seconds": sum(row["elapsed_seconds"] for row in results) / len(results),
        "token_usage": token_usage,
        "token_usage_covered_cases": len(token_rows),
        "api_cost": None,
        "metrics_note": "指标按 Case 统计；未形成结论的 Case 单独记为未分类，不进入 TP/FP/FN/TN，且不算成无 Bug。购物车总价疑似故障须在同一任务两轮干净页面状态下复现。Recovery Rate = 至少重新打开一次页面且最终完成的 Case / 有重新打开记录的 Case。",
    }


def compare_cart_summaries(
    baseline: dict[str, object], current: dict[str, object],
) -> dict[str, object]:
    """只允许同一数据集与实现快照的 Baseline → 复查策略对照。"""
    if baseline["policy"] != BASELINE_POLICY_NAME or current["policy"] != POLICY_NAME:
        raise ValueError("购物车对照必须按 Baseline → 当前规则策略")
    for field in ("dataset", "dataset_sha256", "policy_source_sha256", "case_count"):
        if baseline[field] != current[field]:
            raise ValueError(f"购物车对照的 {field} 不一致")
    metrics = (
        "task_success_rate", "unclassified_case_count", "bug_precision", "bug_recall",
        "bug_f1", "average_steps", "average_latency_seconds", "bug_reproduction_count",
        "agent_execution_failure_count", "time_budget_exceeded_count",
        "recovery_attempt_case_count", "recovered_case_count", "recovery_rate",
    )
    comparison = {
        metric: {"before": baseline.get(metric), "after": current.get(metric)}
        for metric in metrics
    }
    comparison["confusion"] = {
        "before": baseline["confusion"], "after": current["confusion"],
    }
    comparison.update({
        "dataset": current["dataset"],
        "dataset_sha256": current["dataset_sha256"],
        "policy_source_sha256": current["policy_source_sha256"],
        "baseline_policy": baseline["policy"],
        "current_policy": current["policy"],
    })
    return comparison
