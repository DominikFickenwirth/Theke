"""Phase 10: the in-app scheduler. Pure parts only -- schedule parsing, next-fire
computation and the loop driver -- driven with injected clock/wait seams so no
real time passes. The single-pass body (_run_pass) and the CLI wiring are tested
in test_library.py / test_theke.py."""

import threading
from datetime import datetime

import pytest

from theke import scheduler
from theke.core import ConfigError


# -- _parse_at ----------------------------------------------------------------

def test_parse_at_daily():
    assert scheduler._parse_at("03:00") == (None, 3, 0)


def test_parse_at_weekly():
    assert scheduler._parse_at("Mon 20:00") == (0, 20, 0)


def test_parse_at_weekly_case_and_single_digit():
    assert scheduler._parse_at("sun 9:05") == (6, 9, 5)


@pytest.mark.parametrize("bad", ["25:00", "10:99", "Xyz 10:00", "noon", "10"])
def test_parse_at_rejects_garbage(bad):
    with pytest.raises(ConfigError):
        scheduler._parse_at(bad)


# -- parse_schedule -----------------------------------------------------------

def test_parse_schedule_mixed():
    # "start" toggles the at-start pass; the rest normalize to tagged triggers,
    # order preserved.
    assert scheduler.parse_schedule(["start", "03:00", "Mon 20:00", 3600]) == (
        True,
        [("daily", 3, 0), ("weekly", 0, 20, 0), ("interval", 3600)],
    )


def test_parse_schedule_start_is_case_insensitive():
    assert scheduler.parse_schedule(["START"]) == (True, [])


def test_parse_schedule_without_start():
    assert scheduler.parse_schedule(["03:00"]) == (False, [("daily", 3, 0)])


def test_parse_schedule_empty():
    assert scheduler.parse_schedule([]) == (False, [])


@pytest.mark.parametrize("bad", [["garbage"], [0], [-5], [3.5], [None]])
def test_parse_schedule_rejects_bad_entries(bad):
    with pytest.raises(ConfigError):
        scheduler.parse_schedule(bad)


# -- next_run -----------------------------------------------------------------
# All naive local datetimes; expectations hand-derived from NOW below.
# NOW = 2026-06-30 10:30:00 is a Tuesday (weekday 1): 2025-01-01 was a Wednesday
# (weekday 2), 2026-01-01 is therefore a Thursday (weekday 3), and 2026-06-30 is
# 180 days later -> (3 + 180) % 7 == 1 -> Tuesday.

NOW = datetime(2026, 6, 30, 10, 30, 0)


def test_next_run_interval_anchored_to_midnight():
    # midnight + ceil grid; 10:30 with a 1h interval -> the next full hour 11:00.
    assert scheduler.next_run(NOW, [("interval", 3600)]) == datetime(2026, 6, 30, 11, 0, 0)


def test_next_run_interval_strictly_after_a_grid_instant():
    # exactly on a fire instant fires the next one, never "now".
    assert scheduler.next_run(datetime(2026, 6, 30, 10, 0, 0),
                              [("interval", 3600)]) == datetime(2026, 6, 30, 11, 0, 0)


def test_next_run_daily_rolls_to_tomorrow():
    assert scheduler.next_run(NOW, [("daily", 3, 0)]) == datetime(2026, 7, 1, 3, 0, 0)


def test_next_run_daily_later_today():
    assert scheduler.next_run(datetime(2026, 6, 30, 1, 0, 0),
                              [("daily", 3, 0)]) == datetime(2026, 6, 30, 3, 0, 0)


def test_next_run_weekly():
    # Tue 10:30 -> next Monday (2026-07-06) 20:00.
    assert scheduler.next_run(NOW, [("weekly", 0, 20, 0)]) == datetime(2026, 7, 6, 20, 0, 0)


def test_next_run_picks_the_soonest_trigger():
    triggers = [("interval", 3600), ("daily", 3, 0), ("weekly", 0, 20, 0)]
    assert scheduler.next_run(NOW, triggers) == datetime(2026, 6, 30, 11, 0, 0)


