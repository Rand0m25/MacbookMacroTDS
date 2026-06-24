"""End-to-end with a REAL screen: render images to an isolated Xvfb display,
capture them with the real mss backend, and drive the engine's visual-sync
barrier off actual on-screen pixels changing over time.

Skipped automatically when Xvfb / ImageMagick / numpy are unavailable, so the
core suite still runs anywhere.
"""

import os
import shutil
import subprocess
import tempfile
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(
    not (shutil.which("Xvfb") and shutil.which("display") and shutil.which("convert")),
    reason="needs Xvfb + ImageMagick",
)

pytest.importorskip("numpy")
pytest.importorskip("mss")

DISP = ":99"
W, H = 320, 240


def _set_root(path):
    try:
        subprocess.run(["display", "-window", "root", path],
                       env={**os.environ, "DISPLAY": DISP}, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pass  # the draw happens immediately even if the process lingers


@pytest.fixture(scope="module")
def scene():
    proc = subprocess.Popen(["Xvfb", DISP, "-screen", "0", f"{W}x{H}x24", "-nolock"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    old = os.environ.get("DISPLAY")
    os.environ["DISPLAY"] = DISP
    d = tempfile.mkdtemp(prefix="tdsx_")
    grad = os.path.join(d, "grad.png")
    plasma = os.path.join(d, "plasma.png")
    subprocess.run(["convert", "-size", f"{W}x{H}", "gradient:black-white", grad], check=True)
    subprocess.run(["convert", "-size", f"{W}x{H}", "plasma:fractal", plasma], check=True)
    try:
        import mss
        import numpy as np
        with mss.MSS() as sct:
            _set_root(grad)
            arr = np.asarray(sct.grab({"left": 0, "top": 0, "width": W, "height": H}))
        if arr.astype("float64").var() < 1.0:
            pytest.skip("Xvfb capture came back blank")
        yield {"dir": d, "grad": grad, "plasma": plasma}
    finally:
        if old is not None:
            os.environ["DISPLAY"] = old
        else:
            os.environ.pop("DISPLAY", None)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        shutil.rmtree(d, ignore_errors=True)


def test_real_capture_matches_its_own_reference(scene):
    from tds_macro.capture import MssCaptureBackend
    from tds_macro.visual import NumpyComparator, load_reference
    from tds_macro.geometry import WindowGeometry, Rect
    from tds_macro.config import MatchMethod

    # Use the TEXTURED scene: a smooth gradient is both poor for correlation and
    # rendered unfaithfully by `display -window root` (see docs/BUGLOG.md BUG-2).
    _set_root(scene["plasma"])
    cap = MssCaptureBackend()
    geo = WindowGeometry(0, 0, W, H, 1.0)
    live = cap.grab_region(geo, Rect(0, 0, 1, 1))
    cmp = NumpyComparator()
    s_match = cmp.score(live, load_reference(scene["plasma"]), MatchMethod.TM_CCOEFF_NORMED)
    s_other = cmp.score(live, load_reference(scene["grad"]), MatchMethod.TM_CCOEFF_NORMED)
    cap.close()
    assert s_match > 0.9          # real capture matches the image actually on screen
    assert s_match > s_other + 0.1  # and is distinguishable from a different scene


def test_adaptive_wait_reacts_to_real_screen_change(scene):
    from tds_macro.capture import MssCaptureBackend
    from tds_macro.visual import NumpyComparator, load_reference
    from tds_macro.input_backend import MockInputBackend
    from tds_macro.recovery import MockRecoveryController
    from tds_macro.window import MockWindowProvider
    from tds_macro.clock import RealClock
    from tds_macro.engine import Player, WaitResult
    from tds_macro.geometry import Rect
    from tds_macro import strat as S

    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from helpers import mock_config

    _set_root(scene["grad"])  # start on the NON-target scene
    sync = S.SyncPointEvent(1, 0, "sync_point", label="plasma", ref_frame="plasma.png",
                            region=Rect(0, 0, 1, 1), threshold=0.9, timeout_ms=8000,
                            stability_frames=1)
    st = S.StratFile(events=[sync], base_dir=scene["dir"])
    player = Player(st, MockWindowProvider(rect=(0, 0, W, H), retina=1.0), MockInputBackend(),
                    MssCaptureBackend(), NumpyComparator(), RealClock(), MockRecoveryController(),
                    mock_config(sync_park_cursor=False), ref_loader=load_reference)

    def swap():
        time.sleep(0.6)
        _set_root(scene["plasma"])  # the target scene appears mid-wait

    t = threading.Thread(target=swap)
    t.start()
    start = time.monotonic()
    result, _ = player._adaptive_wait(sync)
    elapsed = time.monotonic() - start
    t.join()
    player.capture.close()

    assert result == WaitResult.FIRE      # fired off a real on-screen change
    assert 0.4 < elapsed < 7.0            # genuinely waited for the change, didn't fire early
