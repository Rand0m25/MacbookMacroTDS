# GUI plan + code outline

A control-panel GUI over the existing engine/recorder, in **Tkinter** (Python stdlib → no
new deps, works on macOS, matches the stdlib-core ethos). Functionality-first.

## Architecture: testable controller + thin view

The hard rule that makes "each button does what it's intended" verifiable: **all behavior lives
in a Tk-free `GuiController`** (pure logic, dependency-injected), and the Tk widgets are a thin
view that only (a) call controller methods on click and (b) poll `controller.status()` to redraw.
The controller is unit-tested with fake factories — no display needed.

```
view (Tk)  --click-->  GuiController.<action>()  --->  engine/recorder on a worker thread
   ^                                                          |
   +------------- after(150ms) poll: controller.status() <----+  (+ on_event callbacks for log/errors)
```

## Files
- `tds_macro/gui.py`
  - `GuiDeps` (dataclass of factories, real defaults, lazily imported): `build_config`,
    `build_backends`, `make_hotkeys`, `make_clock`, `make_player`, `make_recorder`,
    `load_strat`, `consent_ok`, `set_consent`. Tests inject fakes.
  - `GuiController` — the logic (below). No `tkinter` import.
  - `run_gui(config)` — builds the Tk view, wires buttons → controller, runs `mainloop()`;
    `import tkinter` happens lazily here so importing `gui` (for tests) never needs Tk.
- `tds_macro/cli.py` — `gui` subcommand → `cmd_gui` → `run_gui(config)`.
- `tests/test_gui_controller.py` — unit tests for every action.

## GuiController API (each maps to a button/action)
- `validate(path) -> (ok: bool, problems: list[str])` — **Validate** button.
- `start_record(path, *, name, map, difficulty, private_server) -> bool` — **Record** button.
  Guards: refuses if already busy. Runs `Recorder.run()` on a worker thread; saves on finish.
- `start_play(path, *, loop_count, dry_run, private_server, accept_ban_risk) -> bool` —
  **Play** button. Guards: busy; consent (returns a `consent_required` event if missing).
  Builds config (overrides from the strat), backends, hotkeys, player; runs on a worker thread.
- `pause_toggle()` — **Pause/Resume** button (flips the hotkey pause event).
- `stop()` — **Stop / Panic** button + window close (sets panic+stop, joins the worker).
- `status() -> dict` — for the status line: `busy, activity('idle'|'record'|'play'), state,
  runs, wins, losses, recoveries, sync_timeouts`.
- `is_busy() -> bool`.
- events via `on_event(kind, payload)`: `log`, `error`, `done`, `consent_required`, `state`.

## Buttons/actions ↔ intended behavior (the checklist tests enforce)
| Control | Intended behavior | Test |
|---|---|---|
| Browse… | open file dialog, set path | (view-only; skipped) |
| Validate | show ✓/problems from `strat.parse` | `test_validate_*` |
| Record | start recorder thread; toggle to Stop; save on stop | `test_record_*` |
| Play | start player thread with loop/dry-run/link; toggle to Stop | `test_play_*` |
| Pause | flip pause event; status shows PAUSED | `test_pause_toggle` |
| Stop/Panic | set panic+stop; worker ends; inputs released | `test_stop_*` |
| Private-server field | flows into config for record+play | `test_play_uses_private_server` |
| Loop count / Dry-run | flow into config | `test_play_passes_options` |
| Accept ban-risk | gates Play; persists consent | `test_play_requires_consent` |
| Busy guard | can't start two activities at once | `test_busy_guard` |
| Status poll | reflects player.stats/state | `test_status_snapshot` |
| Window close | calls stop() | (wired to `stop`) |

## Threading & safety
- One worker thread at a time (`busy` guard). The worker runs `player.run()`/`recorder.run()`
  inside try/except; on exit emits `done`/`error` and clears busy.
- `stop()` sets the hotkey `panic`+`stop` events (the engine's existing abort channel) and
  `join()`s the worker (bounded timeout). Idempotent.
- Status polling is read-only (`player.stats` + `player.state`); the view uses `after()`.
- Global hotkeys (F8 panic, F7 pause) still start during play, so the physical panic key works
  even if the GUI is unfocused.

## Possible errors (mitigations)
- **Tk missing/headless** → `run_gui` import is lazy; CLI prints a clear message if `tkinter`
  import fails. Controller never imports Tk, so the suite runs headless.
- **Worker exception** (window not found, bad strat) → caught → `error` event → status back to idle.
- **Double-start / start while busy** → guarded, returns False + `error` event.
- **Stop before start / double stop** → idempotent no-op.
- **Closing window mid-run** → `stop()` on close → worker joined, inputs released.
- **Consent not accepted** → Play refuses, emits `consent_required` (view shows the ban warning).
- **Blocking the UI thread** → all engine work is on the worker; the UI only polls.

## Test plan (continues the existing discipline)
Unit-test `GuiController` with fake factories (a fake player/recorder that records calls + lets
the test end the run), asserting each action's effect. Then run the full suite + lint, then
resume the multi-agent review loop (after the 5:40pm PT session-limit reset) now including `gui.py`.
