# TDS Macro — Plan & Code Outline

A resolution-independent, recorder-based, **visually self-correcting** macro for Roblox
**Tower Defense Simulator**, written in Python 3, targeting **macOS** (Apple Silicon/Intel),
developed and unit-tested on Linux via mock backends.

> Replicates the *concept* of the YouTube video "I Change How TDS Macros work forever…"
> (bPzZ6-VeVJg). The video could not be watched directly, so this is the canonical,
> feature-complete interpretation, extended with the two features the user specifically
> requested: **error/disconnect recovery** and **visual-sync adaptive timing** (it watches
> the screen and stretches its own timing when the game lags, instead of firing on a fixed clock).

---

## 0. What makes this different from every "press 5, click, sleep 500ms" TDS macro

| Typical TDS macro | This one |
|---|---|
| Hardcoded screen pixels | Coords stored as **0–1 fractions of the Roblox window** → works at any size/resolution/Retina |
| Fixed `sleep()` timing | **Visual-sync barriers**: waits for the expected screen state, stretching timing under lag |
| Breaks silently on lag/disconnect | **Recovery FSM**: detects wrong-map / disconnect / stuck → leaves + resets character → rejoins |
| One-off hand-written coords | **Built-in recorder** captures your play into a human-editable JSON "strat" |
| Runs once | **Auto-loop farming** with a global panic hotkey |

---

## 1. Verified tech stack (macOS, 2026)

| Concern | Library | Notes |
|---|---|---|
| Mouse/keyboard **control + recording** | `pynput` | one lib for both; backend = Quartz CGEvent (logical points). Silently no-ops without Accessibility. |
| Fast **screen capture** | `mss` | reuse ONE `mss.mss()` instance; returns **physical** pixels (2× on Retina). |
| **Window bounds** | `pyobjc-framework-Quartz` | `CGWindowListCopyWindowInfo`; match `kCGWindowOwnerName == "Roblox"`, not the title. |
| **Image compare** | `numpy` (+ optional `opencv-python`) | template match / NCC / MSE / pHash for visual-sync. |
| **Accessibility check** | `pyobjc-framework-ApplicationServices` | `AXIsProcessTrusted()` at startup. |

Install: `pip install pynput mss numpy opencv-python Pillow pyobjc-framework-Quartz pyobjc-framework-ApplicationServices`

### THE #1 invariant (gets every naive macro wrong)
`pynput`/Quartz speak **logical points**; `mss` returns **physical pixels** (2× Retina).
**Everything stored is a normalized 0–1 fraction of the Roblox content box.** Only the capture
layer and the `Coordinates` service know about logical-vs-physical/Retina. Reference frames and
live frames are both captured through the same `mss` path so the 2× factor cancels in comparison.

```
A) normalized → logical (for clicks):   px = Wx + nx*Ww ;  py = Wy + ny*Wh
B) logical → normalized (recorder):     nx = (px-Wx)/Ww ; ny = (py-Wy)/Wh
C) normalized → physical, MONITOR-RELATIVE (index a window image grabbed from monitor M):
      px_phys = ((Wx - Mx) + nx*Ww) * retina ;  py_phys = ((Wy - My) + ny*Wh) * retina
D) physical → normalized (inverse of C):
      nx = ((px_phys/retina) - (Wx - Mx)) / Ww ;  ny = ((py_phys/retina) - (Wy - My)) / Wh
E) region crop in a window image of size (Iw,Ih): crop = img[round(ry*Ih):round((ry+rh)*Ih),
                                                            round(rx*Iw):round((rx+rw)*Iw)]
   # E indexes a window-cropped image in normalized space → retina cancels WITHIN one session only.
   # Cross-session (record 2x / replay 1x): the Comparator RESIZES crops to a canonical size — see §8.
```
> y-origin is **top-left, global logical points** (CGWindow convention) end-to-end. `(Mx,My)` =
> top-left of the monitor `mss` grabbed (can be negative on multi-display). All Retina lives only
> in the capture/`Coordinates` layer. The §0 "works at any size/resolution" claim is **world-space**
> placements; UI-button taps need matching window size + Roblox GUI scale (see §8 / fixed-window recipe).

