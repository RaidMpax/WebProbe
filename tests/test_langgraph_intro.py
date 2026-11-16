"""验证 LangGraph 教学流程的结束条件与尝试上限。"""

import pytest

pytest.importorskip("langgraph")

from webprobe.langgraph_intro import run_intro


def test_matching_snapshot_finishes_after_one_attempt() -> None:
    result = run_intro([["蓝色茶杯"], ["帆布包"]], "蓝色茶杯")
    assert result["status"] == "completed"
    assert result["attempts"] == 1


def test_mismatch_then_match_retries_once() -> None:
    result = run_intro([["帆布包"], ["蓝色茶杯"]], "蓝色茶杯")
    assert result["status"] == "completed"
    assert result["attempts"] == 2
    assert result["visible_products"] == ["蓝色茶杯"]


def test_persistent_mismatch_stops_for_review() -> None:
    result = run_intro([["帆布包"], ["帆布包"]], "蓝色茶杯")
    assert result["status"] == "needs_review"
    assert result["attempts"] == 2


def test_invalid_budget_or_missing_snapshot_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        run_intro([], "蓝色茶杯", max_attempts=0)
    with pytest.raises(ValueError, match="快照数量"):
        run_intro([["蓝色茶杯"]], "蓝色茶杯", max_attempts=2)
