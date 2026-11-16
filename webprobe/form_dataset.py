"""读取反馈表单的固定 Case；这里只校验题目，不运行 Agent。"""

import json
import re
from dataclasses import dataclass
from pathlib import Path


FORM_DATASET = Path(__file__).resolve().parents[1] / "evals" / "datasets" / "feedback_v3.jsonl"


@dataclass(frozen=True)
class FormCase:
    """一条表单题目：输入、产品规格、Ground Truth 和执行预算。"""

    case_id: str
    task: str
    bug_mode: str | None
    page_variant: str | None
    name: str
    message: str
    expected_submitted: bool
    expected_status: str
    verification_condition: str
    success_condition: str
    has_bug: bool
    bug_type: str | None
    max_steps: int
    max_seconds: int


def load_form_cases(path: Path = FORM_DATASET) -> list[FormCase]:
    """把 JSONL 的每行变成 FormCase，并拒绝与固定规格矛盾的答案。"""
    cases: list[FormCase] = []
    seen_ids: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        data = json.loads(line)
        case_id = data["case_id"]
        initial = data["initial_state"]
        expected = data["expected_behavior"]
        truth = data["ground_truth"]
        bug_mode = initial["bug_mode"]
        page_variant = initial.get("page_variant")
        name, message = initial["name"], initial["message"]

        if not re.fullmatch(r"[a-z0-9_]+", case_id) or case_id in seen_ids:
            raise ValueError(f"第 {line_number} 行的 Case ID 无效或重复")
        if bug_mode not in (None, "required_bypass"):
            raise ValueError(f"第 {line_number} 行使用了未知故障开关")
        if page_variant not in (None, "renamed_submit"):
            raise ValueError(f"第 {line_number} 行使用了未知页面变化")
        if not isinstance(name, str) or not isinstance(message, str):
            raise ValueError(f"第 {line_number} 行的表单输入必须是字符串")
        should_submit = bool(name.strip() and message.strip())
        expected_status = "提交成功" if should_submit else "请填写所有必填项"
        if (type(expected["submitted"]) is not bool
                or expected["submitted"] != should_submit
                or expected["status"] != expected_status):
            raise ValueError(f"第 {line_number} 行的预期行为与必填规格矛盾")
        has_bug = bug_mode == "required_bypass"
        bug_type = "required_field_bypass" if has_bug else None
        if (truth["has_bug"] is not has_bug or truth["bug_type"] != bug_type
                or (has_bug and should_submit)):
            raise ValueError(f"第 {line_number} 行的故障开关与 Ground Truth 矛盾")
        success_condition = "report_required_field_bypass" if has_bug else "complete_without_bug"
        if (data["verification_condition"] != "submission_matches_required_spec"
                or data["success_condition"] != success_condition):
            raise ValueError(f"第 {line_number} 行的验证或成功条件无效")
        if (type(data["max_steps"]) is not int or data["max_steps"] < 1
                or type(data["max_seconds"]) is not int or data["max_seconds"] < 1):
            raise ValueError(f"第 {line_number} 行的执行预算无效")

        cases.append(FormCase(
            case_id=case_id,
            task=data["task"],
            bug_mode=bug_mode,
            page_variant=page_variant,
            name=name,
            message=message,
            expected_submitted=expected["submitted"],
            expected_status=expected["status"],
            verification_condition=data["verification_condition"],
            success_condition=data["success_condition"],
            has_bug=has_bug,
            bug_type=bug_type,
            max_steps=data["max_steps"],
            max_seconds=data["max_seconds"],
        ))
        seen_ids.add(case_id)
    if not cases:
        raise ValueError("表单评测数据集不能为空")
    return cases
