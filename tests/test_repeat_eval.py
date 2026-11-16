"""只测试重复实验的汇总与可比性检查；浏览器运行另行验证。"""

import json
from pathlib import Path

import pytest

from webprobe.evals import BASELINE_POLICY_NAME, POLICY_NAME
from webprobe.repeat_eval import run_repeated_comparison, summarize_repeats


def write_fake_run(
    root: Path, name: str, policy: str, wait_ms: int,
    successes: tuple[bool, bool], latency: float, dataset_hash: str = "same",
) -> Path:
    run_dir = root / name
    run_dir.mkdir()
    summary = {
        "policy": policy,
        "search_wait_ms": wait_ms,
        "dataset": "search_v3.jsonl",
        "dataset_sha256": dataset_hash,
        "policy_source_sha256": "same-code",
        "task_success_rate": sum(successes) / len(successes),
        "average_latency_seconds": latency,
        "bad_case_count": len(successes) - sum(successes),
    }
    (run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    results = [
        {"case_id": case_id, "task_success": success}
        for case_id, success in zip(("normal", "slow"), successes)
    ]
    (run_dir / "results.jsonl").write_text(
        "\n".join(json.dumps(result) for result in results) + "\n",
        encoding="utf-8",
    )
    return run_dir


def test_repeated_summary_keeps_per_trial_and_per_case_evidence(tmp_path: Path) -> None:
    pairs = []
    for number, (before_time, after_time) in enumerate(
        ((2.0, 2.5), (2.2, 2.9), (2.4, 2.7)), 1
    ):
        baseline = write_fake_run(
            tmp_path, f"baseline_{number}", BASELINE_POLICY_NAME, 2500,
            (True, False), before_time,
        )
        current = write_fake_run(
            tmp_path, f"current_{number}", POLICY_NAME, 3500,
            (True, True), after_time,
        )
        pairs.append({
            "trial": number,
            "run_order": ["rule_baseline", "rule"] if number % 2 else ["rule", "rule_baseline"],
            "baseline_dir": str(baseline),
            "current_dir": str(current),
        })

    report = summarize_repeats(pairs)
    assert report["trial_count"] == 3
    assert report["task_success_rate_by_trial"] == {
        "baseline": [0.5, 0.5, 0.5], "current": [1.0, 1.0, 1.0]
    }
    assert report["latency_delta_seconds_by_trial"] == [0.5, 0.7, 0.3]
    assert report["median_latency_delta_seconds"] == 0.5
    assert report["latency_delta_range_seconds"] == [0.3, 0.7]
    assert report["per_case_success_count"]["slow"] == {"baseline": 0, "current": 3}
    assert report["trials"][1]["run_order"] == ["rule", "rule_baseline"]


def test_repeated_summary_rejects_changed_dataset(tmp_path: Path) -> None:
    baseline = write_fake_run(
        tmp_path, "baseline", BASELINE_POLICY_NAME, 2500, (True, False), 2.0
    )
    current = write_fake_run(
        tmp_path, "current", POLICY_NAME, 3500, (True, True), 2.5,
        dataset_hash="changed",
    )
    with pytest.raises(ValueError, match="数据集或源码不一致"):
        summarize_repeats([{
            "trial": 1, "run_order": ["rule_baseline", "rule"],
            "baseline_dir": str(baseline), "current_dir": str(current),
        }])


def test_repeat_count_has_small_explicit_bounds() -> None:
    with pytest.raises(ValueError, match="2 到 5"):
        run_repeated_comparison(1)
