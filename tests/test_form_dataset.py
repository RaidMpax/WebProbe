"""校准表单 Case：固定规格和故障标签须与真实 TestLab 表现一致。"""

import json
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from webprobe.form_dataset import FORM_DATASET, FormCase, load_form_cases


FORM_URL = (Path(__file__).resolve().parents[1] / "lab" / "feedback.html").as_uri()
CASES = load_form_cases()


def test_form_dataset_has_fixed_normal_and_bug_cases() -> None:
    assert len(CASES) == 13
    assert sum(case.has_bug for case in CASES) == 4
    changed = [case for case in CASES if case.page_variant == "renamed_submit"]
    assert len(changed) == 2 and all(case.has_bug is False for case in changed)
    # 三种无效输入均有正常/故障一对 Case，便于区分输入差异与故障差异。
    invalid_inputs = [("", ""), ("", "页面可以正常浏览"), ("测试用户", "")]
    for name, message in invalid_inputs:
        matching = [case for case in CASES if (case.name, case.message) == (name, message)]
        assert [case.has_bug for case in matching] == [False, True]


@pytest.mark.parametrize("case", CASES, ids=[case.case_id for case in CASES])
def test_form_case_ground_truth_matches_testlab(case: FormCase) -> None:
    """每案新建浏览器上下文；此处校准环境，不给 Agent 计算成绩。"""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context()
            if case.bug_mode == "required_bypass":
                context.add_init_script("window.__WEBPROBE_REQUIRED_BYPASS__ = true")
            if case.page_variant == "renamed_submit":
                context.add_init_script("window.__WEBPROBE_RENAME_SUBMIT__ = true")
            page = context.new_page()
            page.goto(FORM_URL)
            form = page.locator("#feedback-form")
            expect(form).to_have_attribute("data-submission-count", "0")
            expect(page.get_by_role("status")).to_have_text("尚未提交")

            if case.name:
                page.get_by_label("姓名（必填）").fill(case.name)
            if case.message:
                page.get_by_label("反馈内容（必填）").fill(case.message)
            submit_name = "发送反馈" if case.page_variant == "renamed_submit" else "提交反馈"
            expect(page.get_by_role("button", name=submit_name)).to_be_visible()
            page.get_by_role("button", name=submit_name).click()

            actual_submitted = form.get_attribute("data-submission-count") == "1"
            actual_status = page.get_by_role("status").inner_text()
            matches_spec = (
                actual_submitted == case.expected_submitted
                and actual_status == case.expected_status
            )
            assert matches_spec is (not case.has_bug), case.case_id
            if case.has_bug:
                # 注入故障必须是“无效表单被接受”，而非其他偶然页面错误。
                assert form.evaluate("element => element.checkValidity()") is False
                assert actual_submitted is True
                assert actual_status == "提交成功"
            context.close()
        finally:
            browser.close()


def test_form_dataset_rejects_ground_truth_contradiction(tmp_path: Path) -> None:
    first = json.loads(FORM_DATASET.read_text(encoding="utf-8").splitlines()[0])
    first["ground_truth"]["has_bug"] = True
    invalid = tmp_path / "invalid_form_case.jsonl"
    invalid.write_text(json.dumps(first, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="Ground Truth 矛盾"):
        load_form_cases(invalid)


def test_form_dataset_rejects_unknown_page_variant(tmp_path: Path) -> None:
    first = json.loads(FORM_DATASET.read_text(encoding="utf-8").splitlines()[0])
    first["initial_state"]["page_variant"] = "unknown"
    invalid = tmp_path / "invalid_form_variant.jsonl"
    invalid.write_text(json.dumps(first, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="未知页面变化"):
        load_form_cases(invalid)
