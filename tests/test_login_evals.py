"""登录 Evals 的 Ground Truth、复现证据与 Baseline 对照。"""

import json

from webprobe.login_dataset import load_login_cases
from webprobe.login_evals import run_login_suite


def test_login_dataset_matches_fixed_account_rules() -> None:
    cases = load_login_cases()
    assert len(cases) == 6
    assert sum(case.has_bug for case in cases) == 2
    assert sum(case.page_variant == "renamed_button" for case in cases) == 2


def test_login_current_policy_detects_only_reproduced_valid_credential_failure(tmp_path) -> None:
    run_dir = run_login_suite(output_root=tmp_path, policy="rule")
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert summary["task_success_count"] == 6
    assert summary["confusion"] == {"tp": 2, "fp": 0, "fn": 0, "tn": 4}
    assert all((run_dir / f"{row['case_id']}.zip").is_file() for row in rows)
    bug = next(row for row in rows if row["ground_truth"]["has_bug"])
    assert bug["reproduced_in_session"] is True
    assert bug["login_assertion"]["actual_state"] == "rejected"


def test_login_baseline_keeps_one_valid_login_rejection_unclassified(tmp_path) -> None:
    run_dir = run_login_suite(
        output_root=tmp_path, policy="baseline", case_id="login_valid_rejected_001",
    )
    row = json.loads((run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()[0])

    assert row["run_status"] == "needs_review"
    assert row["agent_conclusion"] is None
    assert row["task_success"] is False
