"""购物车 Dataset、Agent 循环及独立 Evals 的端到端验证。"""

import json

from webprobe.cart_dataset import load_cart_cases
from webprobe.cart_evals import run_cart_suite


def test_cart_dataset_has_fixed_and_self_consistent_ground_truth() -> None:
    cases = load_cart_cases()
    assert len(cases) == 7
    assert sum(case.has_bug for case in cases) == 2
    assert next(case for case in cases if case.has_bug).bug_type == "wrong_cart_total"


def test_cart_eval_suite_reproduces_bug_and_saves_evidence(tmp_path) -> None:
    run_dir = run_cart_suite(output_root=tmp_path, policy="rule")
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert summary["case_count"] == 7
    assert summary["confusion"] == {"tp": 2, "fp": 0, "fn": 0, "tn": 5}
    assert summary["task_success_count"] == 7
    assert all((run_dir / f"{row['case_id']}.png").is_file() for row in rows)
    assert all((run_dir / f"{row['case_id']}.zip").is_file() for row in rows)
    bug = next(row for row in rows if row["ground_truth"]["has_bug"])
    assert bug["agent_conclusion"] == "bug"
    assert bug["reproduced_in_session"] is True
    assert bug["cart_assertion"]["actual_total"] == "78"


def test_cart_baseline_keeps_single_mismatch_unclassified(tmp_path) -> None:
    run_dir = run_cart_suite(
        output_root=tmp_path, policy="baseline", case_id="cart_wrong_total_001",
    )
    row = json.loads((run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()[0])
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))

    assert row["run_status"] == "needs_review"
    assert row["agent_conclusion"] is None
    assert row["task_success"] is False
    assert summary["unclassified_case_count"] == 1
    assert summary["confusion"] == {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
