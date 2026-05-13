"""Tests for cli._cprint's bg-thread cooperation with prompt_toolkit.

Background: when a prompt_toolkit Application is running, a bg thread that
calls ``_pt_print`` directly can race with the input-area redraw and the
printed line can end up visually buried behind the prompt.  ``_cprint`` now
routes cross-thread prints through ``run_in_terminal`` via
``loop.call_soon_threadsafe`` so the self-improvement background review's
``💾 Self-improvement review: …`` summary actually surfaces to the user.

These tests verify the routing logic without spinning up a real PT app.
"""

from __future__ import annotations

import os
import sys
import types
from types import SimpleNamespace

import pytest

import cli


@pytest.fixture(autouse=True)
def reset_output_history():
    cli._configure_output_history(False, 200)
    yield
    cli._configure_output_history(True, 200)


def test_cprint_no_app_direct_print(monkeypatch):
    """No active app → direct _pt_print, no run_in_terminal involvement."""
    calls = []
    monkeypatch.setattr(cli, "_pt_print", lambda x: calls.append(("pt_print", x)))
    monkeypatch.setattr(cli, "_PT_ANSI", lambda t: ("ANSI", t))

    # Patch the prompt_toolkit import the function performs internally.
    fake_pt_app = types.ModuleType("prompt_toolkit.application")
    fake_pt_app.get_app_or_none = lambda: None
    fake_pt_app.run_in_terminal = lambda *a, **kw: calls.append(("run_in_terminal",))
    monkeypatch.setitem(sys.modules, "prompt_toolkit.application", fake_pt_app)

    cli._cprint("hello")

    assert calls == [("pt_print", ("ANSI", "hello"))]


def test_cprint_app_not_running_direct_print(monkeypatch):
    """App exists but not running (e.g. teardown) → direct print."""
    calls = []
    monkeypatch.setattr(cli, "_pt_print", lambda x: calls.append(("pt_print", x)))
    monkeypatch.setattr(cli, "_PT_ANSI", lambda t: t)

    fake_app = SimpleNamespace(_is_running=False, loop=None)
    fake_pt_app = types.ModuleType("prompt_toolkit.application")
    fake_pt_app.get_app_or_none = lambda: fake_app
    fake_pt_app.run_in_terminal = lambda *a, **kw: calls.append(("run_in_terminal",))
    monkeypatch.setitem(sys.modules, "prompt_toolkit.application", fake_pt_app)

    cli._cprint("x")

    assert calls == [("pt_print", "x")]


def test_cprint_bg_thread_schedules_on_app_loop(monkeypatch):
    """App running + different thread → schedules via call_soon_threadsafe."""
    scheduled = []
    direct_prints = []

    monkeypatch.setattr(cli, "_pt_print", lambda x: direct_prints.append(x))
    monkeypatch.setattr(cli, "_PT_ANSI", lambda t: t)

    class FakeLoop:
        def is_running(self):
            return True

        def call_soon_threadsafe(self, cb, *args):
            scheduled.append(cb)

    fake_loop = FakeLoop()

    # Install a fake "current loop" that is NOT the app's loop, so the
    # cross-thread branch is taken.
    fake_current_loop = SimpleNamespace(is_running=lambda: True)
    fake_asyncio = types.ModuleType("asyncio")

    class _Policy:
        def get_event_loop(self):
            return fake_current_loop

    fake_asyncio.get_event_loop_policy = lambda: _Policy()
    monkeypatch.setitem(sys.modules, "asyncio", fake_asyncio)

    fake_app = SimpleNamespace(_is_running=True, loop=fake_loop)
    fake_pt_app = types.ModuleType("prompt_toolkit.application")
    fake_pt_app.get_app_or_none = lambda: fake_app

    run_in_terminal_calls = []

    def _fake_run_in_terminal(func, **kw):
        run_in_terminal_calls.append(func)
        # Simulate run_in_terminal actually calling func (as the real PT
        # impl would once the app loop tick picks it up).
        func()
        return None

    fake_pt_app.run_in_terminal = _fake_run_in_terminal
    monkeypatch.setitem(sys.modules, "prompt_toolkit.application", fake_pt_app)

    cli._cprint("💾 Self-improvement review: Skill updated")

    # call_soon_threadsafe must have been called with a scheduling cb.
    assert len(scheduled) == 1

    # Invoking the scheduled callback should hit run_in_terminal.
    scheduled[0]()
    assert len(run_in_terminal_calls) == 1

    # And run_in_terminal's inner func should have emitted a pt_print.
    assert direct_prints == ["💾 Self-improvement review: Skill updated"]


