"""What a request actually cost, attached to the line that reports it.

THE PROBLEM THIS SOLVES. The request log said how long a request took and nothing about why. That
is the wrong half: this process sits a network away from its database, so "slow" almost always
means "made more round trips than it needed to", and the difference between one query and sixteen
is invisible from the outside. Every performance bug found in this service so far — an aircraft
card at sixteen round trips, a 1:1 lookup at seven — was found by counting statements by hand.
This counts them in production, on every request, for nothing.

HOW IT ATTACHES. A ContextVar holds one small counter object per request. SQLAlchemy's
`before_cursor_execute` / `after_cursor_execute` fire on the Engine CLASS, so they cover every
engine this process opens, including ones created after startup; each one adds to whatever counter
the current context holds, or to nothing at all when there is none (a background task, a script,
the worker). The listeners are installed once, at import.

WHAT IT COSTS. Two `perf_counter()` calls and an integer add per statement. The alternative —
`echo=True`, or a sampling profiler — costs orders of magnitude more and is not on in production,
which is exactly where the numbers are wanted.

The slowest statement of a request is kept, truncated, so a slow line can say which query it was
without logging every query of every request.
"""
import time
from contextvars import ContextVar
from typing import Optional

from sqlalchemy import event
from sqlalchemy.engine import Engine

_current: ContextVar[Optional["RequestCost"]] = ContextVar("request_cost", default=None)

# How much of a statement to keep when it turns out to be the slow one. Enough to recognise the
# query, short enough that a log line stays one line.
_STATEMENT_CHARS = 160


class RequestCost:
    """Counters for one request. Plain attributes, mutated in place — this is on the hot path of
    every statement the request runs, and a dataclass or a lock would both be paying for nothing:
    a request runs on one task, and its statements run one after another."""

    __slots__ = ("queries", "db_seconds", "slowest_seconds", "slowest_statement")

    def __init__(self):
        self.queries = 0
        self.db_seconds = 0.0
        self.slowest_seconds = 0.0
        self.slowest_statement = ""

    def record(self, seconds: float, statement: str) -> None:
        self.queries += 1
        self.db_seconds += seconds
        if seconds > self.slowest_seconds:
            self.slowest_seconds = seconds
            self.slowest_statement = " ".join(statement.split())[:_STATEMENT_CHARS]

    def summary(self) -> str:
        """`db=3/71.2ms` — the two numbers that explain a slow request, in the order they matter:
        how many waits, and how long they took together."""
        return f"db={self.queries}/{self.db_seconds * 1000:.1f}ms"


def begin() -> RequestCost:
    """Start counting for this request. The token is not returned: the context is per-task and
    dies with it, so there is nothing to reset in an ASGI app."""
    cost = RequestCost()
    _current.set(cost)
    return cost


def current() -> Optional[RequestCost]:
    return _current.get()


@event.listens_for(Engine, "before_cursor_execute")
def _before(conn, cursor, statement, parameters, context, executemany):
    # Stamped on the execution context rather than in the ContextVar: `after` receives the same
    # context object, and nothing else is guaranteed to be the same by then.
    context._cost_started = time.perf_counter()


@event.listens_for(Engine, "after_cursor_execute")
def _after(conn, cursor, statement, parameters, context, executemany):
    cost = _current.get()
    if cost is None:
        return                      # a background task, a script, the worker — nothing to attach to
    started = getattr(context, "_cost_started", None)
    if started is None:
        return
    cost.record(time.perf_counter() - started, statement)
