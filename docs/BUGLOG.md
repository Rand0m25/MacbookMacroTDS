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

## Harness note (not a product bug)
`pkill -f "Xvfb :99"` matched the running shell itself (its command line contained
that literal), killing the shell → exit 144 with no output. Use `pkill -x Xvfb`
(exact process name) or kill by PID. Recorded so it isn't re-hit.