---

## 2. Architecture (layered, dependency-injected, mockable)

```
CLI (argparse)            record | play | validate | calibrate | check-perms
   │
Application       Recorder │ Player/Engine(RunLoop FSM) │ RecoveryController(sub-FSM)
   │
Services          Comparator │ Coordinates │ HotkeyManager │ Clock
   │
Value types       Config │ geometry │ Strat model │ Frame                 (zero OS deps)
   │
Platform PORTS    WindowProvider │ InputBackend │ CaptureBackend
                    macOS impl (Quartz/pynput/mss) · Mock impl (fixtures)
```

**Concurrency** — three threads, one-way message passing via `threading.Event`s:
1. **Hotkey listener** — does *nothing* but set atomic Events (panic/stop/pause/start/mark-sync) so it can never be starved.
2. **Engine** — checks the panic/stop Event between every atomic action and inside every sleep/poll.
3. *(optional)* **Capture thread** — owns its own `mss` instance (mss is not thread-safe).

`InputBackend` tracks every held button/key and exposes `release_all()`, wired to
`atexit` + `finally` + signal handlers so panic never leaves a key stuck.

---

## 3. Phased plan + possible errors per phase

> "Possible errors" reference the 30-item risk register (R01–R30) at the end of this doc.

### Phase 0 — Platform ports, permissions, hotkeys *(the foundation everything mocks against)*
- **Build:** `WindowProvider` / `InputBackend` / `CaptureBackend` Protocols + Mock impls; `Clock` (Real + Fake); `HotkeyManager`; `permissions.py` self-checks; factories that hide platform imports.
- **Possible errors:**
  - **R01** Accessibility not granted → pynput silently no-ops. *Mitigate:* `AXIsProcessTrusted()` startup gate; print exact host-app (Terminal/iTerm/VSCode) path + steps; refuse to run.
  - **R02** Screen Recording not granted → black captures, no exception. *Mitigate:* grab one frame, assert not uniformly black.
  - **R04** Linux dev box has no Quartz/Roblox. *Mitigate:* all platform imports behind factory funcs; `import tds_macro` must succeed on Linux.
  - **R21** Global panic hotkey starved/blocked. *Mitigate:* dedicated listener thread that only sets an Event; configurable non-colliding chord; kill-switch-file fallback.

### Phase 1 — Resolution-independent coordinate system
- **Build:** `geometry.py` (A/B/C/D/E conversions, `Point`/`Rect`/`WindowGeometry`, round-trip invariants); `QuartzWindowProvider` (real bounds + Retina scale); aspect-mismatch warning.
- **Possible errors:**
  - **R03** Retina 2× makes clicks land at half/double position. *Mitigate:* probe scale once, store in strat metadata, normalize everything; test that a marker maps to the same logical coord at 1× and 2×.
  - **R05** Window moved/resized/other monitor; lost focus → clicks hit desktop. *Mitigate:* re-resolve window rect every iteration; raise/focus + verify frontmost before each burst.
  - **R06** Roblox UI reflows with size/aspect/GUI-scale (not pure scaling). *Mitigate:* recommend a fixed record/replay window size; store it; warn on mismatch; prefer visual anchors for critical buttons.

### Phase 2 — Strat schema + recorder
- **Build:** `strat.py` (dataclass models, discriminated `Event` union, load/save **atomic**, validation, `schema_version`, migration shims, `expand_macro`); `recorder.py` (listeners → normalized coalesced events; sync-point capture w/ threshold auto-suggest).
- **Possible errors:**
  - **R24** Hand-edited JSON: syntax/type errors, out-of-range coords, missing frames. *Mitigate:* validate at load with field-level messages; check coord ranges + frame existence; **fail fast, never mid-loop**; `validate` subcommand.
  - **R25** Schema drift across versions. *Mitigate:* `schema_version` + migration shims; refuse newer-than-supported.
  - **R26** Bulky/coupled frame assets, broken paths, partial writes. *Mitigate:* `frames/<strat>/<label>.png` convention; validate dims; **atomic temp+rename** writes.
  - **R27** Recorder captures wrong reference frame (window moved / alt-tab). *Mitigate:* track rect continuously; convert at capture time; skip/tag events while Roblox not frontmost.
  - **R20** Listener vs recorder thread race. *Mitigate:* single stop `Event`, locks/queues for shared state.