# -- run_loop -----------------------------------------------------------------
# The loop is driven by an injected wait_fn (no real sleeping): it records the
# requested fire times and sets the stop event after a configured number of
# calls, which is how every test terminates the loop deterministically.

class FakeWait:
    """wait_fn stand-in: records each requested next-fire and trips `stop` once
    it has been called `stop_after` times (so the loop ends after N waits)."""
    def __init__(self, stop_after):
        self.stop_after = stop_after
        self.seen = []

    def __call__(self, nxt, stop):
        self.seen.append(nxt)
        if len(self.seen) >= self.stop_after:
            stop.set()


def run(schedule, wait, results):
    scheduler.run_loop(
        pass_fn=lambda: {"ok": True},
        on_result=results.append,
        schedule=schedule,
        wait_fn=wait,
        now_fn=lambda: NOW,
        stop_event=threading.Event())


def test_run_loop_start_then_scheduled_passes():
    results = []
    wait = FakeWait(stop_after=2)
    run((True, [("interval", 3600)]), wait, results)
    # one start pass + one more before the second wait trips the stop.
    assert len(results) == 2
    assert len(wait.seen) == 2


def test_run_loop_without_start_skips_the_first_pass():
    results = []
    wait = FakeWait(stop_after=2)
    run((False, [("interval", 3600)]), wait, results)
    assert len(results) == 1   # no at-start pass; one scheduled pass before stop


def test_run_loop_start_only_runs_once_and_never_waits():
    results = []
    wait = FakeWait(stop_after=1)
    run((True, []), wait, results)
    assert len(results) == 1
    assert wait.seen == []      # no recurring trigger -> loop exits, no wait


class FakeStop:
    """Event stand-in for _wait_until: records each wait() timeout and trips
    itself once it has been waited on `set_after` times."""
    def __init__(self, set_after):
        self.set_after = set_after
        self.waited = []
        self._set = False

    def is_set(self):
        return self._set

    def wait(self, timeout):
        self.waited.append(timeout)
        if len(self.waited) >= self.set_after:
            self._set = True


def test_wait_until_polls_in_one_second_slices_until_stopped():
    # a far-future deadline is waited in <=1s slices, so a stop set by a signal
    # (seen only between waits, esp. on Windows) is honored within one slice.
    stop = FakeStop(set_after=3)
    scheduler._wait_until(datetime(2026, 6, 30, 11, 0, 0), stop,
                          now_fn=lambda: datetime(2026, 6, 30, 10, 0, 0))
    assert stop.waited == [1.0, 1.0, 1.0]


def test_wait_until_returns_immediately_when_already_stopped():
    stop = FakeStop(set_after=1)
    stop._set = True
    scheduler._wait_until(datetime(2026, 6, 30, 11, 0, 0), stop,
                          now_fn=lambda: datetime(2026, 6, 30, 10, 0, 0))
    assert stop.waited == []


def test_wait_until_caps_the_last_slice_and_stops_at_the_deadline():
    # 0.4 s left -> one 0.4 s wait, then the deadline has passed -> return.
    times = iter([datetime(2026, 6, 30, 10, 0, 0),
                  datetime(2026, 6, 30, 10, 0, 1)])
    stop = FakeStop(set_after=99)
    scheduler._wait_until(datetime(2026, 6, 30, 10, 0, 0, 400000), stop,
                          now_fn=lambda: next(times))
    assert stop.waited == [0.4]


def test_run_loop_guards_a_failing_pass():
    results = []
    wait = FakeWait(stop_after=1)

    def boom():
        raise RuntimeError("pass down")

    scheduler.run_loop(
        pass_fn=boom, on_result=results.append,
        schedule=(True, [("interval", 3600)]),
        wait_fn=wait, now_fn=lambda: NOW, stop_event=threading.Event())
    assert results == [{"error": "pass down"}]   # error captured, loop survived
