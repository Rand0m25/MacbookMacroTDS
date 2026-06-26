# Bug log

Mistakes found during advanced/real-environment testing, with root cause, blast
radius (where else the same class of mistake could live), and the fix. Per the
"document what went wrong + scan for the same error elsewhere" discipline.

---

## BUG-1 — deprecated `mss.mss()` factory (capture)
- **Found by:** running real `mss` 10.2.0 capture under Xvfb — emitted
  `DeprecationWarning: mss.mss is deprecated; use mss.MSS instead`.
- **What went wrong:** `MssCaptureBackend._sct()` called `mss.mss()`. That name is
  deprecated in mss ≥10 and slated for removal, so a future `pip install mss` on
  the user's Mac would eventually break screen capture (with no error at build time).
- **Root cause:** the API was taken from older mss documentation during design.
- **Blast radius scan:** `grep mss.mss(` → only `capture.py:70`. Also scanned for
  other version-fragile APIs (`np.float_/np.bool/...`, `datetime.utcnow`) → none.
- **Fix:** `factory = getattr(mss, "MSS", None) or mss.mss; sct = factory()` —
  prefers the new class, falls back for older mss. (`tds_macro/capture.py`)
- **Status:** fixed; real Xvfb capture now clean.

---

## BUG-2 — test artifact: smooth gradient renders unfaithfully to the X root
- **Found by:** real Xvfb e2e — `score(captured_gradient, gradient_ref)` was 0.11.
- **Diagnosis (not a code bug):** a textured `plasma:` scene captured + scored
  **1.000** (grayMAD 0.2), proving capture + comparator are correct. The smooth
  `gradient:black-white` came back with grayMAD 126 (src variance 270 →
  captured 5467): `display -window root` dithers/re-renders a smooth gradient, and
  smooth ramps are degenerate for normalized cross-correlation anyway.
- **Blast radius:** test-only. Real Roblox frames are textured (like plasma), so
  the production comparator is unaffected; but it's a reminder to pick **textured,
  high-variance ROIs** for sync points (already advised in README "Tips").
- **Fix:** the e2e "matches its own reference" test now uses the plasma scene as
  the match target and the gradient source only as the negative. (`tests/test_xvfb_realcapture.py`)
- **Status:** fixed.

## Review round 1 — 15 confirmed defects (5 high, 9 medium, 1 low), all fixed

**strat.py — raw numeric/type coercion crashed validation (M12 violation).**
- `_base`, `_build_event` (11 sites), `_expect`, `_detector`, `run_end`, and the
  `sync_point` numerics used bare `int()/float()` with the `x or default` idiom,
  which does NOT guard a truthy non-numeric string → an uncaught `ValueError`
  escaped `parse()/load()` instead of a collected `StratValidationError`. A
  non-dict `header` hit `.items()` → `AttributeError`. **Root cause:** the `or`
  idiom only neutralizes falsy values. **Blast radius:** every numeric JSON field
  + header. **Fix:** added a guarded `_num(v, default, name, ctx, problems, cast)`
  helper and routed all numeric coercions through it; type-guarded `Header.from_dict`.

**engine.py D7 (high) — pause/recovery wall-time collapsed event spacing.**
- `clock_offset` only grew at fired syncs; a pause (`_maybe_pause`) or a RESUME
  recovery consumed wall time without absorbing it, so every later event fired
  back-to-back (defeats the anti-cheat min-spacing). **Fix:** `_absorb_wall_time()`
  adds consumed off-timeline wall time to the monotonic `clock_offset`; called
  after pause and after a RESUME recovery.

**engine.py D8 (medium) — `on_timeout="retry"` was a silent no-op** (acted like
`continue`). **Fix:** `_play_sequence` now re-polls the barrier up to
`config.sync_max_retries`, then escalates to recover.

**engine.py D9 (low) — recover classified on the ROI crop, not the full window.**
**Fix:** `_handle_sync_timeout` grabs a fresh full-window frame for `classify`.

**visual.py D10/D11 (high) — `_ncc`/`_sqdiff_sim` returned 1.0 when *either* frame
was flat** → a black/blank screen matched ANY reference (false recovery / false
sync). **Root cause:** `denom==0` fired if either factor was zero. **Fix:** only
"both flat & equal level" → 1.0; exactly one flat → ~0.

**recovery.py D12 (high) — unreadable recovery frame fell back to a flat
placeholder** which (via D10) matched everything. **Fix:** D10/D11 kill the flat
match; `_load_ref` now warns loudly (and `load()` already validates frames exist).

**recorder.py D14 (high) — key events weren't frontmost-gated** (R27 claim was
false). **D15 (high)** — an out-of-window button *release* was dropped, stranding
the press in the coalescer (no click/drag emitted, state corrupted). **Fix:**
`_on_press/_on_release` gate on frontmost and pair every recorded press with its
release via `_pressed_keys`; `_on_click` delivers a clamped release for any
tracked button even off-window.

**cli.py D13 (medium) — record prompt advertised a pause key that doesn't stop
recording.** **Fix:** prompt now names the panic hotkey.

## Review round 2 — 5 confirmed defects (1 high, 3 medium, 1 low), all fixed

- **strat.py (medium ×2):** `config_overrides` and `recovery` weren't validated as
  dicts. A non-dict `config_overrides` crashed `Config.with_overrides().items()`
  with an uncaught `AttributeError`; a non-dict `recovery` was silently dropped
  (safety subsystem disabled with no error). **Fix:** both now record a
  `StratValidationError` problem, matching `run_end`/`expected_map_check`.
