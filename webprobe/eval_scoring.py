"""独立评分：只用固定 Case、运行结论和页面证据计算结果。

这里没有 Playwright 操作。先读 score_case（单题），再读 summarize_results（全套）。
"""

from webprobe.demo_loop import RunResult
from webprobe.eval_dataset import DATASET, EvalCase

MODEL_POLICY_NAME = "model_loop_v4_search_auto_repro_time_budget"


def score_case(
    case: EvalCase, run: RunResult | None, elapsed_seconds: float,
    search_matches: bool | None,
) -> tuple[str | None, bool]:
    """只根据固定 Ground Truth 和执行状态评分，不读取 Agent 自评文本。"""
    # 中断与待检查都没有结论；只有显式疑似 Bug 报告才算预测了 Bug。
    conclusion = None
    if run is not None and run.status == "completed":
        conclusion = "no_bug"
    elif run is not None and run.status == "suspected_bug" and run.bug_type:
        conclusion = "bug"
    if case.success_condition == "complete_without_bug":
        success = conclusion == "no_bug" and search_matches is True
    else:
        success = (
            conclusion == "bug"
            and run is not None
            and run.bug_type == case.bug_type
            and run.reproduced_in_session
            and search_matches is False
        )
    success = success and elapsed_seconds <= case.max_seconds
    return conclusion, success


def summarize_results(results: list[dict[str, object]]) -> dict[str, object]:
    """以一个 Case 为统计单位；未形成结论的 Case 单独计数。"""
    if not results:
        raise ValueError("没有可汇总的评测结果")
    policy_name = results[0]["policy"]
    if any(result["policy"] != policy_name for result in results):
        raise ValueError("不能把不同版本的 Case 混在同一次汇总中")
    model_name = results[0].get("model")
    if any(result.get("model") != model_name for result in results):
        raise ValueError("不能把不同模型的 Case 混在同一次汇总中")
    search_wait_values = {result.get("search_wait_ms") for result in results}
    if len(search_wait_values) != 1:
        raise ValueError("不能把不同搜索等待预算的 Case 混在同一次汇总中")
    classified = [result for result in results if result["agent_conclusion"] is not None]
    true_positives = [
        result for result in classified
        if result["ground_truth"]["has_bug"] and result["agent_conclusion"] == "bug"
    ]
    tp = len(true_positives)
    fp = sum(
        not result["ground_truth"]["has_bug"] and result["agent_conclusion"] == "bug"
        for result in classified
    )
    fn = sum(
        result["ground_truth"]["has_bug"] and result["agent_conclusion"] == "no_bug"
        for result in classified
    )
    tn = sum(
        not result["ground_truth"]["has_bug"] and result["agent_conclusion"] == "no_bug"
        for result in classified
    )
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    reproduced_tp = sum(result["reproduced_in_session"] is True for result in true_positives)
    token_fields = ("input_tokens", "output_tokens", "total_tokens")
    covered_usage = [
        usage for result in results
        if isinstance((usage := result.get("token_usage")), dict)
        and all(type(usage.get(name)) is int and usage[name] >= 0 for name in token_fields)
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
        "search_wait_ms": results[0].get("search_wait_ms"),
        "dataset": DATASET.name,
        "classification_unit": "one_case",
        "case_count": len(results),
        "task_success_count": sum(result["task_success"] is True for result in results),
        "task_success_rate": sum(result["task_success"] is True for result in results) / len(results),
        "time_budget_exceeded_count": sum(
            result.get("time_budget_exceeded") is True for result in results
        ),
        "agent_execution_failure_count": sum(
            result["run_status"] == "execution_failed" for result in results
        ),
        "model_failure_count": sum(
            result["run_status"] == "model_failed"
            or result.get("model_error_type") is not None
            for result in results
        ),
        "model_failure_breakdown": {
            error_type: sum(result.get("model_error_type") == error_type for result in results)
            for error_type in sorted({
                result.get("model_error_type") for result in results
                if result.get("model_error_type") is not None
            })
        },
        "unclassified_case_count": len(results) - len(classified),
        "evaluator_search_assertion_failed_count": sum(
            result["search_assertion"]["passed"] is False for result in results
        ),
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "bug_precision": precision,
        "bug_recall": recall,
        "bug_f1": f1,
        "in_session_bug_reproduction_rate": reproduced_tp / tp if tp else None,
        "recovery_attempt_case_count": len(recovery_cases),
        "recovered_case_count": recovered_cases,
        "recovery_rate": recovered_cases / len(recovery_cases) if recovery_cases else None,
        "average_steps": sum(len(result["steps"]) for result in results) / len(results),
        "average_latency_seconds": sum(result["elapsed_seconds"] for result in results) / len(results),
        "token_usage": token_usage,
        "token_usage_covered_cases": len(covered_usage),
        "api_cost": None,
        "metrics_note": (
            "本地搜索小数据集；未形成结论的 Case 不计入混淆矩阵，超时单独统计。"
            "Recovery Rate = 至少记录一次显式恢复且最终成功的 Case / 有恢复记录的 Case。"
            "Token 用量只汇总所有 Case 都完整返回时的模型 usage；未保存真实定价，API Cost 不估算。"
            "模型服务错误数可与时间预算超限数重叠。"
        ),
    }
