"""SQLite 运行索引的写入、筛选和重复登记行为。"""

import json

from webprobe.run_history import index_eval_summary, index_single_result, list_runs


def test_index_single_result_and_infer_suite_without_copying_steps(tmp_path) -> None:
    run_dir = tmp_path / "ui_run_001"
    run_dir.mkdir()
    result_path = run_dir / "result.json"
    result_path.write_text(json.dumps({
        "task": "检查商品总价。", "policy": "rule", "task_success": True,
        "agent_conclusion": "no_bug", "run_status": "completed",
        "cart_assertion": {"passed": True}, "steps": [{"action": "open"}],
    }), encoding="utf-8")
    db_path = tmp_path / "history.sqlite3"

    record = index_single_result(result_path, db_path=db_path)
    rows = list_runs(run_type="single", suite="cart", db_path=db_path)

    assert record.suite == "cart"
    assert record.task_success_count == 1
    assert rows == [record]
    stored = json.loads(result_path.read_text(encoding="utf-8"))
    assert "steps" in stored  # full trace/evidence stays in the JSON artifact


def test_index_eval_summary_is_idempotent_and_filterable(tmp_path) -> None:
    run_dir = tmp_path / "eval_run_001"
    run_dir.mkdir()
    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps({
        "dataset": "login_v1.jsonl", "policy": "login_rule_recheck_v1",
        "case_count": 4, "task_success_count": 4, "task_success_rate": 1.0,
        "evaluation_scope": "full_suite",
    }), encoding="utf-8")
    db_path = tmp_path / "history.sqlite3"

    first = index_eval_summary(summary_path, db_path=db_path)
    second = index_eval_summary(summary_path, db_path=db_path)
    rows = list_runs(run_type="eval", dataset="login_v1.jsonl", db_path=db_path)

    assert first.run_id == second.run_id
    assert len(rows) == 1
    assert rows[0].suite == "login"
    assert rows[0].case_count == 4
    assert rows[0].task_success_rate == 1.0


def test_list_runs_rejects_unbounded_limit(tmp_path) -> None:
    try:
        list_runs(limit=1_001, db_path=tmp_path / "history.sqlite3")
    except ValueError as error:
        assert "limit" in str(error)
    else:
        raise AssertionError("limit 太大时应该报错")
