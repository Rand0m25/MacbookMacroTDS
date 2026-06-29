# tds_macro

A **resolution-independent, visually self-correcting** macro for the Roblox game
**Tower Defense Simulator**, written in Python 3 for **macOS**.

You record yourself playing once; it replays your actions and—unlike a fixed-`sleep`
macro—**watches the screen and waits for the game to actually reach each step**, so it
doesn't desync when the game lags. It also **heals itself**: on a wrong map or
disconnect it leaves, resets, and rejoins, so an unattended farm loop keeps going.

> ⚠️ **Ban risk.** Automating Roblox/TDS violates the Roblox Terms of Use and can get
> your account banned. This tool uses only screen capture + OS-level input (no memory
> injection) — lower-risk than exploits, but **still a ToU violation. Nothing makes it
> safe.** You opt in once with `--accept-ban-risk` (CLI) or the *Accept ban risk*
> checkbox (GUI). Use at your own risk.

---

## 1. Setup (do this once)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

**Grant two macOS permissions** to the app you launch Python *from* (Terminal / iTerm /
VS Code) — not to "python" itself — then fully **quit and reopen** that app:

1. **System Settings → Privacy & Security → Accessibility**  (lets it move the mouse / press keys)
2. **System Settings → Privacy & Security → Screen Recording**  (lets it see the game)

Check everything is ready:

```bash
python -m tds_macro check-perms     # confirms both permissions
python -m tds_macro smoke           # finds the Roblox window + grabs a test frame
```

---

## 2. Easiest way to use it — the app window

```bash
python -m tds_macro gui
```

A small window opens with everything in one place:

| Control | What it does |
|---|---|
| **Strat file** + **Browse…** | pick an existing `.strat.json` to play / validate |
| **New…** | name a brand-new `.strat.json` and create it (blank), ready to record into |
| **Validate** | check a strat file is well-formed before playing |
| **Name / Map / Difficulty** | labels saved into a new recording |
| **Private server link** | optional — always (re)join this exact server (see §4) |
| **Loop count (0 = ∞)** | how many matches to farm; 0 means forever |
| **Dry run** | go through the motions without sending any input (safe preview) |
| **Accept ban risk** | required tick before **Play** will run |
| **Record into** | which sequence **Record** captures: *Main timeline* (the in-match `events`), *Join sequence* (lobby Play→map→ready), or *Leave/reset sequence* (Roblox Esc→Leave). The latter two **merge** into the chosen strat without touching its other parts. |
| **Record** | start capturing your play; press **Stop** (or `F8`) to finish + save |
| **Play** | replay the strat with visual-sync timing, recovery, and auto-loop |
| **Pause/Resume** · **Stop / Panic** | pause excludes input from a recording; Stop releases all held keys immediately |
| **Settings…** | a separate window to edit hotkeys, humanization/timing, sync-localization, and recovery/sync/safety knobs. Saved to `~/.tds_macro_settings.json` and applied on every launch (Reset-to-defaults included); edit while idle. A strat's own `config_overrides` still win over these. |

The status line shows live `runs / wins / losses / recoveries`.

---

## 3. The command line (same features, scriptable)

```bash
# Create a fresh, empty strat file to record into / hand-edit (--force overwrites).
python -m tds_macro new mystrat.strat.json --map "Polluted Wastelands II" --difficulty Fallen

# Record yourself playing. While recording: F10 = drop a sync point, F8 = stop.
python -m tds_macro record mystrat.strat.json --map "Polluted Wastelands II" --difficulty Fallen

# Check the file (frame paths + dimensions + schema).
python -m tds_macro validate mystrat.strat.json

# Dry-run the visual checks against the live screen (no input is sent).
python -m tds_macro calibrate mystrat.strat.json

# Play it / farm it forever (--loop-count 0). The ban-risk opt-in is required once.
python -m tds_macro play mystrat.strat.json --loop-count 0 --accept-ban-risk
```

`record` captures your live mouse/keyboard/timing into a JSON file; `play` replays it.
You can **edit the JSON by hand** any time — it's plain data, not code.

There's a ready-made example to inspect:

```bash
python examples/make_example.py        # generates examples/pw2_fallen/
python -m tds_macro validate examples/pw2_fallen/pw2_fallen.strat.json
```

---

## 4. Always join one private server (optional)

Give the macro a private-server link and it joins by **opening the link** instead of
clicking through the lobby:

```bash
python -m tds_macro record mystrat.strat.json --map "PW2" \
    --private-server "https://www.roblox.com/games/123/TDS?privateServerLinkCode=XXXX"

# (or pass --private-server to `play` to override whatever the file has)
```

It opens the link, waits for Roblox to come to the front and the expected map to load,
then plays. If your browser asks "Open Roblox?", turn on its *always allow* option once so
it launches automatically. If the server is full/expired and never loads, recovery retries
are bounded and then it stops cleanly.

