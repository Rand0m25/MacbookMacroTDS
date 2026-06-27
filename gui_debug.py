"""Debug launcher for the TDS Macro GUI — finds out *why* the window renders blank.

Run it from the repo root on the Mac:

    cd ~/MacbookMacroTDS
    git pull origin main
    .venv/bin/python gui_debug.py

It launches the GUI exactly like `python -m tds_macro gui`, but writes a detailed
report to `gui_debug.log` next to this file:
  - Tk / Tcl / Python versions
  - any uncaught exception (main thread, worker threads) or Tk callback exception
  - native crashes (faulthandler)
  - after a forced redraw: the geometry + mapped state of the window and EVERY widget,
    so we can tell whether the widgets are missing, zero-size, or present-but-not-drawn.

Reproduce the blank window, then CLOSE it and send me `gui_debug.log` (or
`cat gui_debug.log`).
"""
from __future__ import annotations

import faulthandler
import os
import sys
import threading
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

LOG = os.path.join(HERE, "gui_debug.log")
_f = open(LOG, "w", buffering=1)  # line-buffered: nothing lost on a hard crash


def w(tag, text=""):
    _f.write(f"\n===== {tag} =====\n{text}\n")
    _f.flush()
    sys.__stderr__.write(f"[gui_debug] {tag}\n")


# native crashes / segfaults -> dumped to the log
faulthandler.enable(file=_f, all_threads=True)

# uncaught exceptions, main + worker threads
sys.excepthook = lambda t, e, tb: w(
    "UNCAUGHT (main thread)", "".join(traceback.format_exception(t, e, tb)))
threading.excepthook = lambda a: w(
    f"UNCAUGHT (thread {a.thread.name})",
    "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))

import tkinter as tk  # noqa: E402

w("VERSIONS", f"TkVersion={tk.TkVersion}  TclVersion={tk.TclVersion}\npython={sys.version}")

# Tk callback exceptions (button handlers, the poll loop, etc.)
_orig_report = tk.Tk.report_callback_exception


def _report(self, exc, val, tb):
    w("TK CALLBACK EXCEPTION", "".join(traceback.format_exception(exc, val, tb)))
    _orig_report(self, exc, val, tb)


tk.Tk.report_callback_exception = _report


def _info(widget):
    try:
        return (f"{widget.winfo_class():<12} mapped={int(widget.winfo_ismapped())} "
                f"geo={widget.winfo_width()}x{widget.winfo_height()}"
                f"+{widget.winfo_x()}+{widget.winfo_y()} "
                f"req={widget.winfo_reqwidth()}x{widget.winfo_reqheight()} "
                f"manager={widget.winfo_manager()!r}")
    except Exception as e:  # noqa: BLE001
        return f"<info error: {e}>"


def _dump_tree(root):
    try:
        root.update_idletasks()
        root.update()
    except Exception:  # noqa: BLE001
        w("UPDATE() RAISED", traceback.format_exc())
    lines = [f"ROOT  {_info(root)}  geometry={root.winfo_geometry()}  "
             f"viewable={int(root.winfo_viewable())}"]

    def walk(node, depth):
        for child in node.winfo_children():
            lines.append("  " * depth + _info(child))
            walk(child, depth + 1)

    walk(root, 1)
    w("WIDGET TREE (after forced redraw)", "\n".join(lines))


# patch mainloop so we dump the live widget tree right before the loop blocks,
# then hand off to the real mainloop so the window still behaves normally.
_real_mainloop = tk.Misc.mainloop


def _patched_mainloop(self):
    try:
        _dump_tree(self.winfo_toplevel())
    except Exception:  # noqa: BLE001
        w("DUMP RAISED", traceback.format_exc())
    return _real_mainloop(self)


tk.Misc.mainloop = _patched_mainloop

from tds_macro.cli import build_parser  # noqa: E402

args = build_parser().parse_args(["gui"])
print(f"[gui_debug] launching GUI; report -> {LOG}")
print("[gui_debug] reproduce the blank window, then CLOSE it.")
try:
    rc = args.func(args)
    w("EXIT", f"GUI returned {rc}")
except BaseException:  # noqa: BLE001
    w("EXIT-CRASH", traceback.format_exc())
    raise
finally:
    _f.flush()
