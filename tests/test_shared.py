from __future__ import annotations

from codex_python_sdk._shared import (
    diff_change_counts,
    first_nonempty_text,
    token_usage_summary,
    turn_plan_summary,
)


def test_first_nonempty_text_extracts_nested_value():
    payload = {
        "data": [
            {"ignored": ""},
            {"nested": {"text": "  useful text  "}},
        ]
    }
    assert first_nonempty_text(payload) == "useful text"


def test_token_usage_summary_supports_snake_case_and_missing_totals():
    token_usage = {
        "last": {"total_tokens": 12},
        "total": {"totalTokens": 34},
    }
    assert token_usage_summary(token_usage) == "last=12, total=34"
    assert token_usage_summary({}) is None


def test_diff_change_counts_ignores_file_headers():
    diff = "\n".join(["--- a/x", "+++ b/x", "+added", "+also", "-removed"])
    assert diff_change_counts(diff) == (2, 1)


def test_turn_plan_summary_counts_statuses_and_falls_back_to_explanation():
    params = {
        "explanation": "working",
        "plan": [
            {"status": "inProgress", "step": "a"},
            {"status": "pending", "step": "b"},
            {"status": "completed", "step": "c"},
            "ignore",
        ],
    }
    assert turn_plan_summary(params) == "working, steps=4, inProgress=1, pending=1, completed=1"
    assert turn_plan_summary({"explanation": "no plan"}) == "no plan"
