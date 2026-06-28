"""cmd_play exit-code contract: a startup failure (e.g. a cold-start launch that never produced a
Roblox window) must exit non-zero, not report a false 'done' success (review round 24 #1)."""

from tds_macro.cli import _play_exit_code
from tds_macro.engine import RunStats


def test_worker_crash_is_failure():
    assert _play_exit_code(None) == 1


def test_cold_start_launch_failure_is_failure():
    # runs == 0 AND an 'error:' reason == it never got going (cold-start launch timed out)
    assert _play_exit_code(RunStats(runs=0, stopped_reason="error: WindowNotFoundError: not running")) == 1


def test_recovery_gave_up_with_zero_runs_is_failure():
    # recovery STOP / restart-budget-exhausted with zero completed matches must be a failure too
    assert _play_exit_code(RunStats(runs=0, stopped_reason="recovery stopped on wrong_map")) == 1
    assert _play_exit_code(RunStats(
        runs=0, stopped_reason="aborted after 10 consecutive restarts without a completed run")) == 1


def test_clean_loop_stop_is_success():
    assert _play_exit_code(RunStats(runs=3, stopped_reason="loop_count reached")) == 0


def test_panic_with_zero_runs_is_not_a_failure():
    # a user-initiated panic before any match completed is a clean stop, not a startup error
    assert _play_exit_code(RunStats(runs=0, stopped_reason="panic")) == 0


def test_session_cap_with_zero_runs_is_not_a_failure():
    # a configured time limit is a clean stop even if no match finished
    assert _play_exit_code(RunStats(runs=0, stopped_reason="session cap reached")) == 0


def test_mid_run_error_after_a_completed_match_is_success():
    # it did real work (runs > 0); the error reason is reported but the process still exits 0
    assert _play_exit_code(RunStats(runs=1, stopped_reason="error: RuntimeError: window vanished")) == 0


def test_empty_strat_clean_stop_is_success():
    assert _play_exit_code(RunStats(runs=0, stopped_reason="")) == 0
