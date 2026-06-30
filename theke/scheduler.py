# -- scheduler (phase 10: turn a schedule into fire times, loop one pass) ------
# The in-app scheduler's pure core: parse the `run_schedule` config into triggers,
# compute the next fire time, and drive a loop that runs an injected pass on each
# tick. Everything here is side-effect-free except run_loop, which only calls the
# injected pass_fn/on_result/wait_fn -- so the whole module is testable without a
# DB, the network, or real sleeping. The CLI wiring (cmd_run) and the pass body
# (_run_pass) live in theke.__init__; both are injected in here.
#
# All schedules are fixed-rate (a clock grid), never fixed-delay: the next fire is
# computed from the wall clock, not from when the last pass ended. A pass that
# overruns its slot collapses the missed ticks into one (next_run returns the next
# instant strictly after `now`), and the single-threaded loop makes overlap
# impossible by construction.

import logging
import threading
from datetime import datetime, timedelta

from theke.core import ConfigError

log = logging.getLogger("theke")

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _parse_at(entry: str) -> tuple:
    """Parse a calendar entry "[Weekday ]HH:MM" into (weekday|None, hour, minute);
    weekday is a Mon..Sun index or None for a daily time. Raises ConfigError on
    anything malformed."""
    parts = entry.split()
    if len(parts) == 2:
        weekday = WEEKDAYS.get(parts[0].lower())
        if weekday is None:
            raise ConfigError(f"invalid weekday in run_schedule entry: {entry!r}")
        clock = parts[1]
    elif len(parts) == 1:
        weekday, clock = None, parts[0]
    else:
        raise ConfigError(f"invalid run_schedule entry: {entry!r}")
    try:
        hour, minute = (int(p) for p in clock.split(":"))
    except ValueError:
        raise ConfigError(f"invalid time in run_schedule entry: {entry!r}") from None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ConfigError(f"time out of range in run_schedule entry: {entry!r}")
    return weekday, hour, minute


def parse_schedule(spec: list) -> tuple:
    """Split run_schedule into (run_at_start, triggers). `run_at_start` is set by a
    literal "start" entry (a pass the instant the daemon starts); the rest become
    tagged triggers in input order: an int N -> ("interval", N) seconds, a
    "[Weekday ]HH:MM" string -> ("daily", h, m) or ("weekly", wd, h, m)."""
    run_at_start = False
    triggers = []
    for entry in spec:
        if isinstance(entry, bool):              # bool is an int subclass -- reject
            raise ConfigError(f"invalid run_schedule entry: {entry!r}")
        if isinstance(entry, int):
            if entry <= 0:
                raise ConfigError(f"interval must be positive in run_schedule: {entry!r}")
            triggers.append(("interval", entry))
        elif isinstance(entry, str):
            if entry.lower() == "start":
                run_at_start = True
            else:
                weekday, hour, minute = _parse_at(entry)
                triggers.append(("daily", hour, minute) if weekday is None
                                else ("weekly", weekday, hour, minute))
        else:
            raise ConfigError(f"invalid run_schedule entry: {entry!r}")
    return run_at_start, triggers


def _fire_interval(now: datetime, seconds: int) -> datetime:
    """Next instant on the fixed grid of `seconds`, anchored to local midnight, so
    e.g. an hourly interval lands on the full hour. Strictly after `now`."""
    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    steps = int((now - base).total_seconds() // seconds) + 1
    return base + timedelta(seconds=steps * seconds)


def _fire_clock(now: datetime, hour: int, minute: int, period_days: int,
                weekday=None) -> datetime:
    """Next `hour:minute` strictly after `now`, rolling forward by `period_days`
    (1 daily, 7 weekly). For weekly, first advance to the target weekday."""
    cand = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if weekday is not None:
        cand += timedelta(days=(weekday - now.weekday()) % 7)
    return cand if cand > now else cand + timedelta(days=period_days)


def _fire(trigger: tuple, now: datetime) -> datetime:
    kind = trigger[0]
    if kind == "interval":
        return _fire_interval(now, trigger[1])
    if kind == "daily":
        return _fire_clock(now, trigger[1], trigger[2], 1)
    return _fire_clock(now, trigger[2], trigger[3], 7, weekday=trigger[1])


def next_run(now: datetime, triggers: list) -> datetime:
    """The soonest next fire across all triggers (strictly after `now`)."""
    return min(_fire(t, now) for t in triggers)


def _wait_until(nxt: datetime, stop: threading.Event):
    """Block until `nxt` or until `stop` is set (whichever first)."""
    stop.wait(max(0.0, (nxt - datetime.now()).total_seconds()))


def run_loop(pass_fn, on_result, schedule, *, wait_fn=None, now_fn=None,
             stop_event=None):
    """Drive the schedule: optionally a pass at start, then one pass per fire time
    until `stop_event` is set. `pass_fn` produces a result dict (an exception is
    caught and reported as {"error": ...} so a single bad pass never kills the
    loop); `on_result` consumes it. The clock (`now_fn`), the wait (`wait_fn`) and
    the stop flag are injectable so the loop runs without real time in tests. A
    schedule with no recurring trigger (only "start", or empty) runs the start
    pass, if any, and returns."""
    run_at_start, triggers = schedule
    stop = stop_event if stop_event is not None else threading.Event()
    now_fn = now_fn or datetime.now
    wait_fn = wait_fn or _wait_until

    def one():
        try:
            result = pass_fn()
        except Exception as exc:
            log.warning("run pass failed: %s", exc)
            result = {"error": str(exc)}
        on_result(result)

    if run_at_start:
        one()
        if stop.is_set():
            return
    while triggers and not stop.is_set():
        wait_fn(next_run(now_fn(), triggers), stop)
        if stop.is_set():
            break
        one()
