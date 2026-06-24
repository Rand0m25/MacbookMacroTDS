# tds_macro

A **resolution-independent, visually self-correcting** macro for the Roblox game
**Tower Defense Simulator**, written in Python 3 for **macOS**.

It is an interpretation/replication of the concept in the YouTube video
*"I Change How TDS Macros work forever…"* — built around the two things that make
a TDS macro actually survive real play:

1. **Visual-sync adaptive timing** — instead of firing on a fixed `sleep()`, the
   macro watches a small region of the screen and fires each action when the game
   has *actually* reached the expected state, **stretching its own timing when the
   game lags** so it never desyncs.
2. **Error recovery** — it detects a **wrong map** or a **disconnect** and
   automatically **leaves / resets your character and rejoins**, so an unattended
   farm loop heals itself.

Plus the table stakes: **window-relative coordinates** (one strat works at any
window size / Retina scale), a **built-in recorder**, **human-editable JSON
strats**, and **auto-loop farming** with a global panic hotkey.

> ⚠️ **Ban risk.** Automating Roblox/TDS violates the Roblox Terms of Use and can
> get your account banned. This tool uses only screen capture + OS-level input
> (no memory injection), which is lower-risk than exploits but **still a ToU
> violation — nothing makes it safe.** You must pass `--accept-ban-risk` once to
> use `play`. Use at your own risk.

---

## Install (on the MacBook)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

**Grant two macOS permissions** to the app that launches Python (Terminal / iTerm
/ VS Code) — *not* "python" itself — then fully quit & relaunch it:

- **System Settings → Privacy & Security → Accessibility** (for input control)
- **System Settings → Privacy & Security → Screen Recording** (for capture)

Check them:

```bash
python -m tds_macro check-perms
python -m tds_macro smoke      # finds the Roblox window, captures a frame, etc.
```

## Quickstart

```bash
# 1. Record yourself playing (F10 drops a sync point, F7/F8 stops)
python -m tds_macro record mystrat.strat.json --map "Polluted Wastelands II" --difficulty Fallen

# 2. Sanity-check the file (frame paths + dimensions + schema)
python -m tds_macro validate mystrat.strat.json

# 3. Dry-run the visual gates against the live screen (no input sent)
python -m tds_macro calibrate mystrat.strat.json

# 4. Play it / auto-loop farm it
python -m tds_macro play mystrat.strat.json --loop-count 0 --accept-ban-risk
```

There's a ready example to inspect:

```bash
python examples/make_example.py      # generates examples/pw2_fallen/
python -m tds_macro validate examples/pw2_fallen/pw2_fallen.strat.json
```

## Hotkeys (configurable)

| Key | Action |
|-----|--------|
| `F9` | start / resume |
| `F7` | pause / resume toggle |
| `F8` | **panic** — instantly stop and release all held inputs |
| `F10` | (while recording) drop a sync-point reference frame |

## The strat file

Human-editable JSON. All positions are normalized `{x,y}` in `0..1` of the Roblox
window, so the same file works at any size/resolution.

- `events` — the in-match timeline: `place_tower`, `upgrade`, `ability` (TDS
  macros) plus `mouse_move`/`click`/`drag`/`key_press`/`key_release`/`scroll`/`wait`
  and `sync_point` (the visual barrier).
- `join_sequence` — the **TDS-lobby** rejoin path (Play → map → ready); replayed
  for both loop restarts and recovery.
- `leave_reset_sequence` — the **Roblox-client** leave/reset path (Esc → Leave).
- `run_end` — victory/defeat reference frames so the loop knows when a match ended.
- `expected_map_check` — a positive "the right map is loaded" check (else recover).
- `recovery.wrong_map` / `recovery.disconnect` — detectors that trigger recovery.

A `sync_point`:

```json
{"type":"sync_point","label":"wave5","ref_frame":"frames/wave5.png",
 "region":{"x":0.43,"y":0,"w":0.14,"h":0.07},"threshold":0.88,
 "timeout_ms":30000,"on_timeout":"recover","match":"tm_ccoeff_normed",
 "mask":[{"x":0.5,"y":0.2,"w":0.4,"h":0.6}]}
```

The engine polls `region`, scores it against `ref_frame`, and fires when
`score ≥ threshold`. On match it **rebases the clock (monotonic / stretch-only)**
so every later action slides by the same lag. On timeout it applies `on_timeout`
(`abort` / `continue` / `retry` / `recover`).

## Recovery, in two layers

| Layer | What | Why |
|-------|------|-----|
| **Roblox client** | leave / reset character / disconnect modal | Stable across TDS updates → built-in backbone (`leave_reset_sequence`) |
| **TDS game** | rejoin: Play → map → ready | Changes with TDS updates → you record it (`join_sequence`) |

All failures funnel through one bounded FSM:
`CLASSIFY → (REFOCUS / RECONNECT / RESET → LEAVE → LOBBY) → REJOIN`, with a
per-cause attempt cap so a wrong-map loop can't churn forever.

## Tips for reliable strats

- Record and replay at the **same window size** and Roblox **GUI scale**; UI
  buttons reflow with size (world-space placements are size-independent, button
  taps are not).
- Put sync regions on **static UI chrome**, and `mask` out volatile bits
  (cash counter, wave timer, particles).
- Reset the camera to a known view as your first recorded action.

## Development / tests

The core is pure stdlib + dataclasses; all OS/platform access sits behind mockable
interfaces, so the logic is fully unit-tested on Linux with no display or Roblox:

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q          # 54 tests
python -c "import tds_macro"        # imports cleanly even without numpy
```

See `docs/PLAN.md` for the full design, the 30-item risk register, and the
triple-check fixes (sections 3, 7, 8).

## Limitations

- macOS only for real use (Linux/Windows run the mock backends only).
- No OCR in v1 (out-of-cash is inferred from "the action didn't take", not by
  reading the number).
- Reference frames are tied to your graphics settings / TDS UI version — re-record
  them after a TDS update (`calibrate` tells you which gates went stale).