### Phase 3 — Player / replay engine (fixed-clock first)
- **Build:** `engine.py` Player: ordered event dispatch, mouse interpolation, macro expansion, cooperative panic-aware sleeps, `release_all` guarantees. (Visual-sync added in Phase 4.)
- **Possible errors:**
  - **R07** Camera angle/zoom not captured → placements land wrong. *Mitigate:* recommend a camera-recenter as first recorded action; optional `restart_sequence`; validate post-reset frame.
  - **R10** Inputs too fast → dropped/flagged; replaying idle wastes time. *Mitigate:* min inter-event delay + jitter; compress long idle gaps.
  - **R22** Panic leaves a held key/button. *Mitigate:* track held inputs; `release_all()` on stop/exception/exit.

### Phase 4 — Visual-sync adaptive timing *(the headline feature)*
- **Build:** `visual.py` Comparator (tiered pHash scene gate → per-region SSIM/template/NCC, masking dynamic sub-rects); engine `_adaptive_wait`: poll live region, fire on match (+`stability_frames`, motion-settled gate), **rebase the clock** so all later events stretch with the lag; timeout → recovery.
- **Possible errors:**
  - **R08** Fixed-clock drift under lag → cascade desync. *Mitigate:* this whole phase — gate on screen state, not elapsed time.
  - **R09** Wait hangs forever (frame never appears). *Mitigate:* hard timeout + retry budget on *every* wait; timeout → recovery decision tree. No unbounded waits.
  - **R11** False negative from dynamic content (cash/timer/particles). *Mitigate:* small static ROIs; mask dynamic sub-rects; tuned threshold; `stability_frames`.
  - **R12** False positive (similar screens). *Mitigate:* require multiple ROIs; negative checks; log every score.
  - **R13** References don't generalize (graphics/theme/version). *Mitigate:* store capture metadata; re-capture command; illumination-tolerant metrics; treat frames as regenerable.
  - **R23** Capture too slow → stale decisions/starvation. *Mitigate:* sub-rect grabs, capped rate, downscale, buffer reuse.
  - **R30** Mock tests give false confidence. *Mitigate:* seed corpus with real + noisy/lagged frames; calibrate thresholds on the Mac, not in tests.

### Phase 5 — Error recovery + auto-loop
- **Build:** `recovery.py` sub-FSM: `CLASSIFY → REFOCUS | RECONNECTING | (RESETTING →) LEAVING → LOBBY → REJOINING`; failure detectors (wrong-map, disconnect, kicked, defeat, stuck, out-of-cash, state-mismatch, focus-lost); bounded retries + backoff; the **user's "wrong map / disconnect → leave/reset character"** rule lives here.
- **TWO-LAYER design (leave/reset = Roblox client level; rejoin = TDS game level):**
  - **Leave / Reset Character / disconnect** use **Roblox engine** UI (`Esc` menu → Leave/Reset; native disconnect modal Reconnect/Leave). Game-agnostic and stable across TDS updates → ships as built-in default anchors/steps (in an editable `anchors`/config block, not hardcoded pixels).
  - **Rejoin into a fresh match** uses **TDS lobby** UI (Play → map select → difficulty/mode → ready/vote). Game-specific and unseen → driven by the **user-recorded `restart_sequence`** + reference frames in the strat, never code-hardcoded.
  - The Roblox-level leave drops the avatar into the **TDS hub**, then the TDS-level `_rejoin(expected_map)` takes over. If a hard disconnect dumps to the Roblox app/website instead of the hub, a Roblox-level **re-launch-experience fallback** runs first.
  - Both the between-matches loop restart AND post-error recovery call the **same** `_rejoin(expected_map)` → one TDS lobby sequence to maintain.
