"""Import-safety: the package and every submodule import cleanly (plan M11).

The stronger 'imports without numpy' guarantee is checked in CI / by running
`python -c "import tds_macro"` on a numpy-less interpreter; here we at least
verify the whole graph imports and the public API is present.
"""

import importlib


SUBMODULES = [
    "errors", "geometry", "frame", "clock", "config", "window", "capture", "visual",
    "input_backend", "recovery", "permissions", "hotkeys", "strat", "recorder",
    "engine", "cli", "pngio",
]


def test_all_submodules_import():
    for m in SUBMODULES:
        importlib.import_module(f"tds_macro.{m}")


def test_public_api_present():
    import tds_macro as t
    for name in ("Config", "Player", "Recorder", "StratFile", "load", "save",
                 "RecoveryController", "make_input_backend", "Coordinates", "Frame"):
        assert hasattr(t, name), name