def test_cprint_same_thread_as_app_loop_direct_print(monkeypatch):
    """App running on same thread → direct print (no scheduling)."""
    direct_prints = []
    monkeypatch.setattr(cli, "_pt_print", lambda x: direct_prints.append(x))
    monkeypatch.setattr(cli, "_PT_ANSI", lambda t: t)

    class FakeLoop:
        def is_running(self):
            return True

        def call_soon_threadsafe(self, cb, *args):
            raise AssertionError(
                "call_soon_threadsafe must not be used on the app's own thread"
            )

    fake_loop = FakeLoop()
    fake_asyncio = types.ModuleType("asyncio")

    class _Policy:
        def get_event_loop(self):
            return fake_loop  # same as app loop

    fake_asyncio.get_event_loop_policy = lambda: _Policy()
    monkeypatch.setitem(sys.modules, "asyncio", fake_asyncio)

    fake_app = SimpleNamespace(_is_running=True, loop=fake_loop)
    fake_pt_app = types.ModuleType("prompt_toolkit.application")
    fake_pt_app.get_app_or_none = lambda: fake_app
    fake_pt_app.run_in_terminal = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, "prompt_toolkit.application", fake_pt_app)

    cli._cprint("x")

    assert direct_prints == ["x"]


def test_cprint_swallows_app_loop_attr_error(monkeypatch):
    """Loop missing on app → fall back to direct print, no crash."""
    direct_prints = []
    monkeypatch.setattr(cli, "_pt_print", lambda x: direct_prints.append(x))
    monkeypatch.setattr(cli, "_PT_ANSI", lambda t: t)

    class WeirdApp:
        _is_running = True

        @property
        def loop(self):
            raise RuntimeError("no loop for you")

    fake_pt_app = types.ModuleType("prompt_toolkit.application")
    fake_pt_app.get_app_or_none = lambda: WeirdApp()
    fake_pt_app.run_in_terminal = lambda *a, **kw: None
    monkeypatch.setitem(sys.modules, "prompt_toolkit.application", fake_pt_app)

    cli._cprint("fallback")

    assert direct_prints == ["fallback"]


def test_cprint_swallows_prompt_toolkit_import_error(monkeypatch):
    """If prompt_toolkit.application itself fails to import, fall back."""
    direct_prints = []
    monkeypatch.setattr(cli, "_pt_print", lambda x: direct_prints.append(x))
    monkeypatch.setattr(cli, "_PT_ANSI", lambda t: t)

    # Drop cached prompt_toolkit.application AND install a meta-path finder
    # that raises ImportError on re-import.
    monkeypatch.delitem(sys.modules, "prompt_toolkit.application", raising=False)

    class _BlockFinder:
        def find_module(self, name, path=None):
            if name == "prompt_toolkit.application":
                return self
            return None

        def load_module(self, name):
            raise ImportError("blocked for test")

        def find_spec(self, name, path=None, target=None):
            if name == "prompt_toolkit.application":
                # Returning a bogus spec that will fail on load works too,
                # but raising here keeps the test simple.
                raise ImportError("blocked for test")
            return None

    blocker = _BlockFinder()
    sys.meta_path.insert(0, blocker)
    try:
        cli._cprint("fallback2")
    finally:
        sys.meta_path.remove(blocker)

    assert direct_prints == ["fallback2"]


def test_output_history_preserves_sgr_ansi_and_keeps_recent_lines():
    cli._configure_output_history(True, 10)

    for idx in range(12):
        cli._record_output_history(f"\x1b[31mline-{idx}\x1b[0m")

    assert list(cli._OUTPUT_HISTORY) == [f"\x1b[31mline-{idx}\x1b[0m" for idx in range(2, 12)]


def test_display_history_records_static_ansi_entries_and_skips_ansi_only_lines():
    history = cli.DisplayHistory(max_lines=10)

    history.record_text("\x1b[31mred\x1b[0m\n\x1b[32m\x1b[0m\n\x1b[2Jplain")

    assert [entry.render() for entry in history.entries] == [
        ["\x1b[31mred\x1b[0m"],
        ["plain"],
    ]
    assert all(isinstance(entry, cli.StaticAnsiEntry) for entry in history.entries)


