"""校验 README 引用的版本化 Evals 汇总未与固定数据集脱节。"""

import hashlib
import json
import math
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = PROJECT_ROOT / "evals" / "reports" / "2026-11-16_rule_comparison.json"


def test_published_rule_report_matches_datasets_and_case_level_metrics() -> None:
    report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    assert report["experiment_type"] == "local_rule_baseline_vs_recheck"
    assert report["comparison_controls"]["model_used"] is False
    assert sum(suite["case_count"] for suite in report["suites"]) == 30

    for suite in report["suites"]:
        dataset_path = PROJECT_ROOT / "evals" / "datasets" / suite["dataset"]
        dataset_bytes = dataset_path.read_bytes()
        assert hashlib.sha256(dataset_bytes).hexdigest() == suite["dataset_sha256"]
        cases = [
            json.loads(line)
            for line in dataset_bytes.decode("utf-8").splitlines()
            if line.strip()
        ]
        positive_cases = sum(case["ground_truth"]["has_bug"] is True for case in cases)
        negative_cases = len(cases) - positive_cases
        assert len(cases) == suite["case_count"]

        for policy_result in (suite["baseline"], suite["current"]):
            confusion = policy_result["confusion"]
            classified = sum(confusion.values())
            unclassified = policy_result["unclassified_case_count"]

            assert classified + unclassified == len(cases)
            assert confusion["tp"] + confusion["fn"] <= positive_cases
            assert confusion["fp"] + confusion["tn"] <= negative_cases
            assert policy_result["task_success_count"] <= len(cases)
            assert math.isclose(
                policy_result["task_success_rate"],
                policy_result["task_success_count"] / len(cases),
                rel_tol=0,
                abs_tol=1e-9,
            )

        assert (
            suite["baseline"]["policy_source_sha256"]
            == suite["current"]["policy_source_sha256"]
        )