- **engine.py D3 (high):** `_iter_t0`/`clock_offset` weren't reset between the
  independently-recorded `join_sequence` and `events` — so after the join consumed
  wall time, every early event fired back-to-back (spacing collapse, same class as
  D7). **Fix:** `_play_sequence` rebases the timeline to "now" at its start.
- **recorder.py D4 (low):** `_on_scroll` wasn't frontmost-gated (R27). **Fix:** added.
- **engine/cli D5 (medium):** an unexpected exception in `Player.run` (e.g. the
  Roblox window vanishing → `WindowNotFoundError` from `_arm`) escaped the worker
  thread, leaving `cmd_play` to return success silently. **Fix:** `Player.run` now
  has a broad `except` → graceful stop with `stopped_reason`, inputs still released
  in `finally`; `_run_with_signals` captures + surfaces any worker crash.

(One reviewer sub-call hit the StructuredOutput retry cap and didn't return; its
slice is re-covered in subsequent rounds.)

## Review round 3 — 2 confirmed defects (1 high, 1 medium), all fixed

- **engine.py (high):** the top-level `run` loop had no `_abort_check()` and, for a
  degenerate strat (empty `events`, no `run_end`, empty `join_sequence`) with the
  default `loop_count=0`, every helper early-returned without sleeping → a tight
  busy-spin that pegged a CPU core AND could not be stopped by panic/stop. **Fix:**
  `_abort_check()` at the top of the loop + a panic-aware floor sleep
  (`min_inter_event_ms`) on any zero-work iteration.
- **cli.py (medium):** `cmd_play` returned exit 0 even when the worker crashed
  (`stats is None`), so a failed run reported SUCCESS to CI/shell. **Fix:** return 1
  when `stats is None`.

(Two reviewer slices timed out this round and didn't return — review workflow was
then re-sliced into smaller units so subsequent rounds complete fully.)

## Review round 4 — 8 confirmed defects (1 high, 4 medium, 3 low), all fixed

Round 4 used a rebuilt per-file workflow (15 small slices, completeness-tracked);
it completed fully. (4 verify sub-calls hit a session usage limit; their findings
were left for the next round.)

- **engine.py (high):** `on_timeout="continue"` returned after a full-length
  timeout WITHOUT rebasing the clock → later events fired late/back-to-back.
  **Fix:** `_handle_sync_timeout` rebases on the continue path too.
- **config.py (medium):** `window_rect_override` wasn't shape-checked — a JSON
  string crashed `int(',')`, a digit string silently became `(1,2,3,4)`, a short
  list later crashed `x,y,w,h = rect`. **Fix:** validate it's a 4-number list/tuple
  (raise a clear error); `cmd_play` catches it as a clean message + exit 1.
- **strat.py `_num` (low):** `json.loads` accepts `NaN/Infinity` → `int(inf)` raised
  `OverflowError`. **Fix:** reject non-finite floats as a collected problem.
- **strat.py `_enum` (low):** an unhashable enum value (list/dict) crashed the
  `in` membership test. **Fix:** require a string first.
- **strat.py detectors/run_end (medium):** `_detector`, `_expect`, and `run_end`
  skipped `_no_unknown`, so typo'd keys were silently ignored. **Fix:** added.
- **recovery.py (medium):** `_run_sequence` had no `ScrollEvent` branch → a scroll
  in a recorded leave/reset sequence was silently dropped. **Fix:** added.
- **input_backend.py (medium):** `press_key` recorded held keys only AFTER pressing
  both modifier+key, so a mid-sequence exception could leave a physically-pressed
  key untracked (→ not released by `release_all`). **Fix:** record each key before
  pressing it.
- **geometry.py `region_crop_box` (low, dormant):** a region at the far edge could
  produce an out-of-bounds/empty box; no production caller today. **Fix:** clamp
  lower bounds to `img-1` first, guaranteeing `0<=lo<hi<=img`.

## Review round 5 — 7 confirmed defects (2 medium, 5 low), all fixed

- **strat.py `parse` schema_version (low ×2):** bypassed the `_num` finiteness
  guard, so `"schema_version": Infinity/NaN` crashed `int(inf)` with
  OverflowError/ValueError instead of a clean validation error. **Fix:** finiteness
  check + compute the int once.
- **input_backend.py codec (low):** `pynput_to_name`/`key_to_pynput` were lossy for
  vk-only keys (`str(key)`→`"<65>"`→`from_char("<")` = wrong key). **Fix:** encode
  vk-only keys as `"vk:<n>"` and decode via `KeyCode.from_vk` (lossless round-trip).
- **permissions.py (medium):** when the window couldn't be located, the
  Screen-Recording check short-circuited to **True**, so `check-perms` reported OK
  while it actually couldn't verify. **Fix:** geo `None` → `screen_recording=False`
  + explicit message.
- **recorder.py `EventCoalescer` (medium):** the mouse listener, keyboard listener,
  and main (mark-sync) thread all mutated it with no locking → races on the event
  list / `_down` / `_pending_click` / `_next_id`. **Fix:** an `RLock` taken by every
  public mutator.
- **cli.py (low ×2):** `validate`/`play` caught `StratValidationError` but not the
  `OSError` from `open()` (missing/dir path); `calibrate` didn't wrap `load()` at
  all. **Fix:** catch `(StratValidationError, OSError)` in all three.

## Proactive blast-radius scan (between rounds 5 and 6)

