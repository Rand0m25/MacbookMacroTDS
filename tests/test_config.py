"""Config coercion + validation guards (round 26 audit fixes #7, #8)."""

import pytest

from tds_macro.config import Config, InputBackendKind, ScreenBackendKind, WindowBackendKind


# --- #8: numeric fields must reject a JSON boolean (bool is an int subclass) ---
def test_retina_override_rejects_bool():
    with pytest.raises(ValueError):
        Config().with_overrides({"retina_scale_override": True})
    with pytest.raises(ValueError):
        Config().with_overrides({"retina_scale_override": False})


def test_retina_override_accepts_real_number():
    assert Config().with_overrides({"retina_scale_override": 2.0}).retina_scale_override == 2.0


# --- #7: an incoherent mock-window + real-input mix must be rejected by validate() ---
def test_validate_rejects_mock_window_with_real_input():
    c = Config(window_backend=WindowBackendKind.MOCK, input_backend=InputBackendKind.PYNPUT)
    assert any("incoherent backends" in p for p in c.validate())


def test_validate_rejects_mock_screen_with_real_input():
    c = Config(screen_backend=ScreenBackendKind.MOCK, input_backend=InputBackendKind.PYNPUT)
    assert any("incoherent backends" in p for p in c.validate())


def test_all_mock_backends_are_coherent():
    c = Config(window_backend=WindowBackendKind.MOCK, screen_backend=ScreenBackendKind.MOCK,
               input_backend=InputBackendKind.MOCK, window_rect_override=(0, 0, 1600, 900))
    assert not any("incoherent" in p for p in c.validate())


def test_strat_overrides_cannot_quietly_switch_to_mock_window():
    # a strat's config_overrides flipping window_backend to "mock" on a real run is now caught
    c = Config().with_overrides({"window_backend": "mock"})
    assert any("incoherent backends" in p for p in c.validate())


# --- cursor-behaviour defaults: don't yank the cursor away during a sync; do start inside the window ---
def test_sync_park_cursor_defaults_off():
    # parking visibly jerks the cursor to the corner mid-run; it's now opt-in (was True).
    assert Config().sync_park_cursor is False


def test_center_cursor_on_play_defaults_on():
    assert Config().center_cursor_on_play is True


# --- safety: an empty/blank panic hotkey is silently skipped by HotkeyManager -> reject it ---
def test_validate_rejects_empty_panic_hotkey():
    assert any("panic_hotkey" in p for p in Config(panic_hotkey="").validate())
    assert any("panic_hotkey" in p for p in Config(panic_hotkey="   ").validate())
    assert any("start_hotkey" in p for p in Config(start_hotkey="").validate())


def test_validate_accepts_default_hotkeys():
    assert not any("hotkey" in p for p in Config().validate())


def test_humanize_defaults_on():
    assert Config().humanize is True