- **Possible errors:**
  - **R14** Wrong-map not detected → runs a strat for the wrong map forever. *Mitigate:* verify map before each run; "can't confirm map" = hard stop.
  - **R15** Recovery loops on itself. *Mitigate:* bounded attempts + exponential backoff; global failure counter → halt+notify; "no state change for T" watchdog.
  - **R16** Disconnect/crash → clicks leak into desktop/other app. *Mitigate:* verify Roblox frontmost before every burst; on window loss, suspend input + enter recovery.
  - **R17** Reset/leave menu path changed by Roblox update. *Mitigate:* visually-confirmed step-by-step leave/reset; keep flow in config so it's patchable without code.
  - **R29** Loss/reward popups/full-server/anti-AFK. *Mitigate:* between-runs "dismiss unknown modal" scan; explicit win/loss branch; periodic anti-AFK nudge.

### Phase 6 — Auto-loop farming + humanization + ToS gate
- **Build:** loop orchestration, session caps + randomized breaks, click micro-offset jitter, **first-run ban-risk acknowledgement** (consent file).
- **Possible errors:**
  - **R18** Automation violates Roblox ToU; 24/7 regular patterns are detectable → ban. *Mitigate:* explicit consent; screen+OS-input only (no memory injection); jitter + caps + breaks; document that nothing makes it safe. **Not fully mitigable.**
  - **R19** Byfron/Hyperion may block synthetic input/capture or flag the process. *Mitigate:* standard OS APIs only; verify each action registered; stop+alert if inputs swallowed; minimal footprint.

### Phase 7 — Calibration tooling + docs
- **Build:** `calibrate`/`dry-run` mode (visual gates only, no input; per-gate score report); `validate`; README (permissions, fixed-window recipe, ban warning); golden-frame regression tests.
- **Possible errors:**
  - **R28** Calibration mistakes (size mismatch, ROI over dynamic content, blind thresholds). *Mitigate:* guided dry-run reports which gates matched + scores; warn on size/aspect/GUI-scale mismatch; document recommended setup.

---

## 4. Code outline (module tree + key signatures)

```
tds_macro/
├── tds_macro/
│   ├── __init__.py          # re-exports only; MUST import cleanly on Linux
│   ├── config.py            # Config dataclass + BackendKind/MatchMethod enums
│   ├── geometry.py          # Point, Rect, WindowGeometry, Coordinates (A/B/C/D/E)
│   ├── frame.py             # Frame wrapper (numpy-optional), to_gray/downscale
│   ├── window.py            # WindowProvider Protocol; Quartz + Mock; factory
│   ├── capture.py           # CaptureBackend Protocol; Mss + Mock; factory
│   ├── input_backend.py     # InputBackend Protocol; Pynput + Mock; release_all
│   ├── visual.py            # Comparator Protocol; Cv/Numpy + Mock; SceneClass
│   ├── strat.py             # StratFile/Header/Event union; load/save/validate/migrate
│   ├── recorder.py          # Recorder + EventCoalescer
│   ├── engine.py            # Player + RunState FSM + _adaptive_wait (visual-sync)
│   ├── recovery.py          # RecoveryController sub-FSM + detectors
│   ├── hotkeys.py           # HotkeyManager (dedicated listener thread)
│   ├── clock.py             # Clock Protocol; RealClock + FakeClock
│   ├── permissions.py       # macOS Accessibility/Screen-Recording checks
│   └── cli.py               # argparse entrypoint + subcommand handlers
├── tests/                   # pytest, runs on Linux with mocks (no display/Roblox)
│   ├── test_geometry.py     # round-trip A∘B, C∘D, region crop
│   ├── test_strat.py        # load/save/validate/migrate; bad-file rejection
│   ├── test_recorder.py     # coalescing moves/clicks/drags; normalization; frontmost skip
│   ├── test_engine.py       # timeline order, clock rebasing under lag, panic halts ≤1 action
│   ├── test_adaptive_sync.py# fire-on-match, stability_frames, timeout→recovery
│   ├── test_recovery.py     # FSM transitions; bounded retries; leave/reset path
│   └── test_visual.py       # comparator scores on synthetic frames (numpy-gated)
├── examples/
│   └── pw2_fallen/          # example strat + generated reference frames
│       ├── pw2_fallen.strat.json
│       └── frames/*.png
├── requirements.txt
├── requirements-dev.txt
├── README.md
└── docs/PLAN.md             # this file
```

