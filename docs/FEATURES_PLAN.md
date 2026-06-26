# Feature plan: private-server join + record/playback

Two requested features. Plan first (per request), implement after.

---

## Feature B — Record your actions & play them back  *(status: ALREADY BUILT)*

This is the core of the tool today:
- **Record:** `python -m tds_macro record my.strat.json --map "..."` — `recorder.py` captures
  your live mouse moves / clicks / drags / key presses / scrolls **and their timing**, as
  window-relative (resolution-independent) coordinates, into a human-editable JSON strat.
  `F10` drops a visual sync-point (reference frame); `F8` stops.
- **Play back:** `python -m tds_macro play my.strat.json` — `engine.py` replays it with the
  visual-sync timing engine + recovery + auto-loop.

**Small additions to finish it off:**
1. When recording, also stamp the private-server link (see Feature A) into the strat header,
   so a recorded strat always rejoins the same server on playback.
2. Optional `record --play-after` convenience (record, then immediately replay once to verify).

---

## Feature A — Private-server link (always join that server)  *(NEW)*

**Goal:** the macro always (re)joins one specific private server — at start *and* after every
recovery/loop restart — by **opening the private-server link**, instead of (or before) clicking
through the lobby. This is strictly more reliable than UI navigation and is the natural fix for
"always joins that server."

### How joining works today
`engine.run()` plays `strat.join_sequence` (recorded lobby → map → ready clicks) at the top of
every loop iteration, and recovery REJOIN re-runs the same loop. We add a join *method* that
opens the link instead.

### Design
- **Store the link:** `header.private_server_url` in the strat (per-strat, since it's
  server-specific) + CLI `--private-server URL` (on `record` to bake it in, on `play` to
  override) + `config.private_server_url` override. (Unify with the existing `config.relaunch_url`,
  which already does the same `open <url>` for hard-disconnect recovery.)
- **New `launcher.py`** — `open_url(url)`:
  - macOS real impl: `subprocess.run(["open", url], ...)` (list form → no shell injection).
  - `MockLauncher`: records calls, for tests/Linux.
  - factory `make_launcher(config)`.
- **New engine `_join()` step** (replaces the inline `if join_sequence:` block; used at loop
  start AND, via the existing restart path, on recovery rejoin — one join path, per design #4):
  1. If `private_server_url` is set → `launcher.open_url(url)`, then **wait for Roblox to be
     frontmost** (activate if needed) and for the in-match/lobby anchor before proceeding
     (reusing `expected_map_check` / a join `sync_point` so it's lag-tolerant).
  2. Optionally play a short `join_sequence` afterward to dismiss a browser "Launch Roblox"
     prompt or click Play (if the user's setup needs it).
  3. Else (no link) → play `join_sequence` (current behavior, unchanged).
- **Recovery:** `_relaunch_experience` and the rejoin both route through `launcher.open_url` with
  `private_server_url`. Leave/reset (Roblox client menu) is unchanged.
- **Validation:** the URL must look like a Roblox link (`https://…roblox.com…`,
  `https://ro.blox.com/…`, or `roblox://…`); otherwise a collected validation problem. Empty is
  allowed (falls back to `join_sequence`).

### Phases
0. **Config/schema** — `header.private_server_url` + `config.private_server_url`; URL validation;
   `--private-server` CLI flag; `validate`/`calibrate` show it.
1. **`launcher.py`** — `open_url` + `MockLauncher` + factory + tests.
2. **Engine `_join()`** — open-link-then-wait; wire into the loop; unify recovery rejoin/relaunch.
3. **Focus + browser-prompt handling** — after `open`, wait for Roblox frontmost (`activate`);
   optional `join_sequence` as the confirm/launch-click step.
4. **Recorder** — bake `private_server_url` into the header on `record`.
5. **Tests + docs + example** — mock launcher e2e, validation tests; README; example strat with a
   placeholder private-server URL.

### Possible errors (and mitigations)
- **Invalid/empty URL** → validation problem (Feature-A phase 0); empty → fall back to join_sequence.
- **Browser shows a "Launch Roblox"/"Join" button** that needs a click → the URL alone won't
  enter the game. *Mitigation:* support an optional `join_sequence` played after the open (records
  the confirm click), and/or document setting the browser to auto-open `roblox-player` links. The
  in-match wait + recovery catches the case where the game never loads.
- **`open` focuses the browser, not Roblox** → events would fire into the browser. *Mitigation:*
  after open, wait for Roblox frontmost (`window.activate()` + `is_frontmost`) before any input;
  never inject while not foreground (existing FOCUS_LOST guard).
- **Roblox not installed / `roblox://` not registered** → `open` fails / nothing launches →
  in-match wait times out → recovery → bounded retries → STOP+notify.
- **Private server full / link expired / kicked at join** → join never completes → in-match wait
  timeout → recovery (bounded) → STOP+notify (don't spin forever).
- **Shell injection via URL** → `subprocess.run([...])` list form, never `shell=True`.
- **Linux/CI** → `MockLauncher` no-ops (records the call); real join is macOS-only (already the case).
- **Re-launch storms** → opening the link every restart could spam launches; bound by
  `max_consecutive_restarts` + per-cause caps (already in place).

### Outline (files touched)
- `config.py` — `private_server_url: str = ""` (+ treat `relaunch_url` as its alias/back-compat).
- `strat.py` — `Header.private_server_url`; URL validation in `parse`; serialize in `to_dict`.
- `launcher.py` *(new)* — `Launcher` protocol, `MacLauncher.open_url`, `MockLauncher`, `make_launcher`.
- `engine.py` — `_join()`; replace the inline join block; call at loop start; focus-wait.
- `recovery.py` — `_relaunch_experience` via `launcher` + `private_server_url`.
- `cli.py` — `--private-server` on `record`/`play`; `validate` prints it; build the launcher in `_build_backends`.
- `recorder.py` — write `header.private_server_url` from config on `record`.
- `tests/` — launcher mock tests, URL validation, `_join` open-then-wait e2e, recorder stamping.
- `examples/make_example.py` — add a placeholder `private_server_url`.

### One thing only you know (need before I build phase 3)
How your TDS private-server link actually launches the game on a Mac determines whether I need a
"click the Launch button" step — see the question I'll ask alongside this plan.
