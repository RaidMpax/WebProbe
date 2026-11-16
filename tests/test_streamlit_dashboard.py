"""Evaluation Dashboard 的数据选择和展示辅助函数不需要启动 UI。"""

import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from webprobe import streamlit_app


def write_summary(directory: Path, dataset: str) -> Path:
    summary = directory / "run_001" / "summary.json"
    summary.parent.mkdir(parents=True)
    summary.write_text(json.dumps({"dataset": dataset}), encoding="utf-8")
    return summary


def test_available_runs_merge_and_filter_all_datasets(tmp_path: Path, monkeypatch) -> None:
    form_root = tmp_path / "form"
    search_root = tmp_path / "search"
    cart_root = tmp_path / "cart"
    login_root = tmp_path / "login"
    write_summary(form_root, "feedback_v3.jsonl")
    search_summary = write_summary(search_root, "search_v3.jsonl")
    cart_summary = write_summary(cart_root, "cart_v1.jsonl")
    login_summary = write_summary(login_root, "login_v1.jsonl")
    monkeypatch.setattr(streamlit_app, "FORM_EVAL_RUNS", form_root)
    monkeypatch.setattr(streamlit_app, "SEARCH_EVAL_RUNS", search_root)
    monkeypatch.setattr(streamlit_app, "CART_EVAL_RUNS", cart_root)
    monkeypatch.setattr(streamlit_app, "LOGIN_EVAL_RUNS", login_root)
    monkeypatch.setattr(streamlit_app, "HISTORY_DB", tmp_path / "history.sqlite3")

    assert streamlit_app._available_eval_runs("search_v3.jsonl") == [search_summary]
    assert streamlit_app._available_eval_runs("cart_v1.jsonl") == [cart_summary]
    assert streamlit_app._available_eval_runs("login_v1.jsonl") == [login_summary]
    assert len(streamlit_app._available_eval_runs()) == 4


def test_comparison_value_formats_confusion_and_rates() -> None:
    assert streamlit_app._comparison_value(
        "confusion", {"tp": 1, "fp": 2, "fn": 3, "tn": 4},
    ) == "TP 1 · FP 2 · FN 3 · TN 4"
    assert streamlit_app._comparison_value("task_success_rate", 0.75) == "75.0%"


def test_case_assertion_label_supports_both_eval_schemas() -> None:
    assert streamlit_app._case_assertion_label({
        "form_assertion": {"passed": True},
    }) == "通过"
    assert streamlit_app._case_assertion_label({
        "search_assertion": {"passed": False},
    }) == "失败"
    assert streamlit_app._case_assertion_label({
        "cart_assertion": {"passed": True},
    }) == "通过"
    assert streamlit_app._case_assertion_label({
        "login_assertion": {"passed": False},
    }) == "失败"
    assert streamlit_app._case_assertion_label({}) == "无证据"


def test_read_jsonl_skips_blank_lines_and_keeps_rows(tmp_path: Path) -> None:
    path = tmp_path / "records.jsonl"
    path.write_text('{"case_id":"case_1"}\n\n{"case_id":"case_2"}\n', encoding="utf-8")

    assert streamlit_app._read_jsonl(path) == [
        {"case_id": "case_1"}, {"case_id": "case_2"},
    ]


