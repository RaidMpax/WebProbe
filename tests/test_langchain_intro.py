"""只测试可选 LangChain 教学练习，不把预写模型当成 Agent 评测。"""

import pytest

langchain = pytest.importorskip("langchain")

from langchain_core.messages import AIMessage, ToolMessage

from webprobe.langchain_intro import run_intro


def test_create_agent_executes_one_local_tool_and_keeps_result_in_history() -> None:
    messages = run_intro()

    assert [message.type for message in messages] == ["human", "ai", "tool", "ai"]
    proposal = messages[1]
    tool_result = messages[2]
    assert isinstance(proposal, AIMessage)
    assert proposal.tool_calls[0]["name"] == "lookup_lab_spec"
    assert proposal.tool_calls[0]["args"] == {"keyword": "茶杯"}
    assert isinstance(tool_result, ToolMessage)
    assert tool_result.content == "蓝色茶杯"
    assert tool_result.tool_call_id == proposal.tool_calls[0]["id"]
