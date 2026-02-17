from __future__ import annotations

import rich

from codex_python_sdk import create_client, render_exec_style_events


def main() -> None:
    with create_client(enable_web_search=True) as client:
        started = client.thread_start()
        rich.print(started)
        thread = started.get("thread") if isinstance(started.get("thread"), dict) else {}
        thread_id = thread.get("id") if isinstance(thread.get("id"), str) else None
        if not thread_id:
            raise RuntimeError("thread_start did not return a thread id")
        print(f"thread_id: {thread_id}")

        events = client.responses_events(prompt="What files are in this directory?", session_id=thread_id)
        render_exec_style_events(
            events,
            show_reasoning=True,
            show_system=False,
            show_tool_output=True,
            color="auto",
        )

        result = client.responses_create(
            prompt="What did we just discuss?",
            session_id=thread_id,
            include_events=True,
            turn_params={"model": "gpt-5", "effort": "medium", "summary": "concise"},
        )
        print(f"session_id: {result.session_id}")
        print(result.text)


if __name__ == "__main__":
    main()