The link is **opened once per session, not every loop** — you stay in the same server across
matches, so between runs it only replays your recorded lobby `join_sequence` (the "play
again" clicks) rather than reloading the whole server. Only a real **disconnect** makes
recovery re-open the link to get you back into that same server.

**Cold start.** If Roblox isn't even running when you hit **Play**, the macro opens the link
to **launch** it and waits up to `launch_timeout_ms` (default 60s) for the window to appear,
then plays — so you don't have to open the game yourself first. (Without a link configured, a
missing Roblox window still fails fast with "could not start".)

---

## Hotkeys (configurable)

| Key | Action |
|-----|--------|
| `F9` | start / resume |
| `F7` | pause / resume toggle |
| `F8` | **panic** — instantly stop and release every held input |
| `F10` | (while recording) drop a sync-point reference frame |

---

## Reference

### The strat file
Human-editable JSON. All positions are normalized `{x,y}` in `0..1` of the Roblox window,
so the same file works at any size / Retina scale.

- `events` — the in-match timeline: `place_tower`, `upgrade`, `ability` plus
  `mouse_move` / `click` / `drag` / `key_press` / `key_release` / `scroll` / `wait`, and
  `sync_point` (the visual barrier).
- `header.private_server_url` — a Roblox private-server link; when set the macro joins by
  opening it (preferred over `join_sequence`).
- `join_sequence` — the **TDS-lobby** rejoin path (Play → map → ready); replayed for loop
  restarts and recovery. With a link set, use it only for any post-load clicks.
- `leave_reset_sequence` — the **Roblox-client** leave/reset path (Esc → Leave). Replayed on a
  fixed timer, so it must contain **no** `sync_point`s.

> Record these straight from the GUI: set **Record into → Join sequence** (or **Leave/reset
> sequence**), then **Record**. The capture is merged into that field of the current strat without
> disturbing the rest. (Sync points are auto-stripped from a leave recording.)
- `run_end` — victory/defeat reference frames so the loop knows a match ended.
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
`score ≥ threshold` (threshold must be in `0..1`). On match it **rebases the clock
(monotonic / stretch-only)** so every later action slides by the same lag. On timeout it
applies `on_timeout` (`abort` / `continue` / `retry` / `recover`).

### Recovery, in two layers

| Layer | What | Why |
|-------|------|-----|
| **Roblox client** | leave / reset character / disconnect modal | Stable across TDS updates → built-in (`leave_reset_sequence`) |
| **TDS game** | rejoin: Play → map → ready | Changes with TDS updates → you record it (`join_sequence`) |

All failures funnel through one bounded state machine —
`CLASSIFY → (REFOCUS / RECONNECT / RESET → LEAVE → LOBBY) → REJOIN` — with a per-cause
attempt cap so a wrong-map loop can't churn forever.

**Foreground guard.** Before *every* input it sends (click / keypress / drag / scroll /
move), the macro verifies the **Roblox window is frontmost**, so an action can never land in
another app if you alt-tab away. If focus is lost it refocuses Roblox first (bounded, then
stops); it never fires blind. Opt out with `config_overrides: {"verify_foreground": false}`.

**Match the recorded window size.** Tower placement is a click at a screen position, so if the Roblox
window is a different *aspect* at replay than when you recorded, those clicks land on the wrong tile.
Before playback the macro **resizes the Roblox window back to the size the strat was recorded at**
whenever the live aspect differs (toggle: `match_window_size_on_play`, default on; it leaves the window
alone when the aspect already matches, since a same-aspect size change is handled by the normalized
coordinates). Best-effort via System Events (needs the Accessibility permission you already grant); if
it can't resize, it falls back to the aspect-mismatch warning.

**Cursor placement.** Before playback starts, the macro moves the cursor into the **middle of the
Roblox window** so the first action begins from inside the game (toggle: `center_cursor_on_play`,
default on). And during a visual-sync wait the cursor is now **left where it is** instead of being
parked in a corner — so checking a sync no longer yanks the pointer to the top-right of the screen
(toggle: `sync_park_cursor`, default off; turn it on only if a sync region sits under where the
cursor naturally rests). Both are also editable in **Settings…**.

### Tips for reliable strats
- Record and replay at the **same window size** and Roblox **GUI scale** — UI buttons
  reflow with size (world-space tower placements are size-independent; button taps aren't).
- Put sync regions on **static UI chrome**, and `mask` out volatile bits (cash counter,
  wave timer, particles).
- Reset the camera to a known view as your first recorded action.

### Limitations
- macOS only for real use (Linux/Windows run the mock backends only).
- No OCR — "out of cash" is inferred from "the action didn't take", not by reading the number.
- Reference frames are tied to your graphics settings / TDS UI version — re-record them
  after a TDS update (`calibrate` tells you which checks went stale).

---

## Development / tests

The core is pure stdlib + dataclasses; all OS/platform access sits behind mockable
interfaces, so the logic is fully unit-tested on Linux with no display or Roblox:

```bash
pip install -r requirements-dev.txt
python -m pytest -q          # full suite (250+ tests), runs anywhere
ruff check tds_macro/ tests/ # lint (also enforced by the suite)
python -c "import tds_macro" # imports cleanly even without numpy
```

See `docs/PLAN.md` for the full design and risk register, and `docs/BUGLOG.md` for the
review-and-fix history.