def test_artifact_file_rejects_paths_outside_artifacts(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "project"
    artifact_file = project_root / "artifacts" / "run_1" / "case.png"
    artifact_file.parent.mkdir(parents=True)
    artifact_file.write_bytes(b"image")
    outside_file = tmp_path / "private.txt"
    outside_file.write_text("private", encoding="utf-8")
    monkeypatch.setattr(streamlit_app, "PROJECT_ROOT", project_root)

    assert streamlit_app._artifact_file(str(artifact_file)) == artifact_file.resolve()
    assert streamlit_app._artifact_file(str(outside_file)) is None
    assert streamlit_app._artifact_file(None) is None


def test_case_comparison_marks_fixed_regressed_and_unchanged_cases() -> None:
    rows = streamlit_app._compare_case_results(
        [
            {"case_id": "fixed", "task_success": False, "agent_conclusion": None},
            {"case_id": "regressed", "task_success": True, "agent_conclusion": "no_bug"},
            {"case_id": "still_fails", "task_success": False, "agent_conclusion": None},
        ],
        [
            {"case_id": "fixed", "task_success": True, "agent_conclusion": "no_bug"},
            {"case_id": "regressed", "task_success": False, "agent_conclusion": None},
            {"case_id": "still_fails", "task_success": False, "agent_conclusion": None},
        ],
        [{"case_id": "fixed", "category": "Timeout / Loading Issue"}],
        [{"case_id": "regressed", "category": "Missed Bug"}],
        baseline_has_bad_case_file=True,
        current_has_bad_case_file=True,
    )

    assert [row["变化"] for row in rows] == [
        "修复：当前版完成", "回归：当前版未完成", "持续未完成",
    ]
    assert rows[0]["Baseline Bad Case"] == "Timeout / Loading Issue"
    assert rows[0]["当前版 Bad Case"] == "无分类"
    assert rows[1]["当前版 Bad Case"] == "Missed Bug"


def test_dataset_identity_requires_matching_names_counts_and_hashes() -> None:
    baseline = {"dataset": "search_v3.jsonl", "case_count": 4, "dataset_sha256": "same"}
    current = {"dataset": "search_v3.jsonl", "case_count": 4, "dataset_sha256": "same"}

    assert streamlit_app._dataset_identity_status(baseline, current, {}) == "verified"
    assert streamlit_app._dataset_identity_status(
        {"dataset": "search_v3.jsonl", "case_count": 4}, current, {},
    ) == "unverified"
    assert streamlit_app._dataset_identity_status(
        baseline, {**current, "dataset_sha256": "different"}, {},
    ) == "mismatch"


def test_rule_comparison_runs_baseline_before_current(tmp_path: Path, monkeypatch) -> None:
    baseline_dir = tmp_path / "baseline"
    current_dir = tmp_path / "current"
    search_runner = Mock(side_effect=[baseline_dir, current_dir])
    monkeypatch.setattr(streamlit_app, "run_search_suite", search_runner)

    assert streamlit_app._run_rule_comparison("search_v3.jsonl") == current_dir
    assert search_runner.call_args_list[0].kwargs == {"policy": "rule_baseline"}
    assert search_runner.call_args_list[1].kwargs == {
        "policy": "rule", "baseline_summary": baseline_dir / "summary.json",
    }


def test_cart_rule_comparison_runs_baseline_before_current(tmp_path: Path, monkeypatch) -> None:
    baseline_dir = tmp_path / "cart_baseline"
    current_dir = tmp_path / "cart_current"
    cart_runner = Mock(side_effect=[baseline_dir, current_dir])
    monkeypatch.setattr(streamlit_app, "run_cart_suite", cart_runner)

    assert streamlit_app._run_rule_comparison("cart_v1.jsonl") == current_dir
    assert cart_runner.call_args_list[0].kwargs == {"policy": "baseline"}
    assert cart_runner.call_args_list[1].kwargs == {
        "policy": "rule", "baseline_summary": baseline_dir / "summary.json",
    }


def test_login_rule_comparison_runs_baseline_before_current(tmp_path: Path, monkeypatch) -> None:
    baseline_dir = tmp_path / "login_baseline"
    current_dir = tmp_path / "login_current"
    login_runner = Mock(side_effect=[baseline_dir, current_dir])
    monkeypatch.setattr(streamlit_app, "run_login_suite", login_runner)

    assert streamlit_app._run_rule_comparison("login_v1.jsonl") == current_dir
    assert login_runner.call_args_list[0].kwargs == {"policy": "baseline"}
    assert login_runner.call_args_list[1].kwargs == {
        "policy": "rule", "baseline_summary": baseline_dir / "summary.json",
    }


def test_single_search_run_uses_search_evaluator_and_saves_report(
    tmp_path: Path, monkeypatch,
) -> None:
    @dataclass(frozen=True)
    class SearchFixture:
        case_id: str
        task: str
        max_steps: int
        max_seconds: int

    case = SearchFixture(
        case_id="search_fixture", task="搜索商品并检查结果。", max_steps=4,
        max_seconds=10,
    )
    browser = Mock()
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=Mock(return_value=browser)),
    )
    calls: list[tuple[object, object, Path, dict[str, object]]] = []

    @contextmanager
    def fake_sync_playwright():
        yield playwright

    def fake_run_case(actual_browser, actual_case, output_dir, **kwargs):
        calls.append((actual_browser, actual_case, output_dir, kwargs))
        return {
            "case_id": actual_case.case_id,
            "agent_conclusion": "no_bug",
            "task_success": True,
        }

    monkeypatch.setattr(streamlit_app, "UI_RUNS", tmp_path / "ui_runs")
    monkeypatch.setattr(streamlit_app, "HISTORY_DB", tmp_path / "history.sqlite3")
    monkeypatch.setattr(streamlit_app, "load_search_cases", lambda: [case])
    monkeypatch.setattr(streamlit_app, "run_search_case", fake_run_case)
    monkeypatch.setattr(streamlit_app, "sync_playwright", fake_sync_playwright)

    result_path = streamlit_app._execute_case(
        "search_fixture", "rule", suite="search", max_steps=6, max_seconds=20,
    )
    saved = json.loads(result_path.read_text(encoding="utf-8"))

    assert saved["case_id"] == "search_fixture"
    assert saved["task_success"] is True
    assert saved["rate_limit_wait_seconds"] == 0.0
    assert saved["budget"] == {"max_steps": 6, "max_seconds": 20}
    assert calls[0][0] is browser
    assert calls[0][1] != case
    assert calls[0][1].max_steps == 6
    assert calls[0][1].max_seconds == 20
    assert calls[0][3] == {"policy": "rule", "decider": None}
    browser.close.assert_called_once_with()


