"""Number-row digit keys must be sent as the number ROW, not the numeric keypad.

On macOS, pynput's char->keycode map resolves the digit characters "1".."0" to the NUMERIC KEYPAD
keycodes (e.g. "1" -> 83 = Keypad 1). A game bound to the number ROW (the TDS tower hotbar 1-9) ignores
those, so the tower is never selected ("like the towers were never there"). key_to_pynput forces the
number-row virtual keycode for plain digits on macOS.
"""

import sys

import pytest

from tds_macro.input_backend import _MACOS_NUMBER_ROW_VK, key_to_pynput

# kVK_ANSI_1 .. kVK_ANSI_0 (the top number row), from macOS HIToolbox Events.h
_EXPECTED = {"1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "5": 0x17,
             "6": 0x16, "7": 0x1A, "8": 0x1C, "9": 0x19, "0": 0x1D}


def test_number_row_vk_map_is_correct():
    assert _MACOS_NUMBER_ROW_VK == _EXPECTED


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-specific keycode resolution")
def test_digits_resolve_to_number_row_keycode_on_macos():
    # each digit must map to a pynput KeyCode carrying the number-ROW vk (not the bare char, which pynput
    # would resolve to the numeric keypad, and not None).
    for ch, vk in _EXPECTED.items():
        pk = key_to_pynput(ch)
        assert getattr(pk, "vk", None) == vk, f"{ch!r} -> {pk!r} (expected number-row vk {vk:#x})"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-specific keycode resolution")
def test_letters_unaffected_on_macos():
    # letters were never broken: they stay the bare char (pynput resolves them to the right key)
    assert key_to_pynput("e") == "e"
    assert key_to_pynput("q") == "q"
