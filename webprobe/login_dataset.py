"""读取登录固定数据集，并用本地账号规格验证每题标准答案。"""

import json
import re
from dataclasses import dataclass
from pathlib import Path


LOGIN_DATASET = Path(__file__).resolve().parents[1] / "evals" / "datasets" / "login_v1.jsonl"
TEST_USERNAME = "qa_student"
TEST_PASSWORD = "qa_demo_123"
SUCCESS_STATUS = "登录成功：欢迎 QA 测试用户"
REJECTED_STATUS = "账号或密码错误"


@dataclass(frozen=True)
class LoginCase:
    case_id: str
    task: str
    bug_mode: str | None
    page_variant: str | None
    username: str
    password: str
    expected_authenticated: bool
    expected_status: str
    verification_condition: str
    success_condition: str
    has_bug: bool
    bug_type: str | None
    max_steps: int
    max_seconds: int


def load_login_cases(path: Path = LOGIN_DATASET) -> list[LoginCase]:
    """拒绝无效账号、故障标签矛盾、重复 ID 和与规格不符的预期状态。"""
    cases: list[LoginCase] = []
    seen_ids: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        data = json.loads(line)
        initial = data["initial_state"]
        expected = data["expected_behavior"]
        truth = data["ground_truth"]
        case_id = data["case_id"]
        username, password = initial["username"], initial["password"]
        bug_mode, page_variant = initial["bug_mode"], initial["page_variant"]
        if not re.fullmatch(r"[a-z0-9_]+", case_id) or case_id in seen_ids:
            raise ValueError(f"第 {line_number} 行的 Case ID 无效或重复")
        if bug_mode not in (None, "valid_login_rejected"):
            raise ValueError(f"第 {line_number} 行使用了未知登录故障")
        if page_variant not in (None, "renamed_button"):
            raise ValueError(f"第 {line_number} 行使用了未知页面变化")
        if not isinstance(username, str) or not isinstance(password, str):
            raise ValueError(f"第 {line_number} 行的账号和密码必须是字符串")
        authenticated = username == TEST_USERNAME and password == TEST_PASSWORD
        expected_status = SUCCESS_STATUS if authenticated else REJECTED_STATUS
        if (type(expected["authenticated"]) is not bool
                or expected["authenticated"] is not authenticated
                or expected["status"] != expected_status):
            raise ValueError(f"第 {line_number} 行预期登录状态与固定账号规格不符")
        has_bug = bug_mode == "valid_login_rejected"
        bug_type = "valid_login_rejected" if has_bug else None
        if (truth["has_bug"] is not has_bug or truth["bug_type"] != bug_type
                or (has_bug and not authenticated)):
            raise ValueError(f"第 {line_number} 行故障标签与账号状态矛盾")
        success = "report_reproduced_valid_login_rejection" if has_bug else "complete_without_bug"
        if (data["verification_condition"] != "login_state_and_status_match_credentials_spec"
                or data["success_condition"] != success):
            raise ValueError(f"第 {line_number} 行的验证条件或成功条件无效")
        if (type(data["max_steps"]) is not int or data["max_steps"] < 1
                or type(data["max_seconds"]) is not int or data["max_seconds"] < 1):
            raise ValueError(f"第 {line_number} 行执行预算无效")
        cases.append(LoginCase(
            case_id=case_id, task=data["task"], bug_mode=bug_mode,
            page_variant=page_variant, username=username, password=password,
            expected_authenticated=authenticated, expected_status=expected_status,
            verification_condition=data["verification_condition"],
            success_condition=data["success_condition"], has_bug=has_bug,
            bug_type=bug_type, max_steps=data["max_steps"], max_seconds=data["max_seconds"],
        ))
        seen_ids.add(case_id)
    if not cases:
        raise ValueError("登录评测集不能为空")
    return cases