def test_single_login_run_uses_login_evaluator_without_model_key(tmp_path: Path, monkeypatch) -> None:
    case = SimpleNamespace(
        case_id="login_fixture", task="使用测试账号登录。", max_steps=5, max_seconds=10,
    )
    browser = Mock()
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=Mock(return_value=browser)),
    )
    calls: list[tuple[object, object, Path, dict[str, object]]] = []

    @contextmanager
    def fake_sync_playwright():
        yield playwright

    def fake_run_case(actual_browser, actual_case, output_dir, **kwargs):
        calls.append((actual_browser, actual_case, output_dir, kwargs))
        return {"case_id": actual_case.case_id, "task_success": True}

    monkeypatch.setattr(streamlit_app, "UI_RUNS", tmp_path / "ui_runs")
    monkeypatch.setattr(streamlit_app, "HISTORY_DB", tmp_path / "history.sqlite3")
    monkeypatch.setattr(streamlit_app, "load_login_cases", lambda: [case])
    monkeypatch.setattr(streamlit_app, "run_login_case", fake_run_case)
    monkeypatch.setattr(streamlit_app, "sync_playwright", fake_sync_playwright)

    result_path = streamlit_app._execute_case("login_fixture", "rule", suite="login")
    saved = json.loads(result_path.read_text(encoding="utf-8"))

    assert saved["case_id"] == "login_fixture"
    assert calls[0][0] is browser
    assert calls[0][1] is case
    assert calls[0][3] == {"policy": "rule"}
    browser.close.assert_called_once_with()


def test_single_cart_run_passes_model_decider_to_cart_evaluator(tmp_path: Path, monkeypatch) -> None:
    case = SimpleNamespace(
        case_id="cart_fixture", task="把一个茶杯加入购物车。", max_steps=5, max_seconds=10,
    )
    browser = Mock()
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=Mock(return_value=browser)),
    )
    calls: list[tuple[object, object, Path, dict[str, object]]] = []

    @contextmanager
    def fake_sync_playwright():
        yield playwright

    def fake_run_case(actual_browser, actual_case, output_dir, **kwargs):
        calls.append((actual_browser, actual_case, output_dir, kwargs))
        return {"case_id": actual_case.case_id, "task_success": True}

    decider = Mock()
    decider.wait_for_request_capacity.return_value = 0.0
    monkeypatch.setattr(streamlit_app, "UI_RUNS", tmp_path / "ui_runs")
    monkeypatch.setattr(streamlit_app, "HISTORY_DB", tmp_path / "history.sqlite3")
    monkeypatch.setattr(streamlit_app, "load_cart_cases", lambda: [case])
    monkeypatch.setattr(streamlit_app, "run_cart_case", fake_run_case)
    monkeypatch.setattr(streamlit_app, "OpenAIDecider", SimpleNamespace(from_env=Mock(return_value=decider)))
    monkeypatch.setattr(streamlit_app, "sync_playwright", fake_sync_playwright)

    result_path = streamlit_app._execute_case("cart_fixture", "model", suite="cart")

    assert result_path.is_file()
    assert calls[0][3] == {"policy": "model", "decider": decider}


def test_model_run_rejects_step_budget_exceeding_local_rpm_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = SimpleNamespace(
        case_id="cart_fixture", task="加商品并核对总价。", max_steps=5, max_seconds=10,
    )
    monkeypatch.setattr(streamlit_app, "load_cart_cases", lambda: [case])

    with pytest.raises(ValueError, match="1 到 13"):
        streamlit_app._execute_case(
            "cart_fixture", "model", suite="cart", max_steps=14,
        )
