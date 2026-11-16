"""可选学习练习：用 LangGraph 表达一个有上限的网页检查流程。

输入是预写的页面快照，不访问浏览器或模型。它只展示状态如何在节点间传递，
以及验证失败后怎样决定重试或结束；不能作为网站 Bug 或 Agent 能力证据。
"""

from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph


class ProbeState(TypedDict):
    """所有节点共用的任务状态；节点只返回自己更新的字段。"""

    snapshots: list[list[str]]
    expected_product: str
    max_attempts: int
    attempts: int
    visible_products: list[str]
    action: str
    status: Literal["running", "completed", "needs_review"]


def observe(state: ProbeState) -> dict[str, object]:
    """第 n 次观察取第 n 份预写页面快照。"""
    return {"visible_products": state["snapshots"][state["attempts"]]}


def decide(state: ProbeState) -> dict[str, str]:
    """教学用固定决策；未来真实 Agent 才会在这里调用模型。"""
    return {"action": "check_visible_products"}


def act(state: ProbeState) -> dict[str, int]:
    """模拟完成一次检查，并消耗一次尝试预算。"""
    if state["action"] != "check_visible_products":
        raise ValueError("未知动作")
    return {"attempts": state["attempts"] + 1}


def verify(state: ProbeState) -> dict[str, str]:
    """对照独立规格；持续不符只表示需要人工复核。"""
    if state["visible_products"] == [state["expected_product"]]:
        return {"status": "completed"}
    if state["attempts"] >= state["max_attempts"]:
        return {"status": "needs_review"}
    return {"status": "running"}


def route_after_verify(state: ProbeState) -> Literal["observe", "done"]:
    """条件边：尚有预算且未通过时，回到观察节点。"""
    return "observe" if state["status"] == "running" else "done"


def build_graph():
    graph = StateGraph(ProbeState)
    graph.add_node("observe", observe)
    graph.add_node("decide", decide)
    graph.add_node("act", act)
    graph.add_node("verify", verify)
    graph.add_edge(START, "observe")
    graph.add_edge("observe", "decide")
    graph.add_edge("decide", "act")
    graph.add_edge("act", "verify")
    graph.add_conditional_edges(
        "verify", route_after_verify, {"observe": "observe", "done": END}
    )
    return graph.compile()


def run_intro(
    snapshots: list[list[str]], expected_product: str, max_attempts: int = 2
) -> ProbeState:
    """运行固定快照练习；调用前保证每次尝试都有对应的快照。"""
    if max_attempts < 1:
        raise ValueError("max_attempts 必须大于 0")
    if len(snapshots) < max_attempts:
        raise ValueError("快照数量必须覆盖最大尝试次数")
    initial_state: ProbeState = {
        "snapshots": snapshots,
        "expected_product": expected_product,
        "max_attempts": max_attempts,
        "attempts": 0,
        "visible_products": [],
        "action": "",
        "status": "running",
    }
    # 图自身的条件边控制正常停止；recursion_limit 是额外的循环保护。
    return build_graph().invoke(
        initial_state, config={"recursion_limit": max_attempts * 4 + 2}
    )


if __name__ == "__main__":
    for name, snapshots in (
        ("正常", [["蓝色茶杯"], ["蓝色茶杯"]]),
        ("首次异常后恢复", [["帆布包"], ["蓝色茶杯"]]),
        ("持续不符", [["帆布包"], ["帆布包"]]),
    ):
        result = run_intro(snapshots, expected_product="蓝色茶杯")
        print(f"{name}: {result['status']}, 尝试 {result['attempts']} 次")
