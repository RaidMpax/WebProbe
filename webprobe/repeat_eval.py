"""重复运行同一份规则 Evals，观察单次耗时结论是否稳定。

每轮交替安排两种策略的先后顺序；每次仍由 evals.run_suite 创建新浏览器
上下文并保存原始轨迹。这里只做描述性汇总，不计算统计显著性。
"""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from uuid import uuid4

from webprobe.evals import (
    BASELINE_POLICY_NAME, POLICY_NAME, PROJECT_ROOT, SEARCH_WAIT_MS, run_suite,
)


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def summarize_repeats(pairs: list[dict[str, object]]) -> dict[str, object]:
    """从已落盘的每轮结果汇总；数据集、源码或 Case 顺序不同就拒绝比较。"""
    if not pairs:
        raise ValueError("至少需要一轮已完成的对照结果")
    trial_rows: list[dict[str, object]] = []
    baseline_rates: list[float] = []
    current_rates: list[float] = []
    latency_deltas: list[float] = []
    per_case: dict[str, dict[str, int]] = {}
    dataset_name: str | None = None
    dataset_hash: str | None = None
    source_hash: str | None = None
    expected_case_ids: list[str] | None = None

    for pair in pairs:
        baseline_dir = Path(pair["baseline_dir"])
        current_dir = Path(pair["current_dir"])
        baseline = json.loads((baseline_dir / "summary.json").read_text(encoding="utf-8"))
        current = json.loads((current_dir / "summary.json").read_text(encoding="utf-8"))
        before = _read_jsonl(baseline_dir / "results.jsonl")
        after = _read_jsonl(current_dir / "results.jsonl")
        before_ids = [result["case_id"] for result in before]
        after_ids = [result["case_id"] for result in after]

        if (baseline["policy"] != BASELINE_POLICY_NAME
                or current["policy"] != POLICY_NAME
                or baseline["search_wait_ms"] != SEARCH_WAIT_MS["rule_baseline"]
                or current["search_wait_ms"] != SEARCH_WAIT_MS["rule"]):
            raise ValueError("策略或等待参数与对照设计不一致")
        if (baseline["dataset"] != current["dataset"]
                or baseline["dataset_sha256"] != current["dataset_sha256"]
                or baseline["policy_source_sha256"] != current["policy_source_sha256"]):
            raise ValueError("同轮对照的数据集或源码不一致")
        if dataset_name is None:
            dataset_name = baseline["dataset"]
            dataset_hash = baseline["dataset_sha256"]
            source_hash = baseline["policy_source_sha256"]
            expected_case_ids = before_ids
            per_case = {case_id: {"baseline": 0, "current": 0} for case_id in before_ids}
        if (baseline["dataset"] != dataset_name
                or baseline["dataset_sha256"] != dataset_hash
                or baseline["policy_source_sha256"] != source_hash
                or before_ids != expected_case_ids or after_ids != expected_case_ids):
            raise ValueError("不同轮次的数据集、源码或 Case 顺序不一致")

        for baseline_result, current_result in zip(before, after):
            case_id = baseline_result["case_id"]
            per_case[case_id]["baseline"] += baseline_result["task_success"] is True
            per_case[case_id]["current"] += current_result["task_success"] is True
        baseline_rates.append(baseline["task_success_rate"])
        current_rates.append(current["task_success_rate"])
        delta = round(
            current["average_latency_seconds"] - baseline["average_latency_seconds"], 3
        )
        latency_deltas.append(delta)
        trial_rows.append({
            "trial": pair["trial"],
            "run_order": pair["run_order"],
            "baseline_dir": str(baseline_dir.resolve()),
            "current_dir": str(current_dir.resolve()),
            "baseline_task_success_rate": baseline_rates[-1],
            "current_task_success_rate": current_rates[-1],
            "baseline_average_latency_seconds": baseline["average_latency_seconds"],
            "current_average_latency_seconds": current["average_latency_seconds"],
            "latency_delta_seconds": delta,
            "baseline_bad_case_count": baseline["bad_case_count"],
            "current_bad_case_count": current["bad_case_count"],
        })

    return {
        "dataset": dataset_name,
        "dataset_sha256": dataset_hash,
        "policy_source_sha256": source_hash,
        "repeat_runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "trial_count": len(pairs),
        "search_wait_ms": {
            "baseline": SEARCH_WAIT_MS["rule_baseline"], "current": SEARCH_WAIT_MS["rule"],
        },
        "trials": trial_rows,
        "task_success_rate_by_trial": {
            "baseline": baseline_rates, "current": current_rates,
        },
        "latency_delta_seconds_by_trial": latency_deltas,
        "median_latency_delta_seconds": median(latency_deltas),
        "latency_delta_range_seconds": [min(latency_deltas), max(latency_deltas)],
        "per_case_success_count": per_case,
        "note": "每个 Case 每种策略只运行少量次数；这些是描述性结果，不能证明统计显著性或真实模型能力。",
    }


def run_repeated_comparison(trials: int = 3) -> Path:
    """交替运行两种规则策略，并返回本次重复实验的汇总文件。"""
    if not 2 <= trials <= 5:
        raise ValueError("trials 需在 2 到 5 之间")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid4().hex[:8]
    output_dir = PROJECT_ROOT / "artifacts" / "repeats" / run_id
    output_dir.mkdir(parents=True)
    pairs: list[dict[str, object]] = []
    for number in range(1, trials + 1):
        order = ("rule_baseline", "rule") if number % 2 else ("rule", "rule_baseline")
        run_dirs = {policy: run_suite(policy=policy) for policy in order}
        pairs.append({
            "trial": number,
            "run_order": list(order),
            "baseline_dir": str(run_dirs["rule_baseline"]),
            "current_dir": str(run_dirs["rule"]),
        })
        (output_dir / "progress.json").write_text(
            json.dumps({"requested_trials": trials, "completed_pairs": pairs},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"已完成第 {number}/{trials} 轮", flush=True)
    report_path = output_dir / "repeated_comparison.json"
    report_path.write_text(
        json.dumps(summarize_repeats(pairs), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="重复运行本地四案规则对照")
    parser.add_argument("--trials", type=int, default=3)
    args = parser.parse_args()
    print(run_repeated_comparison(args.trials))