Scanned for the *classes* of bug found so far, beyond what reviewers flagged:
- **A — raw `int()/float()` on JSON:** swept all occurrences; all strat ones go
  via `_num` now. `Point/Rect.from_dict` still do raw `float()` but are NOT on the
  parse path (parser uses `_point`/`_rect`) — dead there, left as-is.
- **B — Header numeric fields (FIXED):** `Header.from_dict` copied `window_aspect`
  / `retina_scale_captured_at` / `reference_resolution` raw, so a malformed value
  crashed `calibrate` (`abs(...)` / `f"{...:.3f}"`) and `engine._arm`. Added
  `_safe_float` coercion in `from_dict`. (Same class as the `_num` fixes.)
- **C — unhashable membership:** all `in <set>` sites take strings; `_enum` already
  guarded. No further action.
- **D — graceful stop (FIXED):** `cmd_record` had `try/finally` but no `except`, so
  a window-not-found at record start threw an uncaught traceback. Added an `except`
  → clean message + exit 1 (matches `cmd_play`).

## Workflow recheck — 9 confirmed defects (2 high, 4 medium, 3 low), all fixed + verified

Found after the 3 *narrow* manual passes had (wrongly) been called clean — see the
"why the manual passes missed these" note below. Each was independently reproduced
before fixing.

- **strat.py (medium) booleans:** `confirm_click`/`confirm`/`require_settled` used raw
  `bool()`, so `"false"`/`"no"` became `True` (silent inversion of intent, M12 violation).
  **Fix:** strict `_bool()` helper → reports the typo.
- **strat.py (medium) negative delays:** `settle_ms`/`between_ms` had no lower bound; a
  negative value reordered the expanded timeline. **Fix:** require `>= 0`.
- **recovery.py (high) `_run_sequence`:** replayed the leave/reset sequence back-to-back
  with NO inter-event delay (recorder stores gaps as `t_ms`, not WaitEvents) → menu clicks
  too fast for Roblox. **Fix:** honor the recorded `t_ms` schedule (`sleep_until`).
- **recorder.py (high) `run`:** the poll-loop `_refresh_geo()` was unguarded → a transient
  window blip mid-recording aborted the whole recording. **Fix:** wrapped (skip the tick).
- **engine.py (medium) `_maybe_break_between_runs`:** fired the break timer at `runs==0`
  (`0 % N == 0`) on every restart. **Fix:** require `runs > 0`.
- **config.py (low) str fields:** `_coerce_type` didn't coerce str fields → `window_title_match`
  as an int → `AttributeError` on the real macOS provider. **Fix:** coerce to `str`.
- **window.py (low) `_find_window`:** `best_area=-1` selected a zero-size minimized window
  (`0 > -1`). **Fix:** `if area <= 0: continue`.
- **geometry.py (low) division:** `logical_to_norm`/`physical_to_norm` could divide by a
  0-size window / 0 retina. **Fix:** `max(1, w/h)` + `retina or 1.0`.
- **cli.py (low) `cmd_calibrate`:** a missing/unreadable sync frame raised uncaught.
  **Fix:** per-sync `try/except` → report + continue.

