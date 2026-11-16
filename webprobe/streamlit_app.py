"""WebProbe 本地演示界面：运行一个固定 Case、查看证据、执行 Evals。"""

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import streamlit as st
from playwright.sync_api import sync_playwright

from webprobe.eval_dataset import load_cases as load_search_cases
from webprobe.form_dataset import load_form_cases
from webprobe.form_evals import run_form_case, run_form_suite
from webprobe.evals import run_case as run_search_case, run_suite as run_search_suite
from webprobe.cart_dataset import load_cart_cases
from webprobe.cart_evals import run_cart_case, run_cart_suite
from webprobe.login_dataset import load_login_cases
from webprobe.login_evals import run_login_case, run_login_suite
from webprobe.run_history import (
    DEFAULT_DB_PATH, index_eval_summary, index_single_result, list_runs,
)
from webprobe.llm_decider import DEFAULT_GEMINI_MODEL, OpenAIDecider, model_credentials_ready


PROJECT_ROOT = Path(__file__).resolve().parents[1]
UI_RUNS = PROJECT_ROOT / "artifacts" / "ui_runs"
FORM_EVAL_RUNS = PROJECT_ROOT / "artifacts" / "form_evals"
SEARCH_EVAL_RUNS = PROJECT_ROOT / "artifacts" / "evals"
CART_EVAL_RUNS = PROJECT_ROOT / "artifacts" / "cart_evals"
LOGIN_EVAL_RUNS = PROJECT_ROOT / "artifacts" / "login_evals"
HISTORY_DB = DEFAULT_DB_PATH
DATASET_NAMES = {
    "商品搜索（4 题）": "search_v3.jsonl",
    "反馈表单（13 题）": "feedback_v3.jsonl",
    "购物车（7 题）": "cart_v1.jsonl",
    "登录（6 题）": "login_v1.jsonl",
}
POLICIES = {
    "规则策略（复查疑似故障）": "rule",
    "规则基线（不复查）": "rule_baseline",
    "模型策略（需要 API 配置）": "model",
}
BUG_TYPE_LABELS = {
    "required_field_bypass": "必填校验被绕过",
    "search_result_mismatch": "搜索结果与固定规格不符",
    "wrong_cart_total": "购物车总价计算错误",
    "valid_login_rejected": "正确测试账号被拒绝",
}