def test_display_history_wraps_callable_entries_for_replay(monkeypatch):
    history = cli.DisplayHistory(max_lines=10)
    calls = []
    printed = []

    history.record_entry(lambda: calls.append("render") or ["dynamic"])
    history.replay(lambda text: printed.append(text), lambda text: text, width=123)

    assert calls == ["render"]
    assert printed == ["dynamic"]
    assert isinstance(history.entries[0], cli.CallableHistoryEntry)


def test_output_history_strips_non_sgr_controls_but_keeps_color():
    cli._configure_output_history(True, 10)

    cli._record_output_history("\x1b[31mred\x1b[0m\x1b[2J\x1b[Hplain")

    assert list(cli._OUTPUT_HISTORY) == ["\x1b[31mred\x1b[0mplain"]


def test_replay_output_history_does_not_record_replayed_lines(monkeypatch):
    cli._configure_output_history(True, 10)
    cli._record_output_history("visible output")
    printed = []

    def _fake_print(value):
        printed.append(value)
        cli._record_output_history("duplicated replay")

    monkeypatch.setattr(cli, "_pt_print", _fake_print)
    monkeypatch.setattr(cli, "_PT_ANSI", lambda text: text)

    cli._replay_output_history()

    assert printed == ["visible output"]
    assert list(cli._OUTPUT_HISTORY) == ["visible output"]


def test_replay_output_history_rerenders_callable_entries(monkeypatch):
    cli._configure_output_history(True, 10)
    widths_seen = []
    printed = []

    def _render_current_width():
        widths_seen.append("called")
        return ["top border", "body"]

    cli._record_output_history_entry(_render_current_width)
    monkeypatch.setattr(cli, "_pt_print", lambda value: printed.append(value))
    monkeypatch.setattr(cli, "_PT_ANSI", lambda text: text)

    cli._replay_output_history()

    assert widths_seen == ["called"]
    assert printed == ["top border", "body"]
    assert list(cli._OUTPUT_HISTORY) == [_render_current_width]


def test_suspend_output_history_restores_legacy_suppressed_global():
    cli._configure_output_history(True, 10)

    assert cli._OUTPUT_HISTORY_SUPPRESSED is False
    with cli._suspend_output_history():
        assert cli._OUTPUT_HISTORY_SUPPRESSED is True
        cli._record_output_history("hidden")

    assert cli._OUTPUT_HISTORY_SUPPRESSED is False
    cli._record_output_history("visible")
    assert list(cli._OUTPUT_HISTORY) == ["visible"]


def test_response_border_entry_renders_at_requested_width():
    entry = cli.ResponseBorderEntry("top", "⚕ Hermes")

    narrow = entry.render(80)[0]
    wide = entry.render(120)[0]

    assert narrow != wide
    assert cli._ANSI_CONTROL_RE.sub("", narrow).lstrip("\n").startswith("╭─⚕ Hermes")
    assert len(cli._ANSI_CONTROL_RE.sub("", narrow).lstrip("\n")) == 80
    assert len(cli._ANSI_CONTROL_RE.sub("", wide).lstrip("\n")) == 120


def test_stream_response_top_border_history_rerenders_at_current_width(monkeypatch):
    cli._configure_output_history(True, 10)
    printed = []
    width = {"cols": 40}
    monkeypatch.setattr(cli.shutil, "get_terminal_size", lambda *_args, **_kwargs: os.terminal_size((width["cols"], 24)))
    monkeypatch.setattr(cli, "_cprint", lambda value: printed.append(value))

    class Dummy:
        show_timestamps = False
        show_reasoning = False
        final_response_markdown = "strip"
        _stream_box_opened = False
        _stream_buf = ""
        _stream_table_buf = []
        _in_stream_table = False

        def _close_reasoning_box(self):
            pass

    cli.HermesCLI._emit_stream_text(Dummy(), "hello")

    assert "╭─" in printed[0]
    entry = list(cli._OUTPUT_HISTORY)[0]
    assert isinstance(entry, cli.AssistantResponseEntry)
    assert entry.label.strip()
    width["cols"] = 60
    replayed = entry.render(width["cols"])[0]
    visible = cli._ANSI_CONTROL_RE.sub("", replayed).lstrip("\n")
    assert visible.startswith(f"╭─{entry.label}")
    assert len(visible) == 60