### Key signatures (selected)
```python
# geometry.py
@dataclass(frozen=True) class WindowGeometry: x:int; y:int; w:int; h:int; retina:float
class Coordinates:
    def __init__(self, geo: WindowGeometry): ...
    def norm_to_logical(self, p: Point) -> tuple[float,float]            # A
    def logical_to_norm(self, px: float, py: float) -> Point             # B
    def region_crop_box(self, r: Rect, img_w:int, img_h:int) -> tuple[int,int,int,int]  # E

# input_backend.py
class InputBackend(Protocol):
    def move(self, px, py, duration_ms=0, easing="linear", hz=120, clock=None): ...
    def click(self, button, pos=None, clicks=1, hold_ms=25): ...
    def drag(self, button, frm, to, duration_ms=300): ...           # press→stepped pts→release
    def press_key(self, key, modifiers=()); def release_key(self, key, modifiers=())
    def start_listeners(self, on_move, on_click, on_scroll, on_press, on_release): ...
    def release_all(self): ...                                      # atexit/finally/signal

# strat.py
@dataclass class StratFile: header:Header; config_overrides:dict; events:list[Event]; recovery:RecoverySpec
def load(path) -> StratFile        # validate, resolve+check ref paths, fail fast
def save(strat, path) -> None      # atomic temp+rename
def expand_macro(ev: Event) -> list[Event]   # place_tower/upgrade/ability → primitives

# engine.py
class Player:
    def __init__(self, strat, window, input, capture, comparator, clock, recovery, config, events): ...
    def run(self) -> RunStats
    def _adaptive_wait(self, sync: SyncPointEvent) -> WaitResult   # FIRE | recovery outcome
    # clock rebase is MONOTONIC (stretch-only): clock_offset = max(clock_offset, now - ev.t_ms)
    def _dispatch(self, ev: Event) -> None
    def _cooperative_sleep_until(self, t_ms: float) -> None        # panic-aware

# recovery.py
class RecoveryController:
    def handle(self, reason: FailureMode|str, *, scene=None) -> Outcome  # RETRY_LOOP|RESET_AND_RESTART|STOP
    def classify(self, window: Frame) -> FailureMode
    def _leave_match(self); def _reset_character(self); def _rejoin(self, expected_map)
```

---

## 5. Strat JSON format (human-editable)

```json
{
  "schema_version": 1,
  "header": {
    "name": "PW2 Fallen Solo", "game": "Tower Defense Simulator",
    "map": "Polluted Wastelands II", "difficulty": "Fallen", "mode": "solo",
    "created": "2026-06-23T14:31:07Z", "created_by": "emreknlk@gmail.com",
    "window_aspect": 1.7777778, "reference_resolution": {"w": 2560, "h": 1440},
    "retina_scale_captured_at": 2.0
  },
  "config_overrides": { "sync_default_threshold": 0.90, "loop_count": 0 },
  "events": [
    {"id":1,"t_ms":1500,"type":"place_tower","tower":"Farm","hotbar_slot":1,
     "pos":{"x":0.412,"y":0.631},"settle_ms":250,"comment":"farm 1 mid"},
    {"id":2,"t_ms":2900,"type":"upgrade","target_pos":{"x":0.555,"y":0.58},
     "upgrade_button_pos":{"x":0.895,"y":0.76},"times":2,"between_ms":300},
    {"id":3,"t_ms":6000,"type":"sync_point","label":"wave_5","ref_frame":"frames/wave5.png",
     "region":{"x":0.43,"y":0.0,"w":0.14,"h":0.07},"threshold":0.88,
     "timeout_ms":30000,"on_timeout":"recover","match":"tm_ccoeff_normed"}
  ],
  "recovery": {
    "wrong_map":  {"ref_frame":"frames/map_vote.png","region":{"x":0.30,"y":0.05,"w":0.40,"h":0.10},"threshold":0.85,"action":"leave_and_restart"},
    "disconnect": {"ref_frame":"frames/disconnect.png","region":{"x":0.33,"y":0.38,"w":0.34,"h":0.24},"threshold":0.92,"action":"reconnect_and_rejoin"}
  }
}
```