### Why the 3 manual passes missed these (the real lesson)
The manual passes were *narrow probes*, not exhaustive per-function review: Pass 1 = crash
fuzz (only catches *raised* exceptions — a `bool("false")==True` inversion doesn't raise),
Pass 2 = timing/concurrency scenarios, Pass 3 = round-trip + CLI exit codes. They
structurally could not catch non-crashing semantic bugs or untested edge cases. The
multi-agent workflow does ~15 independent deep per-file reads and is the more thorough
"check". Going forward the binding check is the verified workflow review, repeated until
3 consecutive rounds find zero genuine defects.

## Workflow recheck round 2 — 9 confirmed (3 high, 3 medium, 3 low), all fixed + verified

- **config.py (high):** `_coerce_type` accepted JSON `null` for non-nullable fields (a
  regression from the R6 coercion work) → `None` reached `validate()` → uncaught
  `TypeError`. **Fix:** reject `null` except for the two nullable fields; `validate()`
  moved inside the guarded block.
- **engine.py (high):** the `recover`/retry-exhausted timeout path didn't absorb the
  elapsed timeout into `clock_offset` (only `continue` did) → a RESUME outcome
  collapsed downstream spacing. **Fix:** `_rebase_clock` before routing to recovery.
- **recovery.py (high):** `STUCK_SYNC` charged its own budget, then reclassified to a
  deeper cause; confirmed recoveries still hit STOP after `max_attempts` stuck-syncs.
  **Fix:** reset the `stuck_sync` counter before delegating.
- **strat.py (med/low):** `_num`(float)/`_coord`/`_safe_float` raised `OverflowError`
  on a huge (~400-digit) int → guarded. `button` field wasn't enum-validated → added.
- **recorder.py (med):** mark-sync failures were swallowed silently → now logged.
- **cli.py (low):** `cmd_calibrate` window lookup unguarded → wrapped.

## Workflow recheck round 3 — 3 confirmed (2 medium, 1 low), all fixed + verified

- **engine.py (med):** `_wait_run_end` slept `recovery_check_every_ms` with no floor →
  hang/busy-spin at 0 (the same hazard `_adaptive_wait` already floored). **Fix:**
  `max(1, ...)` + `validate()` rejects `recovery_check_every_ms <= 0`.
- **hotkeys.py (med):** the kill-switch watcher fired on a *pre-existing* file at
  startup (no baseline) → every run instant-aborted until the user deleted it.
  **Fix:** clear any stale file at `start()` so only a newly-created file triggers panic.
- **config.py (low):** `retina_scale_override <= 0` was unvalidated. **Fix:** `validate()`.

## Workflow recheck round 4 — 3 confirmed (2 medium, 1 low), all fixed + verified

- **strat.py (med):** the `recovery` block skipped `_no_unknown`, so a typo'd detector
  key (`wrng_map`) was silently dropped → that failure mode never recovers. **Fix:** added.
- **hotkeys.py (med):** `_to_pynput_combo` didn't normalize case → a single bad hotkey
  string (`F8`, unknown key, empty) made `GlobalHotKeys` throw, and the broad `except`
  disabled ALL hotkeys incl. **panic**. **Fix:** lowercase/normalize combos, validate +
  register each hotkey individually (one bad combo can't drop panic), log skips.
- **hotkeys.py (low):** `_killswitch_stop` was never cleared → a `start()` after `stop()`
  left the watcher dead. **Fix:** clear it in `start()`.

Trend across rechecks: **9 → 9 → 3 → 3** genuine defects — converging.

## Workflow recheck round 5 (wave-limited) — 4 confirmed (3 medium, 1 low), all fixed + verified

Ran in ~7 min (vs 2.7h) with no lost slices after capping concurrency at 4.
- **engine.py (med):** `_wait_run_end` ignored `FOCUS_LOST` from `_detect_failure` (which
  returns it first), so focus loss during the run-end wait was never recovered AND masked
  disconnect/wrong-map detection. **Fix:** route `FOCUS_LOST` to recovery (refocus) and continue.
- **permissions.py (med):** the `var > 1.0` Screen-Recording heuristic false-negatived on a
  legit near-uniform window (dark/loading/solid-colour) → blocked a valid run. **Fix:** only
  flat-AND-black (`var≈0 and mean≈0`) counts as denial.
- **hotkeys.py (med):** two hotkeys resolving to the same combo silently overwrote each other
  → pause could clobber **panic**. **Fix:** skip a colliding later binding (panic, registered
  first, wins) + warn.
- **recorder.py (low):** a drag that left the window mid-press dropped its moves → `max_dist`
  stale → misclassified as a click. **Fix:** track clamped moves while a button is held.

Trend: **9 → 9 → 3 → 3 → 4** genuine defects.

## Workflow recheck round 6 (wave-limited) — 4 confirmed (3 medium, 1 low), all fixed + verified

- **strat.py (med):** `ability` with `confirm:true` didn't require `confirm_pos`, and
  `expand_macro` silently dropped the confirm click → ability never fired. **Fix:**
  `confirm_pos` is required when `confirm` is true.
- **engine.py (med):** `require_settled` + an already-matching first frame → `seen_low`
  never set → barrier could never fire → spurious TIMEOUT→recovery. **Fix:** an
  already-stable match fires; `require_settled` now means "wait to settle", not "require
  a transition" (updated `test_require_settled_fires_on_stable_match`).
- **recovery.py (med + low):** `DEFEAT`/`VICTORY` (which `classify()` can return, incl.
  via a stuck-sync reclassify) went through `_over_budget` and were never reset → after
  `max_attempts` healthy runs the bot STOPped. **Fix:** short-circuit `DEFEAT`/`VICTORY`
  to `REJOIN` before charging any budget.

Trend: **9 → 9 → 3 → 3 → 4 → 4** genuine defects.

## Workflow recheck round 7 (wave-limited) — 3 confirmed (1 medium, 2 low), all fixed + verified

- **visual.py (med):** `load_reference` only caught `ImportError`, so a Pillow *decode*
  failure (`UnidentifiedImageError`) propagated instead of falling through to cv2/stdlib —
  the documented fallback chain was broken. **Fix:** separate import (skip backend) from
  decode (fall through).
- **pngio.py (low):** `read_png` raised an opaque `IndexError` on a corrupt PNG whose IHDR
  dims exceed the actual scanline data. **Fix:** length check → clear `ValueError`.
- **recorder.py (low):** `_strat_dir` wasn't set in `__init__` → `AttributeError` if
  `build()`/`capture_sync_point()` ran before `run()` (latent). **Fix:** init in `__init__`.

Trend: **9 → 9 → 3 → 3 → 4 → 4 → 3 → 3** genuine defects (all high/medium logic bugs fixed;
now in the low-severity robustness tail).

## Workflow recheck round 8 (wave-limited) — 2 confirmed (1 medium, 1 low), all fixed + verified

- **window.py (med):** `QuartzWindowProvider.activate` `return`ed after the first candidate
  regardless of osascript's exit code (`check=False`), so it never fell through to
  `RobloxPlayer` and silently no-op'd on failure. **Fix:** try all candidates, return only
  on `returncode == 0`, warn otherwise.
- **window.py (low):** a `window_rect_override` on the QUARTZ backend built a mock with
  retina 1.0 (vs Quartz's 2.0 default) → 2x-off physical coords. **Fix:** default to 2.0 on
  QUARTZ, 1.0 only for an explicit MOCK backend.

Trend: **9 → 9 → 3 → 3 → 4 → 4 → 3 → 3 → 2** genuine defects.

## Workflow recheck round 9 (wave-limited) — 5 raised; 4 fixed, 1 REJECTED as false positive

- **REJECTED (claimed HIGH): `MssCaptureBackend` "missing retina scaling".** Verified against
  the installed mss source: `darwin.py grab()` feeds the `{left,top,width,height}` dict to
  `CGWindowListCreateImage` as **logical CoreGraphics points** and returns a **physical-pixel**
  image. So passing logical `geo.*` is correct and live+reference frames are both physical-sized.
  Applying the suggested `*retina` would capture a 2x-too-large region — a regression. **No change.**
- **strat.py (low):** `place_tower` with `confirm_click=False` emitted NO placement click → an
  unplaceable tower. **Fix:** always emit the placement click.
- **engine.py (med):** the humanization break could re-fire on every restart iteration (runs a
  nonzero multiple, restarts don't increment runs). **Fix:** only break after a `completed` run.
- **input_backend.py (low):** `key_to_pynput` silently truncated an unrecognized multi-char key
  (dead-key/IME) to its first char. **Fix:** log a warning (non-silent).
- **cli.py (low):** `cmd_record` started the hotkey listener outside its try/finally → leak if
  construction raised. **Fix:** moved `hk.start()` + construction inside the try (mirrors `cmd_play`).

Trend: **9 → 9 → 3 → 3 → 4 → 4 → 3 → 3 → 2 → 4** genuine defects (+1 false positive rejected this round).

## Deterministic linters added (answering "why not find all at once")

Wired in `ruff` + `pyflakes` (+ `mypy` available) — they enumerate whole classes in ONE
pass that the probabilistic LLM reviews skip (they hunt logic bugs, not dead code). First
run found 25 (ruff) + 15 (mypy):
- **Fixed:** `recovery.py` `RecoveryController` was defined TWICE (Protocol shadowed by the
  concrete class) — renamed the Protocol to `RecoveryControllerProtocol`; 8 unused imports
  removed; recorder import-ordering (E402) fixed.
- **Noise (left):** mypy None-as-sentinel / Optional-narrowing / int-vs-float — not runtime
  bugs; the codebase is intentionally partially-typed. `mypy` is available but not a gate.
- **Gate:** `ruff check` + `pyflakes` are now CLEAN and enforced by `tests/test_lint.py`, so
  this class can't regress. (E702 semicolons intentionally ignored.)

## Workflow recheck round 10 (wave-limited) — 1 confirmed (low), fixed + verified

- **engine.py (low):** my own `require_settled` fix let an already-matching first frame fire
  without ever running the settle check at `stability_frames==1`, defeating S2 in that case.
  **Fix:** `require_settled` now requires ≥1 frame-to-frame settle comparison before firing.

Trend: **9 → 9 → 3 → 3 → 4 → 4 → 3 → 3 → 2 → 4 → 1** genuine defects.

## Workflow recheck round 11 (wave-limited) — 3 confirmed (1 medium, 2 low), all fixed + verified

- **strat.py (low):** `_num(cast=int)` silently truncated a fractional float (`t_ms=100.9 -> 100`).
  **Fix:** reject non-integral floats in int fields.
- **engine.py (med):** `_wait_run_end`'s deadline was fixed at entry, so a slow FOCUS_LOST
  refocus could eat the whole run-end window → exit `NONE`, miscounting a real win/loss.
  **Fix:** extend the deadline by the recovery's wall time.
- **recorder.py (low):** `capture_sync_point` claimed "safe before run()" but passed `_geo=None`
  to `grab_region`. **Fix:** self-`_refresh_geo()` if `_geo is None`.

Trend: **9 → 9 → 3 → 3 → 4 → 4 → 3 → 3 → 2 → 4 → 1 → 3** genuine defects. (Several recent ones are
follow-ons to my own prior fixes — new code gets reviewed too — converging on the margin.)

## Workflow recheck round 12 (wave-limited) — 2 confirmed (both low), fixed + verified

- **strat.py (low):** `ref_frame` was presence-checked but not type-checked → a numeric
  `ref_frame` crashed `resolve_frame` (`os.path.isabs(123)` TypeError) instead of a clean
  problem. **Fix:** new `_req_str` validates string type at all three sites (sync_point,
  expect, detector).
- **recorder.py (low):** `capture_sync_point` before `run()` used `_t0=0`, producing an
  absolute-timestamp sync point that would sort out of order if the recorder was reused.
  **Fix:** lazily seed `_t0` (same epoch as `run()`).

Trend: **9 → 9 → 3 → 3 → 4 → 4 → 3 → 3 → 2 → 4 → 1 → 3 → 2** genuine defects.

## Feature: private-server-link join (Feature A) + record-stamping (2026-06-24)

New `launcher.py` (`open_url`, mockable). `header.private_server_url` + `config.private_server_url`
+ `--private-server` CLI flag + URL validation (`looks_like_roblox_url`). Engine `_join()` opens
the link, waits for Roblox foreground + the expected map (`_await_join`, bounded by
`join_timeout_ms`), then plays; falls back to `join_sequence` when no link. Used at loop start AND
recovery rejoin/relaunch (one join path). Recorder bakes the link into the strat header. 20 new
tests; lint gate now also covers `tests/`. (Record/playback itself was already the core feature.)

## Workflow recheck round 13 (feature code) — 4 raised; 3 fixed, 1 REJECTED (false positive)

- **REJECTED (claimed HIGH again): `grab_region`/`grab_window` "missing retina scaling".**
  Re-verified against the installed mss source a THIRD time: `darwin.grab()` passes the dict
  straight to `CGWindowListCreateImage` with NO scaling, and that API's `CGRect` is in global
  display *points* (`CGDisplayBounds` is points too), returning a physical-pixel image. Passing
  logical `geo.*` is correct; `*retina` would capture a 2x-too-large rect. Added an explanatory
  comment in `capture._grab` so reviewers stop re-raising it. **No change.**
- **engine.py (med):** `_join` ignored `dry_run`, so a dry-run with a private-server link
  opened nothing yet stalled the full `join_timeout_ms` then forced WRONG_MAP recovery.
  **Fix:** skip open/await under `dry_run` (mirrors recovery's guard).
- **input_backend.py (low):** the `clicks>=2` path never tracked the button in `_held_buttons`,
  so a mid-call raise could strand it. **Fix:** track before, discard only after success (both
  click paths) so `release_all()` can always recover.
- **hotkeys.py (low):** `start()` twice without `stop()` leaked the old listener/thread.
  **Fix:** tear the previous one down at the top of `start()` (idempotent).

Trend: **... → 1 → 3 → 2 → 3 (feature)** genuine defects; the retina HIGH is a persistent FP.

## Workflow recheck round 14 (wave-limited) — 2 confirmed (1 medium, 1 low), fixed + verified

- **hotkeys.py (med):** the round-13 double-start guard exposed a TOCTOU — `start()→stop()→
  clear()` could orphan the killswitch daemon (never joined), leaving it able to fire panic.
  **Fix:** `stop()` now joins the watcher (and nulls it) before returning, so a re-arm's
  `clear()` only runs after it's dead.
- **engine.py (low):** `_maybe_pause` left `self.state == PAUSED` after resuming (observers saw
  a stale state). **Fix:** restore the prior state after the pause loop.

Trend: **... → 3 → 2 → 3 → 2** genuine defects; consistently low/medium, several are follow-ons
to my own prior fixes.

## Workflow recheck round 15 (wave-limited) — 3 confirmed (1 high, 2 low), fixed + verified

- **engine.py (high):** the `require_settled` frame-to-frame check dropped the sync `mask`, so a
  masked-out dynamic region (timer/animation) kept "settled" False forever → spurious TIMEOUT.
  **Fix:** pass the same `mask` to the settle comparison.
- **input_backend.py (low):** an unmappable multi-char key still injected its first character.
  **Fix:** `key_to_pynput` returns `None`; `press_key`/`release_key`/`release_all` skip it.
- **recorder.py (low):** a minimized (0-size) window made `dead_zone = 6.0` → every drag misread
  as a click. **Fix:** skip the update on degenerate geometry + clamp to 0.5.

Trend: **... → 2 → 3 → 2 → 3** genuine defects. The `require_settled` path has now had three
follow-on fixes (hang, stability==1, mask) — tricky feature, should be solid now.

## GUI feature (2026-06-24)

Tkinter control panel: Tk-free `GuiController` (dependency-injected, fully unit-tested — every
button's behavior verified without a display) + thin `run_gui` view + `gui` CLI subcommand. 11
controller tests; Xvfb-verified the view builds. Functionality-first per the user's preference.

## Workflow recheck round 16 (feature+full) — 6 raised; 5 fixed, 1 REJECTED

- **REJECTED (low): GUI `start_play` persists consent before validating the start.** This matches
  the CLI's `--accept-ban-risk` (consent is a persistent user acknowledgement written up-front,
  not tied to one successful run). **No change.**
- **engine.py (med):** `_wait_run_end`'s refocus deadline-extension (my round-11 fix) was
  unbounded → persistent focus-flapping could hang the loop. **Fix:** cap total extension at one
  `timeout_ms`.
- **recovery.py (med):** `classify()` derived FOCUS_LOST from LIVE focus even when reclassifying a
  captured `scene` (stuck-sync path) → a stuck sync coinciding with a focus blip was mis-handled.
  **Fix:** `classify(window, *, live=False)` for the reclassify; FOCUS_LOST only when `live`.
- **engine.py (low):** `_route_recovery` hard-set state to IN_MATCH on RESUME even from
  LOBBY/WAIT_RUN_END. **Fix:** restore the prior phase.
- **pngio.py (low):** a truncated chunk header raised `struct.error` not `ValueError`. **Fix:**
  bounds-guard the chunk loop.
- **gui.py (low):** `loop_var.get()` raised `TclError` on a blanked spinbox. **Fix:** guard → 0.

Trend: **... → 3 → 2 → 3 → 5** genuine defects (count up because the GUI + a re-review of all
files added surface; severities still low/medium).

## Workflow recheck round 17 (full) — 4 raised; all 4 confirmed genuine + fixed

- **config.py (low): `_coerce_type` int branch was inconsistent.** A float override `1.9`
  silently truncated to `1` via `int(value)`, while a string `"12.5"` crashed with a raw
  `int('12.5')` ValueError. **Fix:** ints pass through exactly; everything else is parsed via
  `float()` then rejected unless integral, with a clear `"<key> must be an integer"` message.
- **engine.py (med): a configured `run_end` that timed out counted as a completed run.**
  `_wait_run_end` returns `NONE` both when `run_end` is unset (intended completion) *and* when it
  is set but neither victory/defeat/disconnect/wrong-map matched before `timeout_ms` (a stuck
  match). The second case fell through to `runs += 1`, satisfying `loop_count` and resetting the
  restart budget — violating the line-61 invariant "runs = matches actually completed (reached
  run-end)". **Fix:** when `run_end is not None` and the result is `NONE`, route to
  `_route_recovery(STUCK_SYNC)` (→ REJOIN/_RestartLoop, bounded by `max_consecutive_restarts`;
  over-budget → _StopRun) instead of counting it. The `run_end is None` path is unchanged.
- **recorder.py (med): keys still held when recording stopped left an unpaired KeyPressEvent.**
  `run()` stops listeners (nulling the release callback) then calls `build()`; a key physically
  held at stop time had its press recorded but its eventual release fired nothing → on replay the
  engine pressed it and never released it (stuck key). **Fix:** `build()` drains `_pressed_keys`,
  emitting a synthetic release per held key (D14/D15) before `coalescer.finish()`.
- **gui.py (low): a failed consent write dead-ended the Play button.** `set_consent()` swallows
  `OSError`; if the write failed, `consent_ok()` (a file-exists check) returned False and
  `start_play` emitted `consent_required` — asking the user to tick a box they just ticked.
  **Fix:** an explicit `accept_ban_risk=True` this session grants consent in-memory
  (`not (accept_ban_risk or consent_ok())`); the disk write stays best-effort for persistence.

Tests: +8 (`tests/test_bugfixes_recheck15.py`) → **248 pass, ruff + pyflakes clean**.
Trend: **3 → 2 → 3 → 5 → 4** genuine defects. Clean streak still **0/3** (this round found 4).

## Workflow recheck round 18 (full) — 4 raised; all 4 confirmed genuine + fixed

- **capture.py (low): grab rect wasn't clamped to the screen.** A Roblox window dragged
  (partly) off-screen makes the computed mss rect fall outside every monitor → `mss.grab`
  raises `ScreenShotError`, which the run-level catch-all turns into a *premature end of the
  whole farming run* instead of recovery. **Fix:** new pure `_clamp_rect_to_bounds()` intersects
  the rect with `sct.monitors[0]` (≥1×1 result); `_grab` clamps before grabbing, so the
  comparator (which resizes live→ref dims) scores the sliver low → normal sync-timeout/recovery
  takes over. Comparator size-tolerance verified (visual.py:74-76) so clamping can't relocate the
  failure into the compare path.
- **recorder.py (low): `run()` clobbered a pre-run sync point's epoch.** `capture_sync_point()`
  lazily seeds `_t0` under `if not self._t0` (the #w12 shared-epoch design), but `run()`
  unconditionally overwrote `_t0` → a sync point captured before `run()` was timestamped against a
  different origin than later events, inverting `finish()`'s `(t_ms, id)` order. **Fix:** guard
  `run()`'s assignment with `if not self._t0` to honor the shared epoch (reuse is already
  unsupported — the coalescer keeps prior events — so the guard introduces no regression).
- **gui.py / recorder.py (med): Pause did nothing while recording.** `pause_toggle` set
  `hk.events.pause` and the Player honored it, but `Recorder.run` only looped on `is_stop()` and
  its listener callbacks kept feeding the coalescer → the UI claimed a pause while input meant to
  be excluded was still recorded. **Fix:** `Recorder._recording_paused()` gates new intake in
  `_on_press`/`_on_click`(press)/`_on_scroll`/`_on_move`(free moves); releases of already-tracked
  keys/buttons are *never* gated (mirrors the off-focus R27 path) so nothing gets stuck — verified
  by an explicit press-then-release-while-paused pairing test.
- **gui.py (low): `validate()` crashed the Tk callback on a binary file.** `strat.load` opens with
  `encoding="utf-8"`; a non-UTF-8 file raises `UnicodeDecodeError` (a `ValueError`, not caught by
  load's `json.JSONDecodeError` handler nor validate's `OSError`), escaping `validate`'s
  `(ok, problems)` contract into the Tk command callback. **Fix:** `except (OSError, ValueError)`.

Tests: +9 (`tests/test_bugfixes_recheck16.py`) + `macfakes` FakeSct grew a `monitors` attr →
**257 pass, ruff + pyflakes clean**.
Trend: **2 → 3 → 5 → 4 → 4** genuine defects. Clean streak still **0/3** (this round found 4).

## Workflow recheck round 19 (full) — 2 raised; both confirmed genuine + fixed

(One verify agent in this round hit the StructuredOutput retry cap — a transient API failure —
so the round couldn't be clean regardless; both surfaced findings were still real.)

- **engine.py (med): `_await_join`'s deadline ignored pause time.** `deadline = now +
  join_timeout_ms` is fixed before the loop, but `_maybe_pause()` blocks inside it on raw wall
  time (`_absorb_wall_time` only shifts `clock_offset`, not `clock.now_ms()`). A user pause longer
  than `join_timeout_ms` made the loop exit right after resume and return False → spurious
  `WRONG_MAP` recovery counting against `max_consecutive_restarts`. The sibling `_wait_run_end`
  already extends its deadline for blocked time. **Fix:** measure the paused span and push
  `deadline` forward by it, capped at one `join_timeout_ms` (mirrors `_wait_run_end`).
- **recorder.py (low): `if not self._t0` treated a legitimate 0.0 epoch as 'unset'.** A flaw in
  the round-18 fix: `_t0` was initialised to `0.0` and both guards used truthiness, so a clock
  reading exactly 0.0 (FakeClock, or RealClock just after start) looked unset — `run()` re-seeded
  `_t0` to the advanced time, shifting the origin (the very corruption #w12 guards against). **Fix:**
  `_t0` initialises to `None`; both guards use `is None`; `_now_ms()` treats `None` as origin 0.
  (Caught by the reviewer at the FakeClock boundary the round-18 test missed — verifies the value
  of re-reviewing one's own fixes.)

Tests: +3 (`tests/test_bugfixes_recheck17.py`), 1 round-18 assertion updated → **260 pass,
ruff + pyflakes clean**.
Trend: **3 → 5 → 4 → 4 → 2** genuine defects. Clean streak still **0/3** (this round found 2).

## Workflow recheck round 20 (full) — 2 raised; both confirmed genuine + fixed

(First attempt was an infrastructure wash: `complete:false`, slices `cli+init`/`gui` never ran,
5 findings unverified, a cascade of ECONNRESET over ~8.8h. Resumed from the same runId once the
API recovered; cached slices returned instantly and only the failed slices/verifies re-ran.)

- **config.py (low, latent): a nullable field couldn't be cleared back to None.** `_coerce_type`
  keyed None-acceptance off the *current* value (`current is None`) rather than the field's
  declared nullability, so `Config(retina_scale_override=2.0).with_overrides({...: None})` raised
  `ValueError`. Latent today (both callers start from a fresh `Config()` where current is None),
  but wrong `with_overrides` semantics and a trap for any future layered override. **Fix:** module
  `_NULLABLE_FIELDS = {retina_scale_override, window_rect_override}`; the None branch returns None
  for those keys and rejects None for every other field.
- **strat.py (med): a hand-edited `threshold` wasn't bounded to [0,1].** `_num` checks
  type/finiteness/wholeness but not range; `score()` is clamped to [0,1] and the engine matches
  `score >= threshold`, so a threshold of e.g. 50 can *never* match (every sync_point times out →
  spurious recover/abort on a perfectly good run) and a negative one *always* matches (syncs fire
  on the wrong screen). Real for the human-editable strat files. **Fix:** new `_threshold()`
  helper range-checks after `_num`; applied at all three parse sites (sync_point, `expect`,
  detector).

Tests: +11 (`tests/test_bugfixes_recheck18.py`) → **271 pass, ruff + pyflakes clean**.
Trend: **5 → 4 → 4 → 2 → 2** genuine defects. Clean streak still **0/3** (this round found 2).

## Workflow recheck round 21 (full) — 4 raised; 3 genuine + fixed, 1 REJECTED

- **REJECTED (med): `_play_sequence` "leaks an event if you panic while paused".** False positive.
  `_maybe_pause` exits on `should_abort`, but the very next line is `sleep_until(target)`, and
  `RealClock.sleep_until` calls `_check()` at entry (clock.py:43) which raises `PanicAbort` before
  `_dispatch_primitive`. Every production `RealClock` is built `should_abort=hk.should_abort`
  (cli.py:205,260; gui.py:52,65) — the same predicate `_maybe_pause` checks — so a panic while
  paused always aborts at `sleep_until`. The finding missed the entry `_check()`. **No change.**
- **engine.py (low): per-event jitter could invert/collapse spacing.** Signed jitter in
  `[-jitter_ms, +jitter_ms]` was added to each absolute target independently, so a negative draw on
  event N+1 after a positive draw on N could make `target(N+1) < target(N)` → `sleep_until` returns
  instantly → the two fire back-to-back, defeating humanization. **Fix:** clamp each target to
  `>= prev_target` (monotonic non-decreasing); jitter_ms=0 is unchanged.
- **config.py (med): `looks_like_roblox_url` used a substring host test.** `"roblox.com" in u`
  accepted `roblox.com.evil.example`, `evilroblox.com`, `?ref=roblox.com`, etc.; the validated URL
  then flows to `launcher.open_url` → `subprocess.run(["open", url])`, letting a typo'd/hostile link
  steer the join (URL-steering, not RCE — list-form open). **Fix:** parse with `urlparse` and accept
  only host == `roblox.com`/`ro.blox.com` or a `.roblox.com`/`.ro.blox.com` subdomain.
- **hotkeys.py (low): a listener failure disabled panic silently.** The broad `except` around
  `GlobalHotKeys(...).start()` set `_listener=None` with no log, so a real failure (permissions, OS,
  a combo GlobalHotKeys rejects) silently killed the safety-critical panic key. **Fix:** split
  `except ImportError` (no pynput — tests/non-mac, stay quiet) from `except Exception` (pynput
  present but listener failed → `log.warning(... DISABLED ...)`).

Tests: +19 (`tests/test_bugfixes_recheck19.py`) → **290 pass, ruff + pyflakes clean**.
Trend: **4 → 4 → 2 → 2 → 3** genuine defects. Clean streak still **0/3** (this round found 3).

## Note: workflow API failures (investigated 2026-06-24)
Symptoms: `Connection closed mid-response`, `Stream idle timeout`, `StructuredOutput
retry cap (5) exceeded`. Root cause: transient mid-stream drops on long agent
responses, **amplified by concurrency** — firing ~14 review agents at once pressures
the API rate limit, throttling/stalling streams (one round's two dropped verify agents'
retries took ~2.5h each → a 2.7h round). Mitigation: run slices in **waves of ≤4**
(`inWaves` helper in the v4 workflow) + terser outputs + per-slice retries, so fewer
concurrent streams and any drop is recovered.

## Harness note (not a product bug)
`pkill -f "Xvfb :99"` matched the running shell itself (its command line contained
that literal), killing the shell → exit 144 with no output. Use `pkill -x Xvfb`
(exact process name) or kill by PID. Recorded so it isn't re-hit.