def test_stream_response_bottom_border_history_rerenders_at_current_width(monkeypatch):
    cli._configure_output_history(True, 10)
    printed = []
    width = {"cols": 40}
    monkeypatch.setattr(cli.shutil, "get_terminal_size", lambda *_args, **_kwargs: os.terminal_size((width["cols"], 24)))
    monkeypatch.setattr(cli, "_cprint", lambda value: printed.append(value))

    class Dummy:
        _stream_box_opened = True
        _stream_buf = ""
        _stream_table_buf = []
        _in_stream_table = False

        def _close_reasoning_box(self):
            pass

    cli.HermesCLI._flush_stream(Dummy())

    assert "╰" in printed[0]
    entry = list(cli._OUTPUT_HISTORY)[0]
    assert isinstance(entry, cli.ResponseBorderEntry)
    assert entry.kind == "bottom"
    width["cols"] = 60
    replayed = entry.render(width["cols"])[0]
    visible = cli._ANSI_CONTROL_RE.sub("", replayed)
    assert len(visible) == 60


def test_assistant_response_entry_rewraps_long_paragraph_at_replay_width():
    source = (
        "This is a deliberately long assistant response paragraph that should "
        "wrap differently when display history is replayed after the terminal "
        "is resized from a narrow width to a wider width."
    )
    entry = cli.AssistantResponseEntry("⚕ Hermes", source, text_ansi="", markdown_mode="strip", completed=True)

    narrow = [cli._ANSI_CONTROL_RE.sub("", line) for line in entry.render(50)]
    wide = [cli._ANSI_CONTROL_RE.sub("", line) for line in entry.render(90)]

    assert narrow != wide
    assert len(narrow) > len(wide)
    assert narrow[0].lstrip("\n").startswith("╭─⚕ Hermes")
    assert narrow[-1].startswith("╰")
    assert all(len(line.lstrip("\n")) <= 50 for line in narrow if line)
    assert all(len(line.lstrip("\n")) <= 90 for line in wide if line)


def test_assistant_response_entry_realigns_markdown_table_at_replay_width():
    source = "\n".join([
        "| Feature | Notes |",
        "| --- | --- |",
        "| Resize replay | A longer explanation that forces width-sensitive table formatting |",
        "| Raw fallback | Still keeps non-assistant _cprint lines static |",
    ])
    entry = cli.AssistantResponseEntry("⚕ Hermes", source, text_ansi="", markdown_mode="strip", completed=True)

    narrow = [cli._ANSI_CONTROL_RE.sub("", line) for line in entry.render(50)]
    wide = [cli._ANSI_CONTROL_RE.sub("", line) for line in entry.render(90)]

    assert narrow != wide
    assert "Resize replay" in "\n".join(narrow)
    assert "Resize replay" in "\n".join(wide)
    assert all(len(line.lstrip("\n")) <= 50 for line in narrow if line)
    assert all(len(line.lstrip("\n")) <= 90 for line in wide if line)


def test_render_rich_to_ansi_renders_panel_at_requested_width():
    from rich.panel import Panel
    from rich.text import Text

    renderable = Panel(
        Text("A rich panel should be rendered from source at replay width."),
        title="Replay",
    )

    narrow = [cli._ANSI_CONTROL_RE.sub("", line) for line in cli.render_rich_to_ansi(renderable, 80)]
    wide = [cli._ANSI_CONTROL_RE.sub("", line) for line in cli.render_rich_to_ansi(renderable, 120)]

    assert narrow != wide
    assert len(narrow[0]) == 80
    assert len(wide[0]) == 120


def test_rich_renderable_entry_replays_panel_at_requested_width():
    from rich.panel import Panel
    from rich.text import Text

    entry = cli.RichRenderableEntry(Panel(Text("semantic rich output"), title="Tool Output"))

    narrow = [cli._ANSI_CONTROL_RE.sub("", line) for line in entry.render(80)]
    wide = [cli._ANSI_CONTROL_RE.sub("", line) for line in entry.render(120)]

    assert narrow != wide
    assert len(narrow[0]) == 80
    assert len(wide[0]) == 120
    assert "Tool Output" in narrow[0]
    assert "Tool Output" in wide[0]


