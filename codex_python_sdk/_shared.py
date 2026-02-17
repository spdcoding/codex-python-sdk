from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def first_nonempty_text(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            text = first_nonempty_text(item)
            if text:
                out.append(text)
        if out:
            return "\n".join(out)
        return None
    if isinstance(value, dict):
        for key in ("message", "text", "delta", "error", "content"):
            if key in value:
                text = first_nonempty_text(value.get(key))
                if text:
                    return text
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                text = first_nonempty_text(nested)
                if text:
                    return text
    return None


def token_usage_total_tokens(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    total = value.get("totalTokens")
    if isinstance(total, int):
        return total
    total = value.get("total_tokens")
    if isinstance(total, int):
        return total
    return None


def token_usage_summary(token_usage: dict[str, Any]) -> str | None:
    last = token_usage.get("last")
    total = token_usage.get("total")
    last_total = token_usage_total_tokens(last)
    total_total = token_usage_total_tokens(total)
    if last_total is None and total_total is None:
        return None
    if last_total is None:
        return f"total={total_total}"
    if total_total is None:
        return f"last={last_total}"
    return f"last={last_total}, total={total_total}"


def diff_change_counts(diff: str) -> tuple[int, int]:
    added = 0
    removed = 0
    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed


def turn_plan_summary(params: dict[str, Any]) -> str | None:
    plan = params.get("plan")
    if not isinstance(plan, list):
        return first_nonempty_text(params.get("explanation"))

    status_counts: dict[str, int] = {}
    for item in plan:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if isinstance(status, str):
            status_counts[status] = status_counts.get(status, 0) + 1

    parts: list[str] = []
    explanation = params.get("explanation")
    if isinstance(explanation, str) and explanation.strip():
        parts.append(explanation.strip())
    parts.append(f"steps={len(plan)}")
    for status in ("inProgress", "pending", "completed"):
        if status in status_counts:
            parts.append(f"{status}={status_counts[status]}")
    return ", ".join(parts)

