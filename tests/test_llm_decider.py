"""用假响应测试模型接口格式；不把它当成真实模型评测。"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from playwright.sync_api import sync_playwright

from webprobe.browser_tools import BrowserTools
from webprobe.demo_loop import DemoGoal, execute_action
from webprobe.cart_loop import CartGoal
from webprobe.llm_decider import CART_ACTION_NAMES, ModelDecisionError, OpenAIDecider
from webprobe.llm_decider import (
    _GEMINI_REQUEST_TIMESTAMPS, _load_env_file,
    _wait_for_gemini_request_capacity,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GOAL = DemoGoal(
    search_url=(PROJECT_ROOT / "lab" / "search.html").as_uri(),
    detail_url=(PROJECT_ROOT / "lab" / "cup.html").as_uri(),
    keyword="茶杯",
    product_name="蓝色茶杯",
    search_status="找到 1 件商品",
    stock_status="库存：有货",
)


def fake_client(arguments: dict[str, str]) -> Mock:
    client = Mock()
    client.responses.create.return_value = SimpleNamespace(
        output=[
            SimpleNamespace(
                type="function_call",
                name="choose_browser_action",
                arguments=json.dumps(arguments, ensure_ascii=False),
                call_id="offline-test-call",
            )
        ]
    )
    return client


def test_valid_model_action_can_drive_one_local_browser_step() -> None:
    client = fake_client({"action": "fill", "summary": "输入搜索关键词"})
    decider = OpenAIDecider(client, "offline-test-model")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            tools = BrowserTools(browser.new_page(), {GOAL.search_url, GOAL.detail_url})
            initial = tools.open(GOAL.search_url)
            decision = decider.choose(GOAL, initial, {"open"})
            updated = execute_action(tools, decision.action, GOAL)
            assert updated.search_value == "茶杯"
            assert decision.summary == "输入搜索关键词"
            assert client.responses.create.call_args.kwargs["parallel_tool_calls"] is False
        finally:
            browser.close()


def test_unknown_model_action_is_rejected_before_browser_execution() -> None:
    client = fake_client({"action": "delete_data", "summary": "删除数据"})
    decider = OpenAIDecider(client, "offline-test-model")
    with pytest.raises(ModelDecisionError, match="无效"):
        decider.choose(GOAL, None, set())


def test_request_timeout_is_forwarded_to_openai_client() -> None:
    client = fake_client({"action": "stop", "summary": "完成"})
    decider = OpenAIDecider(client, "offline-test-model")

    decider.choose(GOAL, None, set(), timeout_seconds=3.5)

    assert client.responses.create.call_args.kwargs["timeout"] == 3.5


def test_form_decider_rejects_search_goal_before_api_call() -> None:
    client = fake_client({"action": "open", "summary": "打开表单"})
    decider = OpenAIDecider(client, "offline-test-model", task_kind="form")
    with pytest.raises(ModelDecisionError, match="目标类型不一致"):
        decider.choose(GOAL, None, set())
    client.responses.create.assert_not_called()


def test_cart_decider_uses_cart_action_schema_and_rejects_search_goal() -> None:
    client = fake_client({"action": "add_cup", "summary": "添加茶杯"})
    decider = OpenAIDecider(client, "offline-test-model", task_kind="cart")
    with pytest.raises(ModelDecisionError, match="目标类型不一致"):
        decider.choose(GOAL, None, set())
    client.responses.create.assert_not_called()

    cart_goal = CartGoal(
        cart_url=(PROJECT_ROOT / "lab" / "cart.html").as_uri(),
        items=(("蓝色茶杯", 1),), expected_count=1, expected_total=39,
    )
    decision = decider.choose(cart_goal, None, set())

    assert decision.action == "add_cup"
    assert client.responses.create.call_args.kwargs["tools"][0]["parameters"][
        "properties"
    ]["action"]["enum"] == list(CART_ACTION_NAMES)


def test_token_usage_sums_responses_and_marks_missing_usage_unknown() -> None:
    client = Mock()

    def reply(action: str, call_id: str, usage: object) -> SimpleNamespace:
        return SimpleNamespace(
            output=[SimpleNamespace(
                type="function_call", name="choose_browser_action",
                arguments=json.dumps({"action": action, "summary": action}),
                call_id=call_id,
            )],
            usage=usage,
        )

    client.responses.create.side_effect = [
        reply("open", "first", SimpleNamespace(
            input_tokens=12, output_tokens=3, total_tokens=15,
        )),
        reply("stop", "second", SimpleNamespace(
            input_tokens=20, output_tokens=4, total_tokens=24,
        )),
        reply("halt", "third", None),
    ]
    decider = OpenAIDecider(client, "offline-test-model")
    first = decider.choose(GOAL, None, set())
    decider.record_tool_result(first, None)
    decider.choose(GOAL, None, {"open"})
    assert decider.token_usage == {
        "input_tokens": 32, "output_tokens": 7, "total_tokens": 39,
    }

    decider.reset()
    assert decider.token_usage is None
    decider.choose(GOAL, None, set())
    assert decider.token_usage is None


def test_gemini_chat_completions_preserves_tool_turn_and_usage() -> None:
    client = Mock()
    replies = [
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                role="assistant", content=None,
                tool_calls=[SimpleNamespace(
                    id="gemini-call-1", type="function",
                    function=SimpleNamespace(
                        name="choose_browser_action",
                        arguments=json.dumps({"action": "fill", "summary": "输入搜索词"}),
                    ),
                )],
            ))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=4, total_tokens=14),
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                role="assistant", content=None,
                tool_calls=[SimpleNamespace(
                    id="gemini-call-2", type="function",
                    function=SimpleNamespace(
                        name="choose_browser_action",
                        arguments=json.dumps({"action": "stop", "summary": "完成"}),
                    ),
                )],
            ))],
            usage=SimpleNamespace(prompt_tokens=20, completion_tokens=3, total_tokens=23),
        ),
    ]
    captured_messages: list[list[dict[str, object]]] = []

    def create(**kwargs: object) -> SimpleNamespace:
        captured_messages.append([dict(item) for item in kwargs["messages"]])
        return replies.pop(0)

    client.chat.completions.create.side_effect = create
    decider = OpenAIDecider(client, "gemini-test-model", provider="gemini")

    first = decider.choose(GOAL, None, set(), timeout_seconds=2.5)
    assert first.call_id == "gemini-call-1"
    assert captured_messages[0][0]["role"] == "system"
    assert captured_messages[0][1]["role"] == "user"
    first_request = client.chat.completions.create.call_args_list[0].kwargs
    assert first_request["tools"][0]["function"]["name"] == "choose_browser_action"
    assert first_request["tool_choice"]["function"]["name"] == "choose_browser_action"
    assert first_request["max_tokens"] == 256
    assert first_request["timeout"] == 2.5

    decider.record_tool_result(first, None)
    second = decider.choose(GOAL, None, {"fill"})
    assert second.action == "stop"
    assert any(
        message.get("role") == "tool" and message.get("tool_call_id") == "gemini-call-1"
        for message in captured_messages[1]
    )
    assert decider.token_usage == {
        "input_tokens": 30, "output_tokens": 7, "total_tokens": 37,
    }


def test_from_env_defaults_to_gemini_free_tier_without_making_a_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "WEBPROBE_PROVIDER", "GEMINI_API_KEY", "WEBPROBE_API_KEY", "OPENAI_API_KEY",
        "WEBPROBE_MODEL", "WEBPROBE_BASE_URL", "WEBPROBE_REQUEST_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WEBPROBE_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "offline-test-key")
    fake_client_instance = Mock()

    with patch("webprobe.llm_decider.load_project_env"), patch(
        "webprobe.llm_decider.OpenAI", return_value=fake_client_instance,
    ) as client_factory:
        decider = OpenAIDecider.from_env()

    client_factory.assert_called_once_with(
        api_key="offline-test-key", timeout=20.0, max_retries=0,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    assert decider.model_name == "gemini-3.1-flash-lite"
    assert decider._provider == "gemini"
    fake_client_instance.chat.completions.create.assert_not_called()


def test_simple_env_file_loader_reads_values_without_overwriting_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# example\nWEBPROBE_TEST_SETTING='from-file'\n"
        "WEBPROBE_TEST_EMPTY=\nWEBPROBE_TEST_COMMENT=value # note\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("WEBPROBE_TEST_SETTING", raising=False)
    monkeypatch.setenv("WEBPROBE_TEST_EMPTY", "from-environment")
    monkeypatch.delenv("WEBPROBE_TEST_COMMENT", raising=False)

    _load_env_file(str(env_file))

    assert os.environ["WEBPROBE_TEST_SETTING"] == "from-file"
    assert os.environ["WEBPROBE_TEST_EMPTY"] == "from-environment"
    assert os.environ["WEBPROBE_TEST_COMMENT"] == "value"


def test_gemini_pacer_waits_until_next_case_fits_the_free_rpm_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _GEMINI_REQUEST_TIMESTAMPS.clear()
    _GEMINI_REQUEST_TIMESTAMPS.extend([0.0] * 12)
    current_time = [59.0]
    delays: list[float] = []

    monkeypatch.setattr("webprobe.llm_decider.time.monotonic", lambda: current_time[0])

    def fake_sleep(seconds: float) -> None:
        delays.append(seconds)
        current_time[0] += seconds

    monkeypatch.setattr("webprobe.llm_decider.time.sleep", fake_sleep)
    waited = _wait_for_gemini_request_capacity(3)

    assert waited == 1.05
    assert delays == [1.05]
    assert len(_GEMINI_REQUEST_TIMESTAMPS) == 0


def test_from_env_uses_bounded_request_timeout_without_sdk_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WEBPROBE_PROVIDER", "openai")
    monkeypatch.setenv("WEBPROBE_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("WEBPROBE_MODEL", "test-model")
    monkeypatch.setenv("WEBPROBE_REQUEST_TIMEOUT_SECONDS", "12.5")
    monkeypatch.delenv("WEBPROBE_BASE_URL", raising=False)

    fake_client_instance = Mock()
    with patch("webprobe.llm_decider.load_project_env"), patch(
        "webprobe.llm_decider.OpenAI", return_value=fake_client_instance,
    ) as client_factory:
        decider = OpenAIDecider.from_env()

    client_factory.assert_called_once_with(
        api_key="test-key", timeout=12.5, max_retries=0,
    )
    assert decider.model_name == "test-model"


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "not-a-number"])
def test_from_env_rejects_invalid_request_timeout(
    monkeypatch: pytest.MonkeyPatch, value: str,
) -> None:
    monkeypatch.setenv("WEBPROBE_PROVIDER", "openai")
    monkeypatch.setenv("WEBPROBE_API_KEY", "test-key")
    monkeypatch.setenv("WEBPROBE_MODEL", "test-model")
    monkeypatch.setenv("WEBPROBE_REQUEST_TIMEOUT_SECONDS", value)

    with pytest.raises(RuntimeError, match="WEBPROBE_REQUEST_TIMEOUT_SECONDS"):
        with patch("webprobe.llm_decider.load_project_env"):
            OpenAIDecider.from_env()
