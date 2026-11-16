"""读取购物车固定评测 Case，并独立校验 Ground Truth 与价格规格。"""

import json
import re
from dataclasses import dataclass
from pathlib import Path


CART_DATASET = Path(__file__).resolve().parents[1] / "evals" / "datasets" / "cart_v1.jsonl"
PRODUCT_PRICES = {"蓝色茶杯": 39, "帆布包": 59}


@dataclass(frozen=True)
class CartCase:
    """一题的用户目标、初始状态、预期购物车和人工 Ground Truth。"""

    case_id: str
    task: str
    bug_mode: str | None
    items: tuple[tuple[str, int], ...]
    expected_count: int
    expected_total: int
    expected_items: tuple[tuple[str, int], ...]
    verification_condition: str
    success_condition: str
    has_bug: bool
    bug_type: str | None
    max_steps: int
    max_seconds: int


def load_cart_cases(path: Path = CART_DATASET) -> list[CartCase]:
    """加载 JSONL；不认识的产品、价格算错或标签矛盾都会立即报错。"""
    cases: list[CartCase] = []
    seen_ids: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        data = json.loads(line)
        initial = data["initial_state"]
        expected = data["expected_behavior"]
        truth = data["ground_truth"]
        case_id = data["case_id"]
        bug_mode = initial["bug_mode"]
        if not re.fullmatch(r"[a-z0-9_]+", case_id) or case_id in seen_ids:
            raise ValueError(f"第 {line_number} 行的 Case ID 无效或重复")
        if bug_mode not in (None, "wrong_total"):
            raise ValueError(f"第 {line_number} 行使用了未知故障开关")
        raw_items = initial["items"]
        items = tuple((row["name"], row["quantity"]) for row in raw_items)
        if not items or any(name not in PRODUCT_PRICES for name, _ in items):
            raise ValueError(f"第 {line_number} 行包含空商品列表或未知商品")
        if len({name for name, _ in items}) != len(items):
            raise ValueError(f"第 {line_number} 行重复定义了同一商品")
        if any(type(quantity) is not int or not 1 <= quantity <= 5 for _, quantity in items):
            raise ValueError(f"第 {line_number} 行商品数量必须是 1 到 5 的整数")
        count = sum(quantity for _, quantity in items)
        total = sum(PRODUCT_PRICES[name] * quantity for name, quantity in items)
        expected_items = tuple((row[0], row[1]) for row in expected["items"])
        if (expected["count"] != count or expected["total"] != total
                or type(expected["count"]) is not int
                or type(expected["total"]) is not int
                or expected_items != items):
            raise ValueError(f"第 {line_number} 行的预期购物车与固定价格规格不符")
        has_bug = bug_mode == "wrong_total"
        bug_type = "wrong_cart_total" if has_bug else None
        if truth["has_bug"] is not has_bug or truth["bug_type"] != bug_type:
            raise ValueError(f"第 {line_number} 行的故障开关与 Ground Truth 矛盾")
        success = "report_reproduced_wrong_cart_total" if has_bug else "complete_without_bug"
        if (data["verification_condition"] != "cart_items_count_and_total_match_fixed_price_spec"
                or data["success_condition"] != success):
            raise ValueError(f"第 {line_number} 行的验证条件或成功条件无效")
        if (type(data["max_steps"]) is not int or data["max_steps"] < 1
                or type(data["max_seconds"]) is not int or data["max_seconds"] < 1):
            raise ValueError(f"第 {line_number} 行的执行预算无效")
        cases.append(CartCase(
            case_id=case_id, task=data["task"], bug_mode=bug_mode, items=items,
            expected_count=count, expected_total=total, expected_items=items,
            verification_condition=data["verification_condition"],
            success_condition=data["success_condition"], has_bug=has_bug,
            bug_type=bug_type, max_steps=data["max_steps"], max_seconds=data["max_seconds"],
        ))
        seen_ids.add(case_id)
    if not cases:
        raise ValueError("购物车评测数据集不能为空")
    return cases
