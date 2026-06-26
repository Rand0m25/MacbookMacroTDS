"""Generate a complete example strat for 'Polluted Wastelands II' with synthetic
reference frames, so `tds_macro validate` passes and `play --mock`/`calibrate`
have something to run. Frames are placeholder solid-colour PNGs (real ones come
from recording on a Mac). Pure stdlib (uses tds_macro.pngio).

Run:  python examples/make_example.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tds_macro.geometry import Point, Rect
from tds_macro.pngio import write_png
from tds_macro.strat import (
    AbilityEvent, ClickEvent, DetectorSpec, Header, KeyPressEvent, PlaceTowerEvent,
    RecoverySpec, RunEnd, StratFile, SyncPointEvent, UpgradeEvent, WaitEvent, save,
)

HERE = os.path.dirname(os.path.abspath(__file__))
STRAT_DIR = os.path.join(HERE, "pw2_fallen")
FRAMES = os.path.join(STRAT_DIR, "frames")


def _solid(path, w, h, rgba):
    write_png(path, bytes(rgba) * (w * h), w, h, 4)


def _frames():
    os.makedirs(FRAMES, exist_ok=True)
    palette = {
        "wave5.png": (40, 90, 160, 255),
        "map_selected.png": (60, 140, 60, 255),
        "wrong_map.png": (160, 60, 60, 255),
        "disconnect.png": (20, 20, 20, 255),
        "victory.png": (220, 200, 60, 255),
        "defeat.png": (170, 30, 30, 255),
        "lobby.png": (90, 90, 120, 255),
    }
    for name, color in palette.items():
        _solid(os.path.join(FRAMES, name), 64, 48, color)


def build() -> StratFile:
    header = Header(
        name="PW2 Fallen Solo (example)", map="Polluted Wastelands II", difficulty="Fallen",
        mode="solo", created="2026-06-23T00:00:00Z", created_by="example",
        window_aspect=round(1600 / 900, 6), reference_resolution={"w": 3200, "h": 1800},
        retina_scale_captured_at=2.0,
        # Set this to your TDS private-server link to always (re)join the same server;
        # leave "" to use the lobby-click join_sequence below. (placeholder, not a real link)
        private_server_url="",
        notes="Synthetic example. Re-record frames + coords on your Mac before real use.",
    )
    # TDS-level rejoin (lobby -> map -> start). Placeholder coords.
    join = [
        ClickEvent(1, 0, "click", pos=Point(0.5, 0.85), comment="lobby Play"),
        WaitEvent(2, 1500, "wait", duration_ms=1500, reason="matchmaking"),
        ClickEvent(3, 1500, "click", pos=Point(0.5, 0.55), comment="select map tile"),
        ClickEvent(4, 2200, "click", pos=Point(0.85, 0.9), comment="ready/vote"),
    ]
    # Roblox-level leave/reset (Esc -> Leave). Placeholder coords.
    leave_reset = [
        KeyPressEvent(1, 0, "key_press", key="esc"),
        WaitEvent(2, 300, "wait", duration_ms=300),
        ClickEvent(3, 300, "click", pos=Point(0.5, 0.62), comment="Leave button"),
    ]
    events = [
        WaitEvent(1, 0, "wait", duration_ms=1500, reason="round 1 intro"),
        PlaceTowerEvent(2, 1500, "place_tower", tower="Farm", hotbar_slot=1, pos=Point(0.41, 0.63), settle_ms=250),
        UpgradeEvent(3, 2900, "upgrade", target_pos=Point(0.41, 0.63),
                     upgrade_button_pos=Point(0.9, 0.76), times=2, between_ms=300),
        SyncPointEvent(4, 6000, "sync_point", label="wave5", ref_frame="frames/wave5.png",
                       region=Rect(0.43, 0.0, 0.14, 0.07), threshold=0.85, timeout_ms=30000,
                       on_timeout="recover", comment="wait for wave 5; stretches under lag"),
        PlaceTowerEvent(5, 6500, "place_tower", tower="Minigunner", hotbar_slot=2, pos=Point(0.55, 0.5), settle_ms=250),
        AbilityEvent(6, 8000, "ability", tower_pos=Point(0.41, 0.63), ability_button_pos=Point(0.9, 0.84),
                     comment="farm/commander ability"),
    ]
    return StratFile(
        header=header,
        config_overrides={"loop_count": 0, "sync_default_threshold": 0.88,
                          "relaunch_url": "roblox://placeId=3260590327"},
        events=events, join_sequence=join, leave_reset_sequence=leave_reset,
        run_end=RunEnd(
            victory=DetectorSpec("frames/victory.png", Rect(0.35, 0.2, 0.3, 0.2), 0.85),
            defeat=DetectorSpec("frames/defeat.png", Rect(0.35, 0.2, 0.3, 0.2), 0.85),
            timeout_ms=900000),
        expected_map_check=DetectorSpec("frames/map_selected.png", Rect(0.3, 0.05, 0.4, 0.1), 0.85),
        recovery=RecoverySpec(
            wrong_map=DetectorSpec("frames/wrong_map.png", Rect(0.3, 0.05, 0.4, 0.1), 0.85, "leave_and_restart"),
            disconnect=DetectorSpec("frames/disconnect.png", Rect(0.33, 0.38, 0.34, 0.24), 0.9, "reconnect_and_rejoin"),
            lobby_anchor=DetectorSpec("frames/lobby.png", Rect(0.0, 0.0, 0.3, 0.1), 0.85)),
        base_dir=STRAT_DIR,
    )


def main():
    os.makedirs(STRAT_DIR, exist_ok=True)
    _frames()
    strat = build()
    path = os.path.join(STRAT_DIR, "pw2_fallen.strat.json")
    save(strat, path)
    print(f"wrote {path} and {len(os.listdir(FRAMES))} frames in {FRAMES}")


if __name__ == "__main__":
    main()