**Event types:** `mouse_move, click, drag, key_press, key_release, wait, sync_point` (primitives)
and `place_tower, upgrade, ability` (TDS macros that expand to primitives). All positions are
normalized `{x,y}` in 0–1. `sync_point.on_timeout ∈ {abort, continue, retry, recover}`.

---

## 6. Decisions on open questions (locked for v1)

1. **Macro sub-syncs:** macros expand to blind primitive sequences with small built-in waits; authors add `sync_point`s around fragile spots. (Keeps v1 simple.)
2. **Camera reset:** documented as the recommended first recorded action / `restart_sequence`; engine validates the post-reset frame but doesn't force it.
3. **Cursor occlusion:** engine parks the cursor to a neutral corner before each sync poll (`sync_park_cursor=True`).
4. **Loop restart == recovery rejoin:** single `_rejoin(expected_map)` path; no duplicate map-select code. **Two-layer:** leave/reset = Roblox-client anchors (stable defaults); rejoin = TDS-lobby `restart_sequence` recorded by the user (unseen UI → not hardcoded). Hard-disconnect-to-website → Roblox re-launch fallback before the TDS rejoin.
5. **Frames:** `frames/<label>.png` relative to the strat dir; validate existence + dims; no checksums in v1.
6. **OCR:** out of scope for v1. Out-of-cash detected via "expected post-action frame didn't appear." `method:ocr` reserved.
7. **Humanization:** jitter + click micro-offsets + session caps + randomized breaks ship in v1; first-run consent file gates auto-loop.
8. **Calibration:** thresholds auto-suggested at record time; achieved scores logged to `<strat>.scores.jsonl`; no auto-retune in v1.
9. **Linux:** Mock is the only non-macOS path; no real Linux input/capture backend in v1.
10. **Versioning:** single integer `schema_version` (drop `$schema`/`engine_version`); migration shims key off it.
11. **No Pydantic/typer:** stdlib `dataclasses` + manual validation + `argparse` → zero-dep core, fully testable on this Linux box.

---

## 7. Risk register (R01–R30) — see per-phase mapping in §3
env/permissions: R01 R02 R03 R04 · coordinates: R05 R06 R07 · timing: R08 R09 R10 ·
detection: R11 R12 R13 R23 R30 · recovery: R14 R15 R16 R17 · ToS/ban: R18 R19 ·
concurrency: R20 R21 R22 · file/schema: R24 R25 R26 · edge/usability: R27 R28 R29

---

## 8. Review fixes applied (AUTHORITATIVE — these override earlier prose on conflict)

Triple-check verdict: **GO-WITH-FIXES**. The 17 must-fixes (M1–M17) below are binding build directives.

### 8.1 Coordinate math (M6, M7)
- Conversions C/D are **monitor-relative**: subtract the grabbed monitor's origin `(Mx,My)` before applying retina (a left-hand monitor at logical x=−1920 must NOT yield −3840 px). D is now defined (inverse of C).
- y-origin is **top-left global logical** end-to-end; `QuartzWindowProvider` must return top-left bounds (not Cocoa-flipped). `test_geometry` asserts A∘B = id, C∘D = id, and a marker round-trip with a non-zero `(Mx,My)`.