def test_resume_history_panel_records_semantic_rich_entry(monkeypatch):
    cli._configure_output_history(True, 10)
    printed = []

    dummy = object.__new__(cli.HermesCLI)
    monkeypatch.setattr(dummy, "_console_print", lambda renderable: printed.append(renderable))

    dummy.conversation_history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "A previous assistant answer that should reflow."},
    ]
    dummy.resume_display = "full"

    dummy._display_resumed_history()

    entries = list(cli._OUTPUT_HISTORY)
    assert len(printed) == 1
    assert len(entries) == 1
    assert isinstance(entries[0], cli.RichRenderableEntry)
    narrow = [cli._ANSI_CONTROL_RE.sub("", line) for line in entries[0].render(80)]
    wide = [cli._ANSI_CONTROL_RE.sub("", line) for line in entries[0].render(120)]
    assert narrow != wide
    assert len(narrow[0]) == 80
    assert len(wide[0]) == 120


def test_stream_response_history_uses_single_semantic_assistant_entry(monkeypatch):
    cli._configure_output_history(True, 20)
    printed = []
    monkeypatch.setattr(cli, "_pt_print", lambda value: printed.append(value))
    monkeypatch.setattr(cli, "_PT_ANSI", lambda text: text)

    class Dummy:
        show_timestamps = False
        show_reasoning = False
        final_response_markdown = "strip"
        _stream_box_opened = False
        _stream_buf = ""
        _stream_table_buf = []
        _in_stream_table = False
        _stream_text_ansi = ""

        def _close_reasoning_box(self):
            pass

    dummy = Dummy()
    cli.HermesCLI._emit_stream_text(dummy, "one long assistant response line that is replayable\n")
    cli.HermesCLI._flush_stream(dummy)

    entries = list(cli._OUTPUT_HISTORY)
    assert len(entries) == 1
    assert isinstance(entries[0], cli.AssistantResponseEntry)
    assert entries[0].completed is True
    assert entries[0].source_text == "one long assistant response line that is replayable\n"
    assert not any(isinstance(entry, cli.StaticAnsiEntry) for entry in entries)


def test_suspend_output_history_blocks_recording():
    cli._configure_output_history(True, 10)

    with cli._suspend_output_history():
        cli._record_output_history("hidden")
        cli._record_output_history_entry("also hidden")

    assert list(cli._OUTPUT_HISTORY) == []


def test_display_history_skips_carriage_return_spinner_frames():
    history = cli.DisplayHistory(max_lines=10)

    history.record_text("\r  ⠋ running tool (0.1s)")
    history.record_text("\r                                        \r")
    history.record_text("persistent tool summary")

    assert [entry.render() for entry in history.entries] == [["persistent tool summary"]]


def test_display_history_preserves_crlf_transcript_lines():
    history = cli.DisplayHistory(max_lines=10)

    history.record_text("first line\r\nsecond line\r\n")

    assert [entry.render() for entry in history.entries] == [["first line"], ["second line"]]


def test_transient_cprint_prints_without_recording_history(monkeypatch):
    cli._configure_output_history(True, 10)
    printed = []
    monkeypatch.setattr(cli, "_pt_print", lambda value: printed.append(value))
    monkeypatch.setattr(cli, "_PT_ANSI", lambda text: text)

    cli._cprint_transient("  ┊ ⚡ preparing terminal…")
    cli._cprint("legitimate transcript line")

    assert printed == ["  ┊ ⚡ preparing terminal…", "legitimate transcript line"]
    assert list(cli._OUTPUT_HISTORY) == ["legitimate transcript line"]


def test_tool_generation_status_is_not_replayed(monkeypatch):
    cli._configure_output_history(True, 10)
    printed = []
    monkeypatch.setattr(cli, "_pt_print", lambda value: printed.append(value))
    monkeypatch.setattr(cli, "_PT_ANSI", lambda text: text)

    dummy = object.__new__(cli.HermesCLI)
    dummy._stream_box_opened = False
    dummy._close_reasoning_box = lambda: None
    cli.HermesCLI._on_tool_gen_start(dummy, "terminal")

    assert printed == ["  ┊ 💻 preparing terminal…"]
    assert list(cli._OUTPUT_HISTORY) == []


def test_clear_output_history_removes_replayable_lines():
    cli._configure_output_history(True, 10)
    cli._record_output_history("before clear")

    cli._clear_output_history()

    assert list(cli._OUTPUT_HISTORY) == []
