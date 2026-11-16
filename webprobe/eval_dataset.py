"""固定评测题目：读取人工编写的 JSONL，并检查标准答案是否自洽。

先看 EvalCase 的字段，再看 load_cases 如何把一行 JSON 转为一个 Case。
本模块不打开浏览器，也不计算 Agent 分数。
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET = PROJECT_ROOT / "evals" / "datasets" / "search_v3.jsonl"


@dataclass(frozen=True)
class EvalCase:
    """人工固定的任务与 Ground Truth；运行中的页面不能修改这些字段。"""

    case_id: str
    task: str
    bug_mode: str | None
    load_delay_ms: int
    keyword: str
    expected_products: tuple[str, ...]
    expected_status: str
    stock_status: str
    verification_condition: str
    success_condition: str
    has_bug: bool
    bug_type: str | None
    max_steps: int
    max_seconds: int


def load_cases(path: Path = DATASET) -> list[EvalCase]:
    """逐行读取 JSONL，并拒绝不认识的故障开关或重复 Case ID。"""
    cases: list[EvalCase] = []
    seen_ids: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        data = json.loads(line)
        case_id = data["case_id"]
        bug_mode = data["initial_state"]["bug_mode"]
        load_delay_ms = data["initial_state"].get("load_delay_ms", 0)
        truth = data["ground_truth"]
        expected = data["expected_behavior"]
        if not re.fullmatch(r"[a-z0-9_]+", case_id) or case_id in seen_ids:
            raise ValueError(f"第 {line_number} 行的 Case ID 无效或重复")
        if bug_mode not in (None, "search_mismatch"):
            raise ValueError(f"第 {line_number} 行使用了未知故障开关")
        if type(load_delay_ms) is not int or not 0 <= load_delay_ms <= 3000:
            raise ValueError(f"第 {line_number} 行的加载延迟无效")
        if (bug_mode is not None) != truth["has_bug"]:
            raise ValueError(f"第 {line_number} 行的故障开关与 Ground Truth 矛盾")
        expected_success_condition = (
            "report_search_result_mismatch" if truth["has_bug"] else "complete_without_bug"
        )
        if (data["verification_condition"] != "visible_products_equal_fixed_spec"
                or data["success_condition"] != expected_success_condition):
            raise ValueError(f"第 {line_number} 行的验证或成功条件无效")
        if not expected["visible_products"] or data["max_steps"] < 1 or data["max_seconds"] < 1:
            raise ValueError(f"第 {line_number} 行的预期商品或预算无效")
        cases.append(EvalCase(
            case_id=case_id,
            task=data["task"],
            bug_mode=bug_mode,
            load_delay_ms=load_delay_ms,
            keyword=expected["keyword"],
            expected_products=tuple(expected["visible_products"]),
            expected_status=expected["status"],
            stock_status=expected["stock_status"],
            verification_condition=data["verification_condition"],
            success_condition=data["success_condition"],
            has_bug=truth["has_bug"],
            bug_type=truth["bug_type"],
            max_steps=data["max_steps"],
            max_seconds=data["max_seconds"],
        ))
        seen_ids.add(case_id)
    if not cases:
        raise ValueError("评测数据集不能为空")
    return cases