### 8.2 Engine timing & panic (M1, M2, M8, M9, M10)
- **M1 — clock rebase is stretch-only/monotonic:** `clock_offset = max(clock_offset, now - ev.t_ms)`. A sync may push later events *later* only; an early frame fires immediately but never pulls downstream events earlier than recorded spacing. Test: two syncs, second recovers early → assert `clock_offset` non-decreasing and every later inter-event gap ≥ recorded gap.
- **M2 — stability vs timeout decoupled:** `validate()` requires `timeout_ms ≥ (stability_frames+1)*poll_ms + slack` (auto-bump + warn otherwise). The match check runs **before** the timeout check each poll, so a match on the final poll wins.
- **M8 — single timing authority:** `t_ms` is authoritative; `wait`/`settle_ms`/`between_ms` are *derived relative gaps*. `expand_macro` converts macro-internal waits into absolute `t_ms` so they participate in clock rebasing (a lagged placement's `settle_ms` stretches too — no hidden fixed-clock desync).
- **M9 — panic model:** the hotkey listener thread *only* sets a `threading.Event`. The **engine thread** polls it between every atomic action and inside every sleep/poll, then calls `release_all()` in its own `finally`. SIGINT/SIGTERM handlers run on the **main thread** and only set the same Event (engine runs on a worker; main waits). `atexit`+`try/finally` are best-effort backstops (don't cover SIGKILL). `release_all()` is idempotent + lock-guarded (S14).
- **M10 — bounded panic latency:** `_cooperative_sleep_until` sleeps in ≤15ms slices; `move()`/`drag()` take the panic Event and check it between every interpolation step, raising `PanicAbort` → engine `finally` → `release_all()`. Panic Event checked immediately before AND after each capture+compare; per-poll capture is sub-rect/downscaled to stay within the slice budget. Test: panic mid-drag leaves zero buttons held.

### 8.3 Comparator (M3, S1, S2)
- **M3 — resolution-agnostic:** after fractional crop (E), **resize both** reference and live crops to a canonical pixel size (the reference crop's stored dims) before NCC/SSIM/template; scale templates too. `retina_scale_captured_at` + live geometry detect mismatch. §1 "2x cancels" holds only same-retina; otherwise the comparator normalizes geometry. `test_visual` seeds a 1x-vs-2x same-content pair → high score.
- **S1 — masking:** `sync_point` and recovery detectors take optional `mask: [{x,y,w,h}]` normalized exclusion rects (cash/timer/particles); Comparator zeroes them before scoring.
- **S2 — rising-edge gate:** a match is accepted only after the region first *differed* from the target at wait-entry (or a brief negative-gate confirms the prior scene cleared), so back-to-back identical loop iterations don't false-match instantly.

### 8.4 Schema additions (M14, M15, M16, M17, S12)
- **M14 — `join_sequence`:** first-class list of primitive/sync events (lobby → map-select → start), recorded in Phase 2, replayed by *both* `_rejoin(expected_map)` and the loop restart. Without it auto-loop can't complete one iteration.
- **M15 — `run_end`:** `{victory:{ref_frame,region,threshold}, defeat:{...}, timeout_ms}`. Engine enters `WAIT_RUN_END` after the last event; the loop and win/loss branch consume it. Capture victory/defeat frames in the example.
- **M16 — positive `expected_map_check`:** `{ref_frame: frames/map_selected_<name>.png, region, threshold}` confirms the *intended* map before the run. "No positive match" ⇒ wrong-map trigger; "cannot confirm correct map" ⇒ hard stop (R14). Generic vote-screen match alone is insufficient.
- **M17 — disconnect action vocabulary:** disconnect/kicked ⇒ `reconnect_and_rejoin` (RECONNECTING → REJOINING); wrong_map/stuck *in a live match* ⇒ `reset_character` (RESETTING → LEAVING → LOBBY → REJOINING). Never "reset character" on a disconnect (no character exists).
- **S12 — optional `expect` on place_tower/upgrade:** `{ref_frame,region,threshold,timeout_ms}` so the engine can flag "action didn't take" → classify out-of-cash / state-mismatch (reconciles decision #6).

### 8.5 Recovery FSM — full transition table (M4, M5)
States: `CLASSIFY, REFOCUS, RECONNECTING, RELAUNCH_EXPERIENCE, RESETTING, LEAVING, LOBBY, REJOINING, WAIT_RUN_END, STOP`. CLASSIFY is **total** over `FailureMode`:

| FailureMode | → state | success → | fail/exhausted → |
|---|---|---|---|
| FOCUS_LOST | REFOCUS | resume prior state | STOP+notify |
| DISCONNECTED | RECONNECTING | REJOINING (or RELAUNCH_EXPERIENCE if dumped to website) | STOP+notify |
| KICKED | LOBBY | REJOINING | STOP+notify |
| WRONG_MAP | RESETTING→LEAVING→LOBBY | REJOINING | STOP+notify |
| STUCK_SYNC | (reclassify; else) RESETTING→LEAVING→LOBBY | REJOINING | STOP+notify |
| OUT_OF_CASH | LEAVING→LOBBY | REJOINING | STOP+notify |
| STATE_MISMATCH | reconcile to screen (LOBBY or resync) | resume/REJOINING | STOP+notify |
| DEFEAT / VICTORY | **not recovery** → POSTMATCH → farm loop | — | — |

- Every state has {success, failure, retry-exhausted=STOP} edges. `test_recovery` asserts CLASSIFY total and every state has an exit.
- **M5 — per-cause attempt caps** (e.g. ≤3 wrong-map rejoins) *independent* of the global "no state change" watchdog (which can't fire on the match→lobby→wrong-match cycle because state genuinely changes). Each rejoin decrements the budget even when the rejoin "succeeds" but map verification fails → STOP+notify.

### 8.6 Imports, validation, phase order (M11, M12, M13, S10)
- **M11 — clean core imports:** `frame.py` stores raw `bytes+shape` by default, `HAS_NUMPY` guard, lazy `as_numpy()`. All of cv2/pynput/mss/Quartz/objc/PIL imported only inside factory funcs/method bodies. `__init__.py` re-exports **only** value types, Protocols, and factory *functions* — never concrete backends. CI smoke test: `python -c "import tds_macro"` with numpy uninstalled.
- **M12 — registry validator:** `TYPE → dataclass` registry; per-type `from_dict` checks required keys, coerces/validates types, range-checks coords to [0,1], rejects unknown `type`/extra keys, validates enums, checks `schema_version`, verifies `ref_frame` exists and reads PNG dims from the 8-byte IHDR via stdlib `struct` (no PIL). Collects **all** errors, raises `StratValidationError` with event id + field.
- **M13 — interfaces early:** `RecoveryController` Protocol + recording mock are a **Phase 0/early-3** deliverable; `Player`/`_adaptive_wait` depend only on the `RecoveryController` and `Comparator` Protocols, with `MockComparator` injected → engine/adaptive-sync tests need **no** numpy/cv2.
- **S10 — test matrix:** stdlib-only suites = geometry/strat/recorder/recovery/engine/adaptive_sync (Mock everything + FakeClock); numpy-gated = test_visual via `pytest.importorskip("numpy")`.

### 8.7 Other should-fixes folded in
S3 (don't park cursor mid-placement), S4 (mss thread-confined, one capture owner), S5 (coalescer: pixel dead-zone, press-timestamp double-click), S6 (scroll primitive OR loud warn), S7 (`AXIsProcessTrustedWithOptions` prompt + variance-based black check), S8 (window pick: layer==0 + owner set, largest, per-display retina), S9 (documented key codec + round-trip test), S11 (atomic save: temp in same dir + fsync + os.replace, for PNG too), S13 (mandatory camera-anchor at run start), S15 (soften res-independence claim — done in §1).

---

## 9. Post-build verification (4-agent audit of the finished code)

Verdict: **SHIP** (41/47 checks satisfied; rest low-severity). All safety-critical
behaviours (M1/M2/M8/M9/M10), the schema/validator (M12/M14/M15/M16/M17), the
recovery FSM (M4/M5), comparator (M3), and geometry (M6/M7) confirmed against the
code with file:line evidence, plus all six user requirements end-to-end. 58 unit
tests pass; `import tds_macro` works without numpy.

Defects found and **fixed** after the audit:
- **(medium) Recovery was open-loop.** Added a `recovery.lobby_anchor` detector +
  `_confirm_at_lobby()`: leave/reset and reconnect now **visually confirm** they
  reached the hub before counting success (R17/§8.5). Added `RELAUNCH_EXPERIENCE`
  (`config.relaunch_url`, `open` the experience) as the hard-disconnect-to-website
  fallback. Confirmed recovery resets its per-cause budget; unconfirmed retries
  still climb to the M5 cap → STOP.
- **(low) M2 silent bump.** Engine now `log.warning`s when it raises a too-small
  `timeout_ms` instead of silently bumping.
- **(low) M12 top-level keys.** Validator now rejects unknown top-level strat keys.
- **(low) Sync-timeout classification.** `expect_*` action-verify syncs that time
  out now classify as `OUT_OF_CASH` (action didn't take), not generic `STUCK_SYNC`.

