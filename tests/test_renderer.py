from __future__ import annotations

from io import StringIO

from codex_python_sdk.renderer import ExecStyleRenderer


def test_compute_use_color_respects_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("CLICOLOR_FORCE", raising=False)
    assert ExecStyleRenderer._compute_use_color("auto", StringIO()) is False


def test_compute_use_color_respects_clicolor_force(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("CLICOLOR_FORCE", "1")
    assert ExecStyleRenderer._compute_use_color("auto", StringIO()) is True


def test_compute_use_color_handles_isatty_exception(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR_FORCE", raising=False)

    class _BrokenStream:
        def isatty(self) -> bool:
            raise RuntimeError("isatty failed")

    assert ExecStyleRenderer._compute_use_color("auto", _BrokenStream()) is False
