"""Regression test for the GUI teardown crash caught by gui_debug.py:

    _tkinter.TclError: can't invoke "destroy" command: application has been destroyed
    (tds_macro/gui.py on_close -> root.destroy())

Root cause: on_close (the WM_DELETE_WINDOW handler) calls root.update(), which
re-pumps the Tk event loop and can deliver a second WM_DELETE_WINDOW (re-entering
on_close) before the outer call reaches root.destroy() — so destroy() ran against an
already-destroyed application. The handler must now tear down exactly once and tolerate
an already-destroyed root.

Needs a usable Tk display; skipped automatically on a headless box so the core suite
still runs anywhere.
"""

import pytest

from tds_macro import gui  # imports tkinter lazily inside run_gui, so safe to import here

tk = pytest.importorskip("tkinter")


@pytest.fixture
def display_ok():
    try:
        r = tk.Tk()
        r.destroy()
    except tk.TclError as e:  # headless / no $DISPLAY
        pytest.skip(f"no usable Tk display: {e}")


def _run_gui_capturing_on_close(monkeypatch):
    """Build the real GUI but capture its WM_DELETE_WINDOW handler and skip the
    blocking mainloop, so a test can drive on_close directly."""
    captured = {}
    orig_protocol = tk.Tk.protocol

    def grab(self, name=None, func=None):
        if name == "WM_DELETE_WINDOW" and func is not None:
            captured["on_close"] = func
        return orig_protocol(self, name, func)

    monkeypatch.setattr(tk.Tk, "protocol", grab)
    monkeypatch.setattr(tk.Tk, "mainloop", lambda self: None)  # don't block the test
    rc = gui.run_gui()
    assert rc == 0
    assert "on_close" in captured, "run_gui did not register a WM_DELETE_WINDOW handler"
    return captured["on_close"]


def test_on_close_second_delivery_does_not_crash(display_ok, monkeypatch):
    """A second WM_DELETE_WINDOW delivery (what root.update() re-pumped) must be a
    no-op, not a TclError from destroying an already-destroyed app."""
    on_close = _run_gui_capturing_on_close(monkeypatch)
    on_close()        # the real teardown
    on_close()        # the re-pumped second delivery — must not raise


def test_on_close_survives_reentrant_close(display_ok, monkeypatch):
    """Mirror the actual crash path: on_close re-enters *during* its own root.update()."""
    on_close = _run_gui_capturing_on_close(monkeypatch)

    state = {"reentered": False}

    def reentrant_update(self):
        if not state["reentered"]:
            state["reentered"] = True
            on_close()            # the event loop delivering a queued second close
        # the root may now be gone; don't forward to the real update()

    monkeypatch.setattr(tk.Misc, "update", reentrant_update)
    on_close()                    # must not raise despite the re-entrant teardown
    assert state["reentered"]     # the re-entry actually happened (test is meaningful)
