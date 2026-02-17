from __future__ import annotations

import argparse
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable

from codex_python_sdk import create_client, render_exec_style_events


@dataclass
class DemoResult:
    name: str
    ok: bool
    detail: str


def _truncate(value: Any, limit: int = 120) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _find_first_string(value: Any, keys: set[str]) -> str | None:
    if isinstance(value, dict):
        for key in keys:
            maybe = value.get(key)
            if isinstance(maybe, str) and maybe:
                return maybe
        for nested in value.values():
            found = _find_first_string(nested, keys)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_first_string(nested, keys)
            if found:
                return found
    return None


def _run_case(
    results: list[DemoResult],
    name: str,
    fn: Callable[[], Any],
    *,
    strict: bool,
) -> Any:
    try:
        value = fn()
        results.append(DemoResult(name=name, ok=True, detail=_truncate(value)))
        print(f"[PASS] {name}: {_truncate(value)}")
        return value
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        results.append(DemoResult(name=name, ok=False, detail=detail))
        print(f"[FAIL] {name}: {detail}")
        if strict:
            raise
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex SDK smoke runner and API demo.")
    parser.add_argument(
        "--mode",
        choices=("smoke", "demo", "full"),
        default="smoke",
        help="smoke: quick health check; demo: stable API showcase; full: demo + unstable remote cases.",
    )
    parser.add_argument("--model", default=None, help="Optional model for thread/start.")
    parser.add_argument("--strict", action="store_true", help="Fail fast on first interface failure.")
    parser.add_argument("--show-events", action="store_true", help="Render event stream in exec-style.")
    parser.add_argument("--prompt-create", default="Reply with exactly: READY", help="Prompt for responses_create.")
    parser.add_argument("--prompt-events", default="List three files in current directory.", help="Prompt for responses_events.")
    parser.add_argument("--prompt-stream", default="Reply with exactly: STREAM_OK", help="Prompt for responses_stream_text.")
    args = parser.parse_args()

    results: list[DemoResult] = []
    last_turn_id: str | None = None
    fork_thread_id: str | None = None

    with create_client(
        on_command_approval=lambda params: {"decision": "accept"},
        on_file_change_approval=lambda params: {"decision": "accept"},
        on_tool_request_user_input=lambda params: {"answers": {}},
    ) as client:
        start_params = {"model": args.model} if args.model else None
        bootstrap_thread_id: str | None = None
        started = _run_case(
            results,
            "thread_start",
            lambda: client.thread_start(params=start_params),
            strict=args.strict,
        )
        if isinstance(started, dict):
            thread = started.get("thread")
            if isinstance(thread, dict) and isinstance(thread.get("id"), str):
                bootstrap_thread_id = thread["id"]

        def _run_create() -> Any:
            if bootstrap_thread_id is not None:
                return client.responses_create(prompt=args.prompt_create, session_id=bootstrap_thread_id)
            return client.responses_create(prompt=args.prompt_create)

        created = _run_case(
            results,
            "responses_create",
            _run_create,
            strict=args.strict,
        )
        thread_id: str | None = bootstrap_thread_id
        if created is not None and isinstance(created.session_id, str) and created.session_id:
            thread_id = created.session_id
        if thread_id is None:
            print("[INFO] Skipping follow-up cases because no usable session_id is available.")

        def _run_stream() -> str:
            assert thread_id is not None
            chunks = list(client.responses_stream_text(prompt=args.prompt_stream, session_id=thread_id))
            return "".join(chunks).strip()

        if thread_id is not None:
            _run_case(results, "responses_stream_text", _run_stream, strict=args.strict)

        if args.mode != "smoke" and thread_id is not None:
            def _run_events() -> str:
                nonlocal last_turn_id
                event_types: list[str] = []
                event_count = 0
                events = client.responses_events(prompt=args.prompt_events, session_id=thread_id)
                if args.show_events:
                    render_exec_style_events(events, show_reasoning=True, show_system=False, show_tool_output=True, color="auto")
                    return "rendered by ExecStyleRenderer"
                for event in events:
                    event_count += 1
                    if len(event_types) < 8:
                        event_types.append(event.type)
                    if event.turn_id:
                        last_turn_id = event.turn_id
                return f"events={event_count}, sample_types={event_types}"

            _run_case(results, "responses_events", _run_events, strict=args.strict)
            _run_case(results, "thread_read", lambda: client.thread_read(thread_id, include_turns=True), strict=args.strict)
            _run_case(results, "thread_list", lambda: client.thread_list(limit=10), strict=args.strict)
            _run_case(
                results,
                "thread_name_set",
                lambda: client.thread_name_set(thread_id, "demo-thread"),
                strict=args.strict,
            )

            forked = _run_case(
                results,
                "thread_fork",
                lambda: client.thread_fork(thread_id, params={"approvalPolicy": "on-request"}),
                strict=args.strict,
            )
            fork_id = _find_first_string(forked, {"threadId", "id"}) if forked is not None else None
            if isinstance(fork_id, str):
                fork_thread_id = fork_id

            _run_case(results, "thread_loaded_list", lambda: client.thread_loaded_list(limit=5), strict=args.strict)
            _run_case(results, "skills_list", lambda: client.skills_list(force_reload=False), strict=args.strict)
            _run_case(results, "app_list", lambda: client.app_list(limit=5), strict=args.strict)
            _run_case(results, "model_list", lambda: client.model_list(limit=5), strict=args.strict)
            _run_case(results, "account_rate_limits_read", lambda: client.account_rate_limits_read(), strict=args.strict)
            _run_case(results, "account_read", lambda: client.account_read(refresh_token=False), strict=args.strict)
            _run_case(results, "thread_archive", lambda: client.thread_archive(thread_id), strict=args.strict)
            _run_case(results, "thread_unarchive", lambda: client.thread_unarchive(thread_id), strict=args.strict)
            _run_case(
                results,
                "skills_config_write",
                lambda: client.skills_config_write("demo-skill-path", False),
                strict=args.strict,
            )

        if args.mode == "full":
            if thread_id is None:
                print("[INFO] Skipped full-mode unstable APIs because no usable session_id is available.")
                unstable_thread_id = None
            else:
                unstable_thread_id = fork_thread_id or thread_id
            if unstable_thread_id is not None:
                remote_read = _run_case(results, "skills_remote_read", lambda: client.skills_remote_read(), strict=args.strict)
                review_out = _run_case(
                    results,
                    "review_start",
                    lambda: client.review_start(unstable_thread_id, {"type": "uncommittedChanges"}, delivery="detached"),
                    strict=args.strict,
                )
                review_turn_id = None
                review_thread_id = unstable_thread_id
                if isinstance(review_out, dict):
                    review_turn = review_out.get("turn")
                    if isinstance(review_turn, dict) and isinstance(review_turn.get("id"), str):
                        review_turn_id = review_turn["id"]
                    maybe_review_thread_id = review_out.get("reviewThreadId")
                    if isinstance(maybe_review_thread_id, str) and maybe_review_thread_id:
                        review_thread_id = maybe_review_thread_id
                if not review_turn_id:
                    review_turn_id = last_turn_id

                if isinstance(review_turn_id, str):
                    def _interrupt() -> dict[str, Any]:
                        assert review_turn_id is not None
                        return client.turn_interrupt(review_thread_id, review_turn_id)

                    interrupt_out = _run_case(
                        results,
                        "turn_interrupt",
                        _interrupt,
                        strict=args.strict,
                    )
                else:
                    interrupt_out = None
                    print("[INFO] Skipped turn_interrupt because review_start did not return a turn id.")

                if interrupt_out is not None:
                    def _wait_review_turn_terminal() -> dict[str, Any]:
                        assert review_turn_id is not None
                        deadline = time.monotonic() + 30.0
                        last_status: str | None = None
                        while time.monotonic() < deadline:
                            snapshot = client.thread_read(review_thread_id, include_turns=True)
                            turns = snapshot.get("turns")
                            if isinstance(turns, list):
                                for turn in reversed(turns):
                                    if not isinstance(turn, dict):
                                        continue
                                    if turn.get("id") != review_turn_id:
                                        continue
                                    maybe_status = turn.get("status")
                                    if isinstance(maybe_status, str):
                                        last_status = maybe_status
                                        if maybe_status != "inProgress":
                                            return {"terminal": True, "status": maybe_status}
                                    break
                            time.sleep(0.5)
                        return {"terminal": False, "status": last_status}

                    wait_out = _run_case(
                        results,
                        "wait_review_turn_terminal",
                        _wait_review_turn_terminal,
                        strict=args.strict,
                    )

                    if isinstance(wait_out, dict) and wait_out.get("terminal") is True:
                        rollback_thread_id = unstable_thread_id
                        _run_case(
                            results,
                            "thread_rollback",
                            lambda: client.thread_rollback(rollback_thread_id, 1),
                            strict=args.strict,
                        )
                    else:
                        print("[INFO] Skipped thread_rollback because review turn did not reach terminal status in time.")
                else:
                    print("[INFO] Skipped thread_rollback because turn_interrupt did not complete successfully.")

                _run_case(
                    results,
                    "thread_compact_start",
                    lambda: client.thread_compact_start(unstable_thread_id),
                    strict=args.strict,
                )

                hazelnut_id = _find_first_string(remote_read, {"hazelnutId"}) if remote_read is not None else None
                if isinstance(hazelnut_id, str) and hazelnut_id:
                    _run_case(
                        results,
                        "skills_remote_write",
                        lambda: client.skills_remote_write(hazelnut_id, False),
                        strict=args.strict,
                    )
                else:
                    print("[INFO] Skipped skills_remote_write because skills_remote_read did not return a hazelnut id.")
        elif args.mode == "demo" and thread_id is not None:
            print("[INFO] Skipped unstable APIs. Use '--mode full' to run remote/interrupt/compact cases.")
        elif args.mode == "smoke":
            print("[INFO] Smoke mode ran core checks only. Use '--mode demo' for broader API coverage.")

    passed = sum(1 for item in results if item.ok)
    failed = len(results) - passed
    print("\n=== SUMMARY ===")
    print(f"mode={args.mode} total={len(results)} passed={passed} failed={failed}")
    if failed:
        print("failed cases:")
        for item in results:
            if not item.ok:
                print(f"- {item.name}: {item.detail}")
        if args.strict:
            raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
