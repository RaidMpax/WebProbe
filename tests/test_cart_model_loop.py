"""购物车模型循环的离线集成测试：假模型响应 + 真实本地 Chromium。"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from playwright.sync_api import sync_playwright

from webprobe.cart_dataset import load_cart_cases
from webprobe.cart_evals import run_cart_case
from webprobe.llm_decider import OpenAIDecider


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _fake_client(actions: list[str]) -> Mock:
    client = Mock()
    replies = []
    for index, action in enumerate(actions, start=1):
        replies.append(SimpleNamespace(
            output=[SimpleNamespace(
                type="function_call",
                name="choose_browser_action",
                arguments=json.dumps({"action": action, "summary": f"选择 {action}"}),
                call_id=f"cart-test-{index}",
            )],
            usage=SimpleNamespace(
                input_tokens=10, output_tokens=2, total_tokens=12,
            ),
        ))
    client.responses.create.side_effect = replies
    return client


def _run_case(case_id: str, actions: list[str], output_dir: Path):
    case = next(case for case in load_cart_cases() if case.case_id == case_id)
    decider = OpenAIDecider(_fake_client(actions), "offline-test-model", task_kind="cart")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            return run_cart_case(browser, case, output_dir, policy="model", decider=decider)
        finally:
            browser.close()


def test_model_cart_completes_normal_case_and_records_tokens(tmp_path) -> None:
    result = _run_case(
        "cart_single_bag_001", ["open", "add_bag", "stop"], tmp_path,
    )

    assert result["run_status"] == "completed"
    assert result["agent_conclusion"] == "no_bug"
    assert result["task_success"] is True
    assert result["model_calls"] == 3
    assert result["token_usage"] == {
        "input_tokens": 30, "output_tokens": 6, "total_tokens": 36,
    }
    assert (tmp_path / "cart_single_bag_001.zip").is_file()


def test_model_report_is_only_a_suspicion_until_python_replays_wrong_total(tmp_path) -> None:
    result = _run_case(
        "cart_wrong_total_001",
        ["open", "add_cup", "add_bag", "report_bug"], tmp_path,
    )

    assert result["run_status"] == "suspected_bug"
    assert result["agent_conclusion"] == "bug"
    assert result["reported_bug_type"] == "wrong_cart_total"
    assert result["reproduced_in_session"] is True
    assert result["task_success"] is True
    assert result["cart_assertion"]["actual_total"] == "78"
    assert [step["action"] for step in result["steps"]] == [
        "open", "add_cup", "add_bag", "open", "add_cup", "add_bag",
    ]
    assert len(result["recoveries"]) == 1


def test_python_rejects_false_bug_report_when_cart_matches_the_spec(tmp_path) -> None:
    result = _run_case(
        "cart_single_bag_001", ["open", "add_bag", "report_bug"], tmp_path,
    )

    assert result["run_status"] == "completed"
    assert result["agent_conclusion"] == "no_bug"
    assert result["task_success"] is True
    assert any("已拒绝误报" in line for line in result["decision_summaries"])


def test_model_provider_error_text_is_saved_in_eval_record(tmp_path) -> None:
    case = next(case for case in load_cart_cases() if case.case_id == "cart_single_bag_001")
    client = Mock()
    client.responses.create.return_value = SimpleNamespace(
        output=[SimpleNamespace(
            type="function_call", name="choose_browser_action",
            arguments=json.dumps({"action": "not_allowed", "summary": "无效"}),
            call_id="invalid-cart-call",
        )],
        usage=None,
    )
    decider = OpenAIDecider(client, "offline-test-model", task_kind="cart")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            result = run_cart_case(
                browser, case, tmp_path, policy="model", decider=decider,
            )
        finally:
            browser.close()

    assert result["run_status"] == "model_failed"
    assert result["agent_conclusion"] is None
    assert result["model_error_type"] == "invalid_model_response"
    assert result["error"]