def _apply_style() -> None:
    """让页面像测试工作台：安静底色，颜色只表达运行状态。"""
    st.markdown(
        """
        <style>
        :root { --ink: #18252D; --muted: #63727A; --line: #D9E0DF;
                --paper: #F4F6F3; --amber: #D98A25; }
        [data-testid="stAppViewContainer"] { background: var(--paper); color: var(--ink); }
        [data-testid="stSidebar"] { background: #E8EDEA; border-right: 1px solid var(--line); }
        [data-testid="stHeader"] { background: transparent; }
        h1, h2, h3 { color: var(--ink); letter-spacing: -0.025em; }
        .stButton > button { border-radius: 5px; border: 1px solid #263B45;
                            background: #263B45; color: white; min-height: 2.7rem; }
        .stButton > button:hover { border-color: var(--amber); background: #314B56; color: white; }
        code, pre { border-radius: 4px !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _header() -> None:
    st.title("WebProbe")
    st.caption("本地浏览器 QA 工作台 · TestLab only")


def _credentials_ready() -> bool:
    return model_credentials_ready()


def _execute_case(
    case_id: str, policy: str, suite: str = "form",
    max_steps: int | None = None, max_seconds: int | None = None,
) -> Path:
    """运行一条 TestLab Case；业务断言和证据格式复用对应 Evals。"""
    if suite == "login" and policy == "model":
        raise ValueError("登录目前只提供离线规则策略")
    if suite == "search":
        cases = {case.case_id: case for case in load_search_cases()}
        runner = run_search_case
        task_kind = "search"
    elif suite == "form":
        cases = {case.case_id: case for case in load_form_cases()}
        runner = run_form_case
        task_kind = "form"
    elif suite == "cart":
        cases = {case.case_id: case for case in load_cart_cases()}
        runner = run_cart_case
        task_kind = "cart"
    elif suite == "login":
        cases = {case.case_id: case for case in load_login_cases()}
        runner = run_login_case
        task_kind = "login"
    else:
        raise ValueError(f"不支持的单题任务类型: {suite}")
    case = cases[case_id]
    step_limit = 13 if policy == "model" else 30
    if max_steps is not None and (
        type(max_steps) is not int or not 1 <= max_steps <= step_limit
    ):
        raise ValueError(f"最大步骤数必须在 1 到 {step_limit} 之间")
    if max_seconds is not None and (
        type(max_seconds) is not int or not 1 <= max_seconds <= 120
    ):
        raise ValueError("最大执行时间必须在 1 到 120 秒之间")
    if max_steps is not None or max_seconds is not None:
        case = replace(
            case,
            max_steps=max_steps if max_steps is not None else case.max_steps,
            max_seconds=max_seconds if max_seconds is not None else case.max_seconds,
        )
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    output_dir = UI_RUNS / run_id
    output_dir.mkdir(parents=True)
    decider = OpenAIDecider.from_env(task_kind=task_kind) if policy == "model" else None
    rate_limit_wait = (
        decider.wait_for_request_capacity(case.max_steps + 1)
        if decider is not None else 0.0
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            if suite == "cart":
                local_policy = "baseline" if policy == "rule_baseline" else policy
                result = runner(
                    browser, case, output_dir, policy=local_policy, decider=decider,
                )
            elif suite == "login":
                local_policy = "baseline" if policy == "rule_baseline" else policy
                result = runner(browser, case, output_dir, policy=local_policy)
            else:
                result = runner(
                    browser, case, output_dir, policy=policy, decider=decider,
                )
        finally:
            browser.close()
    result_path = output_dir / "result.json"
    result["rate_limit_wait_seconds"] = rate_limit_wait
    result["budget"] = {
        "max_steps": case.max_steps,
        "max_seconds": case.max_seconds,
    }
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    index_single_result(result_path, suite=suite, db_path=HISTORY_DB)
    return result_path


def run_test_page() -> None:
    _header()
    st.header("运行测试")
    st.write("选择 TestLab 中的一条固定任务。每次运行都会启动新的浏览器上下文并保存证据。")

    suite_label = st.radio(
        "TestLab 功能", ["商品搜索", "反馈表单", "购物车", "登录"],
        horizontal=True, key="single_test_suite",
    )
    suite = {
        "商品搜索": "search", "反馈表单": "form", "购物车": "cart", "登录": "login",
    }[suite_label]
    cases = {
        "search": load_search_cases,
        "form": load_form_cases,
        "cart": load_cart_cases,
        "login": load_login_cases,
    }[suite]()
    by_id = {case.case_id: case for case in cases}
    selected_id = st.selectbox(
        "测试任务",
        options=list(by_id),
        format_func=lambda case_id: f"{case_id}  ·  {by_id[case_id].task}",
        key=f"single_test_case_{suite}",
    )
    case = by_id[selected_id]
    st.markdown("**测试目标**")
    st.info(case.task)
    st.caption(
        f"TestLab 页面：`lab/{ {'search': 'search.html', 'form': 'feedback.html', 'cart': 'cart.html', 'login': 'login.html'}[suite] }`　|　"
        f"最多 {case.max_steps} 个浏览器动作　|　"
        f"最长 {case.max_seconds} 秒"
    )

    policy_options = list(POLICIES) if suite in ("search", "form", "cart") else list(POLICIES)[:2]
    policy_label = st.radio("执行策略", policy_options, horizontal=True)
    policy = POLICIES[policy_label]
    step_limit = 13 if policy == "model" else 30
    max_steps = st.slider(
        "最大浏览器动作数", min_value=1, max_value=step_limit,
        value=min(case.max_steps, step_limit),
        key=f"single_steps_{suite}_{selected_id}_{policy}",
    )
    max_seconds = st.slider(
        "最长执行时间（秒）", min_value=1, max_value=120,
        value=min(case.max_seconds, 120),
        key=f"single_seconds_{suite}_{selected_id}_{policy}",
    )
    if policy == "model":
        if _credentials_ready():
            st.caption(f"已从项目 .env 读取 Gemini 配置，将使用 {DEFAULT_GEMINI_MODEL}。")
        else:
            st.warning("还没找到 Gemini Key。按 README 的三步把旧 Key 配置到项目 .env。")
    if policy == "model":
        st.caption(f"模型最多调用 {max_steps + 1} 次；网页内容作为不可信观察处理。")

    if st.button("启动测试", type="primary", disabled=(policy == "model" and not _credentials_ready())):
        try:
            with st.spinner("正在启动隔离浏览器并执行测试…"):
                result_path = _execute_case(
                    selected_id, policy, suite=suite,
                    max_steps=max_steps, max_seconds=max_seconds,
                )
            st.session_state["latest_ui_result"] = str(result_path)
            st.success("测试完成。打开左侧“测试报告”查看轨迹与证据。")
            st.code(str(result_path.parent), language=None)
        except Exception as error:
            st.error(f"测试未能完成：{type(error).__name__}: {error}")


def _available_reports() -> list[Path]:
    # 旧版文件先导入索引；新报告运行时已在 _execute_case 即时登记。
    for path in UI_RUNS.glob("*/result.json"):
        index_single_result(path, db_path=HISTORY_DB)
    return [
        row.evidence_path for row in list_runs(
            run_type="single", limit=1_000, db_path=HISTORY_DB,
        ) if row.evidence_path.is_file()
    ]


def report_page() -> None:
    _header()
    st.header("测试报告")
    reports = _available_reports()
    if not reports:
        st.info("还没有单题运行记录。先到“运行测试”执行一个 TestLab Case。")
        return

    latest = st.session_state.get("latest_ui_result")
    default = next((index for index, path in enumerate(reports) if str(path) == latest), 0)
    selected = st.selectbox(
        "运行记录", reports, index=default,
        format_func=lambda path: f"{path.parent.name} · {path.stem}",
    )
    result = json.loads(selected.read_text(encoding="utf-8"))

    if result["agent_conclusion"] == "bug":
        bug_label = BUG_TYPE_LABELS.get(
            result["reported_bug_type"], result["reported_bug_type"],
        )
        st.warning(f"发现疑似故障 · {bug_label}")
        st.caption("复现证据已通过固定规则核验；Agent 成功完成了这次测试任务。")
    elif result["task_success"]:
        st.success(f"任务完成 · {result['agent_conclusion'] or result['run_status']}")
    elif result.get("time_budget_exceeded"):
        st.error("未完成 · 超过任务时间预算")
    else:
        st.error(f"未完成 · {result['run_status']}")
        if result.get("model_error_type"):
            st.caption(f"模型服务错误类别：{result['model_error_type']}")

    left, right = st.columns([1, 1])
    with left:
        st.markdown("**测试目标**")
        st.write(result["task"])
        st.markdown("**独立断言**")
        assertion_result = (
            result.get("form_assertion")
            or result.get("search_assertion")
            or result.get("cart_assertion")
            or result.get("login_assertion")
            or {}
        )
        assertion = assertion_result.get("passed")
        st.write("通过" if assertion is True else "失败" if assertion is False else "没有足够证据")
        if "search_assertion" in result:
            st.markdown("**搜索结果**")
            actual_products = assertion_result.get("actual_products")
            st.write("、".join(actual_products) if actual_products is not None else "没有完整搜索结果")
            st.caption(f"固定预期：{'、'.join(result.get('expected_products') or [])}")
        if "cart_assertion" in result:
            st.markdown("**购物车观察**")
            st.write(
                f"件数：{assertion_result.get('actual_count') or '—'}　|　"
                f"总价：¥{assertion_result.get('actual_total') or '—'}"
            )
        if "login_assertion" in result:
            st.markdown("**登录观察**")
            st.write(
                f"状态：{assertion_result.get('actual_state') or '—'}　|　"
                f"提示：{assertion_result.get('actual_status') or '—'}"
            )
        st.markdown("**运行摘要**")
        st.write(
            f"策略：{result['policy']}  |  用时：{result['elapsed_seconds']:.2f}s  |  "
            f"模型调用：{result['model_calls'] if result['model_calls'] is not None else '—'}"
        )
        budget = result.get("budget")
        if isinstance(budget, dict):
            st.caption(
                f"预算：最多 {budget.get('max_steps', '—')} 个浏览器动作，"
                f"最长 {budget.get('max_seconds', '—')} 秒。"
            )
    with right:
        st.markdown("**页面截图**")
        screenshot = Path(result["screenshot"])
        if screenshot.is_file():
            st.image(str(screenshot), width="stretch")

    st.subheader("执行轨迹")
    for index, step in enumerate(result["steps"], start=1):
        observation = step.get("observation")
        title = f"{index}. {step['action']}"
        if observation:
            title += f" · {observation.get('status') or '页面已观察'}"
        elif step.get("error"):
            title += f" · {step.get('error_type') or '错误'}"
        with st.expander(title, expanded=(index == 1)):
            if observation:
                st.json(observation)
            if step.get("error"):
                st.code(step["error"])
    if result.get("decision_summaries"):
        st.markdown("**模型可见决策摘要**")
        for summary in result["decision_summaries"]:
            st.write(f"• {summary}")
    if result.get("recoveries"):
        st.markdown("**恢复操作**")
        for recovery in result["recoveries"]:
            st.write(f"• {recovery}")
    if result.get("rate_limit_wait_seconds"):
        st.caption(
            f"为遵守 Gemini 免费层速率限制，执行前等待了 "
            f"{result['rate_limit_wait_seconds']:.1f} 秒。"
        )

    trace = Path(result["trace"])
    if trace.is_file():
        st.download_button(
            "下载 Playwright Trace", trace.read_bytes(), file_name=trace.name,
            mime="application/zip",
        )


def _available_eval_runs(dataset_name: str | None = None) -> list[Path]:
    """合并所有固定功能集的评测历史，并可按数据集名称筛选。"""
    paths = [
        *FORM_EVAL_RUNS.glob("*/summary.json"),
        *SEARCH_EVAL_RUNS.glob("*/summary.json"),
        *CART_EVAL_RUNS.glob("*/summary.json"),
        *LOGIN_EVAL_RUNS.glob("*/summary.json"),
    ]
    for path in paths:
        index_eval_summary(path, db_path=HISTORY_DB)
    return [
        row.evidence_path for row in list_runs(
            run_type="eval", dataset=dataset_name, limit=1_000, db_path=HISTORY_DB,
        ) if row.evidence_path.is_file()
    ]


def _run_rule_comparison(dataset_name: str) -> Path:
    """依次运行同一数据集的规则 Baseline 和当前版，返回当前版目录。"""
    if dataset_name == "search_v3.jsonl":
        baseline = run_search_suite(policy="rule_baseline")
        return run_search_suite(
            policy="rule", baseline_summary=baseline / "summary.json",
        )
    if dataset_name == "feedback_v3.jsonl":
        baseline = run_form_suite(policy="rule_baseline")
        return run_form_suite(
            policy="rule", baseline_summary=baseline / "summary.json",
        )
    if dataset_name == "cart_v1.jsonl":
        baseline = run_cart_suite(policy="baseline")
        return run_cart_suite(policy="rule", baseline_summary=baseline / "summary.json")
    if dataset_name == "login_v1.jsonl":
        baseline = run_login_suite(policy="baseline")
        return run_login_suite(policy="rule", baseline_summary=baseline / "summary.json")
    raise ValueError(f"不支持的固定评测集: {dataset_name}")


def _comparison_value(label: str, value: object) -> str:
    """把两份评测的通用比较字段显示为简短文本。"""
    if label == "confusion" and isinstance(value, dict):
        return "TP {tp} · FP {fp} · FN {fn} · TN {tn}".format(**value)
    if isinstance(value, float):
        if label in (
            "task_success_rate", "recovery_rate", "bug_f1", "bug_precision", "bug_recall",
        ):
            return f"{value:.1%}"
        if label == "average_latency_seconds":
            return f"{value:.3f}s"
        return f"{value:.2f}"
    if value is None:
        return "—"
    return str(value)


def _case_assertion_label(row: dict[str, object]) -> str:
    """同一列显示搜索、表单或购物车评测器的独立断言结果。"""
    assertion = (
        row.get("form_assertion") or row.get("search_assertion")
        or row.get("cart_assertion") or row.get("login_assertion") or {}
    )
    passed = assertion.get("passed") if isinstance(assertion, dict) else None
    return "通过" if passed is True else "失败" if passed is False else "无证据"


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    """读取运行目录中的 JSONL；空文件表示没有记录。"""
    if not path.is_file():
        return []
    rows: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path} 第 {line_number} 行不是 JSON 对象")
        rows.append(value)
    return rows


def _artifact_file(path_value: object) -> Path | None:
    """只允许 Dashboard 读取 artifacts/ 内存在的证据文件。"""
    if not isinstance(path_value, str) or not path_value:
        return None
    candidate = Path(path_value).resolve()
    artifact_root = (PROJECT_ROOT / "artifacts").resolve()
    if not candidate.is_relative_to(artifact_root) or not candidate.is_file():
        return None
    return candidate


def _show_run_case_evidence(
    result: dict[str, object] | None,
    fallback_record: dict[str, object] | None,
    key_prefix: str,
) -> None:
    """展示单次运行的截图、Trace 和步骤；可在并排对照中复用。"""
    evidence = result or fallback_record or {}
    screenshot = _artifact_file(evidence.get("screenshot"))
    if screenshot:
        st.image(str(screenshot), caption="最终页面截图", width="stretch")
    else:
        st.caption("没有可读取的截图证据。")
    trace = _artifact_file(evidence.get("trace"))
    if trace:
        st.download_button(
            "下载 Playwright Trace", trace.read_bytes(), file_name=trace.name,
            mime="application/zip", key=f"trace_{key_prefix}",
        )

    if result is None:
        st.info("该运行没有逐题轨迹记录。")
        return
    steps = result.get("steps")
    if not isinstance(steps, list) or not steps:
        st.info("该 Case 没有保存浏览器步骤。")
    else:
        for index, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                continue
            observation = step.get("observation")
            status = observation.get("status") if isinstance(observation, dict) else None
            title = f"{index}. {step.get('action') or '未知动作'}"
            if status:
                title += f" · {status}"
            elif step.get("error_type"):
                title += f" · {step['error_type']}"
            with st.expander(title):
                if isinstance(observation, dict):
                    st.json(observation)
                if step.get("error"):
                    st.code(str(step["error"]))
    for label, key in (("决策摘要", "decision_summaries"), ("恢复操作", "recoveries")):
        summaries = result.get(key)
        if isinstance(summaries, list) and summaries:
            st.markdown(f"**{label}**")
            for summary in summaries:
                st.write(f"• {summary}")


def _show_bad_case_review(run_dir: Path, bad_cases: list[dict[str, object]]) -> None:
    """展示分类、对应运行轨迹和截图/Trace，供人工复核 Bad Case。"""
    st.subheader("Bad Case 复查")
    st.caption("自动分类只用于筛选现象，不代表已确认网站根因；请结合步骤和证据人工判断。")
    if not bad_cases:
        st.success("这次运行没有被分类为 Bad Case 的 Case。")
        return

    categories = sorted({str(row.get("category") or "未分类") for row in bad_cases})
    category_filter = st.selectbox(
        "按现象筛选", ["全部"] + categories,
        key=f"bad_case_category_{run_dir.name}",
    )
    filtered = [
        row for row in bad_cases
        if category_filter == "全部" or row.get("category") == category_filter
    ]
    st.dataframe(
        [
            {
                "Case": row.get("case_id"),
                "分类": row.get("category") or "未分类",
                "运行状态": row.get("run_status"),
                "需要人工复核": "是" if row.get("requires_manual_review") else "否",
            }
            for row in filtered
        ], hide_index=True, width="stretch",
        key=f"bad_case_table_{run_dir.name}_{category_filter}",
    )

    selected_case_id = st.selectbox(
        "查看 Case 证据",
        options=[str(row.get("case_id") or "未知 Case") for row in filtered],
        format_func=lambda case_id: next(
            f"{case_id} · {row.get('category') or '未分类'}"
            for row in filtered if row.get("case_id") == case_id
        ),
        key=f"bad_case_detail_{run_dir.name}_{category_filter}",
    )
    record = next(
        row for row in filtered if row.get("case_id") == selected_case_id
    )
    st.markdown(f"**分类：** {record.get('category') or '未分类'}")
    st.markdown(f"**分类依据：** {record.get('basis') or '没有记录'}")
    st.markdown(f"**运行状态：** {record.get('run_status') or '未知'}")
    ground_truth = record.get("ground_truth_has_bug")
    truth_label = "有预设 Bug" if ground_truth is True else "无预设 Bug" if ground_truth is False else "未知"
    st.markdown(f"**固定 Ground Truth：** {truth_label}")
    if record.get("error_type") or record.get("model_error_type"):
        st.markdown(
            f"**错误类别：** {record.get('error_type') or record.get('model_error_type')}"
        )

    result_rows = _read_jsonl(run_dir / "results.jsonl")
    result = next(
        (row for row in result_rows if row.get("case_id") == selected_case_id), None,
    )
    if result is None:
        st.caption("该历史运行没有对应的逐题轨迹文件，仍可查看分类记录中的证据路径。")
    st.markdown("**浏览器执行轨迹**")
    _show_run_case_evidence(
        result, record, f"bad_case_{run_dir.name}_{selected_case_id}",
    )


def _dataset_identity_status(
    baseline: dict[str, object], current: dict[str, object], comparison: dict[str, object],
) -> str:
    """核对 Case 对照的身份；返回 verified、unverified 或 mismatch。"""
    if baseline.get("dataset") != current.get("dataset"):
        return "mismatch"
    if baseline.get("case_count") != current.get("case_count"):
        return "mismatch"
    if comparison.get("dataset_hash_match") is False:
        return "mismatch"
    before_hash = baseline.get("dataset_sha256")
    after_hash = current.get("dataset_sha256")
    comparison_hash = comparison.get("dataset_sha256")
    if before_hash and after_hash and before_hash != after_hash:
        return "mismatch"
    if comparison_hash and after_hash and comparison_hash != after_hash:
        return "mismatch"
    if before_hash and after_hash:
        return "verified"
    return "unverified"


def _compare_case_results(
    baseline_rows: list[dict[str, object]],
    current_rows: list[dict[str, object]],
    baseline_bad_cases: list[dict[str, object]],
    current_bad_cases: list[dict[str, object]],
    *,
    baseline_has_bad_case_file: bool,
    current_has_bad_case_file: bool,
) -> list[dict[str, str]]:
    """按 Case ID 对齐两次运行；成功状态和分类都保留，避免只看汇总数字。"""
    baseline_by_id = {str(row["case_id"]): row for row in baseline_rows if row.get("case_id")}
    current_by_id = {str(row["case_id"]): row for row in current_rows if row.get("case_id")}
    baseline_categories = {
        str(row["case_id"]): str(row.get("category") or "未分类")
        for row in baseline_bad_cases if row.get("case_id")
    }
    current_categories = {
        str(row["case_id"]): str(row.get("category") or "未分类")
        for row in current_bad_cases if row.get("case_id")
    }

    def task_label(row: dict[str, object] | None) -> str:
        if row is None:
            return "缺少记录"
        return "成功" if row.get("task_success") is True else "未成功"

    def conclusion_label(row: dict[str, object] | None) -> str:
        if row is None:
            return "缺少记录"
        return {"bug": "报告疑似故障", "no_bug": "报告无故障"}.get(
            row.get("agent_conclusion"), "未形成结论",
        )

    def category_label(
        case_id: str, categories: dict[str, str], file_exists: bool,
    ) -> str:
        if case_id in categories:
            return categories[case_id]
        return "无分类" if file_exists else "无分类文件"

    records: list[dict[str, str]] = []
    all_case_ids = sorted(set(baseline_by_id) | set(current_by_id))
    for case_id in all_case_ids:
        before = baseline_by_id.get(case_id)
        after = current_by_id.get(case_id)
        before_success = before.get("task_success") if before else None
        after_success = after.get("task_success") if after else None
        if before is None or after is None:
            change = "缺少对应运行记录"
        elif before_success is False and after_success is True:
            change = "修复：当前版完成"
        elif before_success is True and after_success is False:
            change = "回归：当前版未完成"
        elif before_success is True and after_success is True:
            change = "持续成功"
        elif before_success is False and after_success is False:
            change = "持续未完成"
        else:
            change = "结果状态不明确"
        records.append({
            "Case": case_id,
            "Baseline 任务": task_label(before),
            "当前版任务": task_label(after),
            "Baseline 结论": conclusion_label(before),
            "当前版结论": conclusion_label(after),
            "Baseline Bad Case": category_label(
                case_id, baseline_categories, baseline_has_bad_case_file,
            ),
            "当前版 Bad Case": category_label(
                case_id, current_categories, current_has_bad_case_file,
            ),
            "变化": change,
        })
    return records


def _show_case_comparison(
    selected_summary: Path, summary: dict[str, object], comparison: dict[str, object],
) -> None:
    """读取对照运行的产物并展示逐 Case 改善、回归和分类变化。"""
    st.subheader("逐 Case 改善与回归")
    baseline_summary_path = _artifact_file(comparison.get("baseline_summary"))
    if baseline_summary_path is None:
        st.info("这条对照记录没有可读取的项目内 Baseline 摘要，无法对齐逐题结果。")
        return
    baseline = json.loads(baseline_summary_path.read_text(encoding="utf-8"))
    identity = _dataset_identity_status(baseline, summary, comparison)
    if identity == "mismatch":
        st.error("Baseline 与当前运行的数据集身份或 Case 数量不一致；已停止逐题比较。")
        return
    if identity == "unverified":
        st.warning("历史摘要没有完整的数据集哈希；下表只按 Case ID 对齐，结果身份尚未完全验证。")

    baseline_run_dir = baseline_summary_path.parent
    baseline_results_path = baseline_run_dir / "results.jsonl"
    current_results_path = selected_summary.parent / "results.jsonl"
    if not baseline_results_path.is_file() or not current_results_path.is_file():
        st.info("Baseline 或当前运行缺少逐题结果文件，无法生成 Case 对照。")
        return
    baseline_bad_path = baseline_run_dir / "bad_cases.jsonl"
    current_bad_path = selected_summary.parent / "bad_cases.jsonl"
    baseline_rows = _read_jsonl(baseline_results_path)
    current_rows = _read_jsonl(current_results_path)
    case_changes = _compare_case_results(
        baseline_rows,
        current_rows,
        _read_jsonl(baseline_bad_path),
        _read_jsonl(current_bad_path),
        baseline_has_bad_case_file=baseline_bad_path.is_file(),
        current_has_bad_case_file=current_bad_path.is_file(),
    )
    if case_changes:
        st.dataframe(
            case_changes, hide_index=True, width="stretch",
            key=f"case_comparison_{selected_summary.parent.name}",
        )
        change_by_id = {row["Case"]: row["变化"] for row in case_changes}
        selected_case_id = st.selectbox(
            "并排查看这个 Case 的证据",
            options=list(change_by_id),
            format_func=lambda case_id: f"{case_id} · {change_by_id[case_id]}",
            key=f"paired_case_{selected_summary.parent.name}",
        )
        baseline_by_id = {
            str(row["case_id"]): row for row in baseline_rows if row.get("case_id")
        }
        current_by_id = {
            str(row["case_id"]): row for row in current_rows if row.get("case_id")
        }
        before = baseline_by_id.get(selected_case_id)
        after = current_by_id.get(selected_case_id)
        task = (after or before or {}).get("task")
        if task:
            st.caption(str(task))
        left, right = st.columns(2)
        with left:
            st.markdown(f"**Baseline · {baseline.get('policy', comparison.get('baseline_policy'))}**")
            if before:
                st.write(
                    f"任务：{'成功' if before.get('task_success') is True else '未成功'}　|　"
                    f"结论：{before.get('agent_conclusion') or before.get('run_status')}"
                )
            _show_run_case_evidence(
                before, None, f"paired_baseline_{selected_summary.parent.name}_{selected_case_id}",
            )
        with right:
            st.markdown(f"**当前版 · {summary.get('policy', comparison.get('current_policy'))}**")
            if after:
                st.write(
                    f"任务：{'成功' if after.get('task_success') is True else '未成功'}　|　"
                    f"结论：{after.get('agent_conclusion') or after.get('run_status')}"
                )
            _show_run_case_evidence(
                after, None, f"paired_current_{selected_summary.parent.name}_{selected_case_id}",
            )
    else:
        st.info("两次运行都没有可对齐的 Case 记录。")


def dashboard_page() -> None:
    _header()
    st.header("Evaluation Dashboard")
    st.write("选择一套人工固定答案的数据集。规则对照会依次运行 Baseline 和当前策略，评分器独立检查浏览器证据。")

    dataset_label = st.selectbox("固定数据集", list(DATASET_NAMES), key="eval_dataset")
    dataset_name = DATASET_NAMES[dataset_label]
    run_mode = st.radio(
        "运行方式", ["Baseline + 当前规则对照", "单独运行一个策略"],
        horizontal=True, key="eval_run_mode",
    )
    if run_mode == "Baseline + 当前规则对照":
        if st.button("运行规则对照"):
            try:
                with st.spinner(f"正在运行 {dataset_label} 的 Baseline 和当前版；每题会保存截图与 Trace…"):
                    run_dir = _run_rule_comparison(dataset_name)
                st.session_state["latest_eval_summary"] = str(run_dir / "summary.json")
                st.success("规则对照完成并已保存。")
                st.rerun()
            except Exception as error:
                st.error(f"评测未能完成：{type(error).__name__}: {error}")
    else:
        policy_options = (
            list(POLICIES) if dataset_name in (
                "search_v3.jsonl", "feedback_v3.jsonl", "cart_v1.jsonl",
            )
            else list(POLICIES)[:2]
        )
        policy_label = st.selectbox("评测策略", policy_options, key="suite_policy")
        policy = POLICIES[policy_label]
        credentials_ready = _credentials_ready() if policy == "model" else True
        if policy == "model":
            st.warning("模型评测会调用所选服务商。只有点击运行后才会发送请求；费用和免费额度取决于服务商账户。")
            if not credentials_ready:
                st.info("当前没有检测到可用的模型配置；规则评测不需要 API Key。")

        if st.button(
            f"运行 {dataset_label}",
            disabled=(policy == "model" and not credentials_ready),
        ):
            try:
                with st.spinner(f"正在运行 {dataset_label}；每题会保存截图与 Trace…"):
                    if dataset_name == "search_v3.jsonl":
                        run_dir = run_search_suite(policy=policy)
                    elif dataset_name == "feedback_v3.jsonl":
                        run_dir = run_form_suite(policy=policy)
                    elif dataset_name == "login_v1.jsonl":
                        login_policy = "baseline" if policy == "rule_baseline" else policy
                        run_dir = run_login_suite(policy=login_policy)
                    else:
                        cart_policy = "baseline" if policy == "rule_baseline" else policy
                        run_dir = run_cart_suite(policy=cart_policy)
                st.session_state["latest_eval_summary"] = str(run_dir / "summary.json")
                st.success("评测完成并已保存。")
                st.rerun()
            except Exception as error:
                st.error(f"评测未能完成：{type(error).__name__}: {error}")

    summaries = _available_eval_runs(dataset_name)
    if not summaries:
        st.info("还没有完整评测记录。上方可运行规则策略；真实模型评测需先配置 API。")
        return
    latest = st.session_state.get("latest_eval_summary")
    default = next((index for index, path in enumerate(summaries) if str(path) == latest), 0)
    selected = st.selectbox(
        "已保存的评测", summaries, index=default,
        format_func=lambda path: f"{path.parent.name} · {json.loads(path.read_text(encoding='utf-8')).get('policy')}",
    )
    summary = json.loads(selected.read_text(encoding="utf-8"))
    st.caption(
        f"范围：{summary.get('evaluation_scope', 'full_suite')}　|　"
        f"数据集：{summary['dataset']}　|　题数：{summary['case_count']}"
    )

    st.subheader("任务结果")
    st.dataframe(
        {
            "指标": [
                "任务成功率", "未形成结论", "执行失败", "模型服务失败", "时间预算超限",
                "恢复成功率（完成数/尝试数）", "平均浏览器步骤", "平均耗时",
            ],
            "结果": [
                f"{summary['task_success_count']}/{summary['case_count']} ({summary['task_success_rate']:.0%})",
                str(summary["unclassified_case_count"]),
                str(summary["agent_execution_failure_count"]),
                str(summary["model_failure_count"])
                if "model_failure_count" in summary else "旧运行未记录",
                str(summary.get("time_budget_exceeded_count", 0)),
                (
                    f"{summary['recovered_case_count']}/{summary['recovery_attempt_case_count']} "
                    f"({summary['recovery_rate']:.0%})"
                    if summary.get("recovery_rate") is not None else "无恢复尝试"
                ) if "recovery_attempt_case_count" in summary else "旧运行未记录恢复指标",
                f"{summary['average_steps']:.1f}",
                f"{summary['average_latency_seconds']:.2f}s",
            ],
        }, hide_index=True, width="stretch",
        key=f"eval_task_metrics_{dataset_name}_{selected.parent.name}",
    )
    confusion = summary["confusion"]
    st.markdown(
        f"**按 Case 统计**　TP {confusion['tp']}　FP {confusion['fp']}　"
        f"FN {confusion['fn']}　TN {confusion['tn']}"
    )

    comparison_path = selected.parent / "comparison.json"
    if comparison_path.is_file():
        comparison = json.loads(comparison_path.read_text(encoding="utf-8"))
        st.subheader("Baseline 对照")
        st.caption(f"{comparison['baseline_policy']} → {comparison['current_policy']}")
        comparison_labels = {
            "task_success_rate": "任务成功率",
            "unclassified_case_count": "未形成结论",
            "agent_execution_failure_count": "执行失败",
            "model_failure_count": "模型服务失败",
            "time_budget_exceeded_count": "时间预算超限",
            "bad_case_count": "Bad Case 数",
            "average_steps": "平均步骤",
            "average_latency_seconds": "平均耗时",
            "bug_precision": "Bug Precision",
            "bug_recall": "Bug Recall",
            "bug_f1": "Bug F1",
            "recovery_attempt_case_count": "有恢复尝试的 Case",
            "recovered_case_count": "恢复后完成的 Case",
            "recovery_rate": "Recovery Rate",
            "confusion": "混淆矩阵",
        }
        comparison_rows = []
        for metric, label in comparison_labels.items():
            values = comparison.get(metric)
            if isinstance(values, dict) and "before" in values and "after" in values:
                comparison_rows.append({
                    "指标": label,
                    "Baseline": _comparison_value(metric, values["before"]),
                    "当前版": _comparison_value(metric, values["after"]),
                })
        if comparison_rows:
            st.dataframe(
                comparison_rows, hide_index=True, width="stretch",
                key=f"eval_comparison_{selected.parent.name}",
            )
        st.caption(
            "数据集哈希一致："
            + ("是" if comparison.get("dataset_hash_match", True) else "否")
        )
        _show_case_comparison(selected, summary, comparison)

    results_path = selected.parent / "results.jsonl"
    if results_path.is_file():
        rows = _read_jsonl(results_path)
        st.subheader("逐题结果")
        st.dataframe(
            [
                {
                    "Case": row["case_id"],
                    "结论": row["agent_conclusion"] or row["run_status"],
                    "断言": _case_assertion_label(row),
                    "搜索结果": "、".join(row.get("search_assertion", {}).get("actual_products") or []),
                    "购物车断言": (
                        "通过" if row.get("cart_assertion", {}).get("passed") is True
                        else "失败" if row.get("cart_assertion", {}).get("passed") is False
                        else "—"
                    ),
                    "登录断言": (
                        "通过" if row.get("login_assertion", {}).get("passed") is True
                        else "失败" if row.get("login_assertion", {}).get("passed") is False
                        else "—"
                    ),
                    "任务成功": "是" if row["task_success"] else "否",
                    "时间预算": "超限" if row.get("time_budget_exceeded") else "未超限",
                    "耗时": f"{row['elapsed_seconds']:.2f}s",
                }
                for row in rows
            ], hide_index=True, width="stretch",
            key=f"eval_case_results_{selected.parent.name}",
        )
    bad_cases_path = selected.parent / "bad_cases.jsonl"
    if bad_cases_path.is_file():
        _show_bad_case_review(selected.parent, _read_jsonl(bad_cases_path))
    else:
        st.subheader("Bad Case 复查")
        st.info("这条历史运行没有保存 Bad Case 分类文件；旧记录不会按新规则重新分类。")
    if summary.get("token_usage"):
        st.caption(
            f"Token 用量（{summary.get('token_usage_covered_cases', summary['case_count'])}/"
            f"{summary['case_count']} 个 Case）：{summary['token_usage']}；API 费用未计算。"
        )
    elif summary.get("token_usage_covered_cases", 0):
        st.caption(
            f"Token 用量合计未知：仅 {summary['token_usage_covered_cases']}/"
            f"{summary['case_count']} 个 Case 返回完整用量；API 费用未计算。"
        )
    else:
        st.caption("Token 用量：没有完整真实 API 用量记录；API 费用未计算。")
    st.caption(f"实验摘要：`{selected}`")


def main() -> None:
    st.set_page_config(page_title="WebProbe · Browser QA", page_icon="🧪", layout="wide")
    _apply_style()
    pages = {
        "测试工作台": [
            st.Page(run_test_page, title="运行测试", icon="🧭", default=True),
            st.Page(report_page, title="测试报告", icon="🧾"),
        ],
        "评测": [
            st.Page(dashboard_page, title="Evaluation Dashboard", icon="📊"),
        ],
    }
    st.navigation(pages, position="sidebar").run()


if __name__ == "__main__":
    main()
