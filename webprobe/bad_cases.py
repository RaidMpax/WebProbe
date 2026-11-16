"""根据已保存的评测字段，给失败 Case 做保守的现象分类。

分类只帮助人工筛选轨迹，不证明网站故障根因，也不参与 Evals 评分。
"""


def classify_bad_case(result: dict[str, object]) -> dict[str, object] | None:
    """成功任务返回 None；失败任务返回一个可复查的分类记录。"""
    if result["task_success"] is True:
        return None

    status = result["run_status"]
    conclusion = result["agent_conclusion"]
    has_bug = result["ground_truth"]["has_bug"]
    failed_step = next(
        (step for step in reversed(result["steps"]) if step["error_type"]), None
    )
    action = failed_step["action"] if failed_step else None
    error_type = failed_step["error_type"] if failed_step else None

    if status == "execution_failed":
        if action in ("wait_search", "wait_stock") and error_type == "SearchWaitTimeout":
            category = "Timeout / Loading Issue"
        elif action in ("fill", "submit", "detail") and error_type == "TimeoutError":
            category = "Element Localization Failure"
        else:
            category = "Tool Failure"
        basis = f"执行失败：{action or '未知动作'} / {error_type or '未知错误类型'}"
    elif status == "environment_error":
        category, basis = "Environment Failure", "浏览器环境报错"
    elif status == "model_failed" or result.get("model_error_type") is not None:
        model_error_type = result.get("model_error_type")
        category = "Model Failure"
        basis = f"模型调用或响应校验失败：{model_error_type or '未细分'}"
    elif conclusion == "bug" and not has_bug:
        category, basis = "False Positive", "报告了 Bug，但固定 Ground Truth 为无 Bug"
    elif conclusion == "no_bug" and has_bug:
        category, basis = "Missed Bug", "报告无 Bug，但固定 Ground Truth 为有 Bug"
    elif status in (
        "budget_exhausted", "step_budget_exhausted", "model_call_budget_exhausted",
        "time_budget_exhausted",
    ):
        category, basis = "Budget Exhausted", "达到执行预算，未完成任务"
    elif status == "needs_review":
        category, basis = "Inconclusive", "流程停止，但没有形成可评分的结论"
    else:
        category, basis = "Unclassified", "现有结构化证据不足以自动归类"

    return {
        "case_id": result["case_id"],
        "category": category,
        "basis": basis,
        "run_status": status,
        "agent_conclusion": conclusion,
        "ground_truth_has_bug": has_bug,
        "failed_action": action,
        "error_type": error_type,
        "model_error_type": result.get("model_error_type"),
        "screenshot": result.get("screenshot"),
        "trace": result.get("trace"),
        "requires_manual_review": True,
    }
