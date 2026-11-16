"""可选学习练习：用 LangChain 跑通一次离线工具调用。

这不是 WebProbe 的浏览器 Agent，也不参加 Evals。模型的两次回复均预先写好，
用来观察框架怎样执行工具并把结果插入消息历史。
"""

from collections.abc import Sequence
from typing import Any

from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, BaseMessage


def lookup_lab_spec(keyword: str) -> str:
    """查询 TestLab 的固定商品规格；本练习不访问浏览器。"""
    specifications = {"茶杯": "蓝色茶杯"}
    return specifications.get(keyword, "没有这项规格")


class ScriptedToolModel(FakeMessagesListChatModel):
    """允许测试模型接受工具列表；实际选择动作仍由预写响应决定。"""

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ScriptedToolModel":
        if len(tools) != 1:
            raise ValueError("本练习只提供一个工具")
        return self


def run_intro() -> list[BaseMessage]:
    """返回完整消息：用户提问、工具提议、工具结果、预写结束语。"""
    model = ScriptedToolModel(responses=[
        AIMessage(
            content="",
            tool_calls=[{
                "name": "lookup_lab_spec",
                "args": {"keyword": "茶杯"},
                "id": "scripted-call-1",
            }],
        ),
        AIMessage(content="演示结束；请检查上一条工具结果。"),
    ])
    agent = create_agent(
        model=model,
        tools=[lookup_lab_spec],
        system_prompt="这是本地工具调用练习。",
    )
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "茶杯的预期商品是什么？"}]},
        config={"recursion_limit": 6},
    )
    return result["messages"]


if __name__ == "__main__":
    for message in run_intro():
        if isinstance(message, AIMessage) and message.tool_calls:
            print(f"ai tool_calls: {message.tool_calls}")
        else:
            print(f"{message.type}: {message.content}")
