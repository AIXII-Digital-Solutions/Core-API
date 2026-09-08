"""Currency -> USD rates for the claims loader.

A claims sheet states amounts in whatever currency the policy was written in, so a report cannot add
them up. The loader records the rate it used and the converted amount next to the original (see
forecast.acys_claims), and this module is where the rate comes from.

WHICH RATE. A row is converted at the rate of ITS OWN calendar year, not today's: a 2019 claim is
worth what it was worth in 2019, and booking it at today's rate would restate history by tens of
percent. The year is represented by its LAST DAY (31 December) — the close-of-year rate — and only a
row whose year is the current one (or, defensively, a later one) uses the latest published rate,
because 31 December has not happened yet.

That choice is close-of-year, not the year's average. An average would arguably suit an amount that
accumulated over twelve months, but it needs the whole daily series and a stated averaging rule;
close-of-year is one published number that anyone can look up and check. Swapping it later means
changing _year_reference_date and nothing else.

SOURCES. Primary is Frankfurter (api.frankfurter.dev), which publishes the European Central Bank's
daily reference rates: no API key, no quota, a stated update schedule, and — crucially here — the
same endpoint shape for a historical date, where it rolls back to the last working day on its own
(asking for 2022-12-31 answers with 2022-12-30). Fallback is open.er-api.com, also key-less, but it
serves ONLY the current rate: it can stand in for a current-year row and never for a historical one,
so a historical lookup that fails is reported rather than quietly answered with today's number.

ECB data starts at 1999-01-04. A row dated before that cannot be converted and is refused — the
column is nullable precisely so that "no rate exists for this year" stays visible instead of being
papered over with a modern one.

CACHING. A closed year's rate never changes, so it is cached for a long time under the year itself;
the current-year rate is cached under today's date, so a new publication is simply a new key. Either
way a bulk load of a thousand rows costs at most one lookup per (currency, year) pair it contains.

FAILURE. If no source answers, the loader is told so and the write is refused rather than stored with
a missing or guessed rate — forecast.acys_claims allows the pair to be NULL, but a silently
unconverted row would still be found later and believed.
"""
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import httpx

from Config import setup_logger

logger = setup_logger("currency_rates")

USD = "USD"

_CACHE_PREFIX = "fx:usd"
# A closed year's close-of-year rate is immutable, so it is worth keeping; the current-year rate is
# keyed by today's date, so a new publication is a new key and the TTL is only a backstop.
_CACHE_TTL_CURRENT = 36 * 3600
_CACHE_TTL_HISTORICAL = 30 * 24 * 3600

_HTTP_TIMEOUT = 8.0                  # a load waits on this, so it stays short
_PRIMARY_URL = "https://api.frankfurter.dev/v1/{when}"
_FALLBACK_URL = "https://open.er-api.com/v6/latest/{base}"

# ECB reference rates begin here; nothing earlier can be converted.
_EARLIEST_YEAR = 1999


class RateUnavailable(RuntimeError):
    """No FX source could supply the rate. The caller refuses the write rather than guessing."""


def _as_rate(value) -> Decimal:
    """Rates arrive as JSON floats; convert via str so the decimal is the printed value, not the
    binary approximation of it."""
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, TypeError):
        raise RateUnavailable(f"FX source returned a non-numeric rate: {value!r}")
    if rate <= 0:
        raise RateUnavailable(f"FX source returned a non-positive rate: {rate}")
    return rate


def _year_reference_date(year: int) -> str:
    """The date a calendar year is converted at: its last day. Frankfurter rolls back to the last
    working day by itself (2022-12-31 answers with 2022-12-30), so no calendar arithmetic is needed
    here — and doing it here would only guess at which days the ECB published."""
    return f"{year}-12-31"


def _is_current_or_future(year) -> bool:
    """A row in the current year has no close-of-year rate yet, so it takes the latest published one.
    A year in the FUTURE takes it too: the alternative is refusing a row over a date that has simply
    not arrived, and the latest rate is the only honest answer available."""
    return year is None or int(year) >= date.today().year


async def _fetch_primary(client: httpx.AsyncClient, base: str, when: str):
    """`when` is 'latest' or an ISO date. Both are the same endpoint on Frankfurter."""
    r = await client.get(_PRIMARY_URL.format(when=when), params={"base": base, "symbols": USD})
    if r.status_code == 404:
        # out of range in either direction: before the ECB series starts, or a date not yet reached
        raise RateUnavailable(f"Frankfurter has no data for {when}")
    r.raise_for_status()
    body = r.json()
    rate = (body.get("rates") or {}).get(USD)
    if rate is None:
        raise RateUnavailable(f"Frankfurter has no {base}->{USD} rate for {when}")
    return _as_rate(rate), body.get("date") or when


async def _fetch_fallback(client: httpx.AsyncClient, base: str, when: str):
    """Current rate only. Refuses to answer a historical question with today's number, which would be
    a wrong answer wearing the shape of a right one."""
    if when != "latest":
        raise RateUnavailable("fallback source serves only the current rate")
    r = await client.get(_FALLBACK_URL.format(base=base))
    r.raise_for_status()
    body = r.json()
    if body.get("result") != "success":
        raise RateUnavailable(f"exchangerate-api returned {body.get('result')!r} for {base}")
    rate = (body.get("rates") or {}).get(USD)
    if rate is None:
        raise RateUnavailable(f"exchangerate-api has no {base}->{USD} rate")
    return _as_rate(rate), (body.get("time_last_update_utc") or "")[:16] or date.today().isoformat()


async def get_usd_rate(redis, currency: str, year=None) -> Decimal:
    """The currency -> USD rate for a row of `year`. USD is 1 and never hits the network or cache.

    `year` None, the current year or a later one => the latest published rate. Any earlier year =>
    that year's close-of-year rate. `redis` may be None (cache skipped); a Redis failure is logged
    and ignored, since a cache that is down must not take the loader with it."""
    cur = (currency or "").strip().upper()
    if not cur:
        raise RateUnavailable("no currency given")
    if cur == USD:
        return Decimal(1)

    if _is_current_or_future(year):
        when, cache_slot, ttl = "latest", date.today().isoformat(), _CACHE_TTL_CURRENT
    else:
        y = int(year)
        if y < _EARLIEST_YEAR:
            raise RateUnavailable(
                f"no published {cur}->{USD} rate for {y}: the ECB reference series starts in "
                f"{_EARLIEST_YEAR}")
        when, cache_slot, ttl = _year_reference_date(y), str(y), _CACHE_TTL_HISTORICAL

    key = f"{_CACHE_PREFIX}:{cur}:{cache_slot}"

    if redis is not None:
        try:
            cached = await redis.get(key)
            if cached:
                value = cached.decode() if isinstance(cached, (bytes, bytearray)) else str(cached)
                return _as_rate(value)
        except Exception as ex:      # a broken cache is not a broken load
            logger.warning("FX cache read failed for %s %s: %s", cur, cache_slot, ex)

    errors = []
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        for name, fetch in (("frankfurter", _fetch_primary), ("exchangerate-api", _fetch_fallback)):
            try:
                rate, published = await fetch(client, cur, when)
                logger.info("FX %s->USD = %s for %s (%s, published %s)",
                            cur, rate, cache_slot, name, published)
                if redis is not None:
                    try:
                        await redis.set(key, str(rate), ex=ttl)
                    except Exception as ex:
                        logger.warning("FX cache write failed for %s %s: %s", cur, cache_slot, ex)
                return rate
            except Exception as ex:
                errors.append(f"{name}: {ex}")
                logger.warning("FX source %s failed for %s %s: %s", name, cur, cache_slot, ex)

    raise RateUnavailable(
        f"no FX source could supply {cur}->{USD} for {cache_slot} ({'; '.join(errors)})")


async def get_usd_rates(redis, pairs) -> dict:
    """Rates for several (currency, year) pairs, fetched once per DISTINCT pair.

    A sheet repeats the same two or three currencies across many years, and each (currency, year)
    is one lookup — not one per row. Returns {(CUR, year): rate}. Raises on the first pair that
    cannot be resolved, so the caller can refuse the whole batch instead of writing part of it."""
    out: dict = {}
    for currency, year in pairs:
        key = ((currency or "").strip().upper(), year)
        if key not in out:
            out[key] = await get_usd_rate(redis, key[0], key[1])
    return out


def to_usd(amount: Decimal, rate: Decimal) -> Decimal:
    """Convert and round to cents, half-up — the rounding a person doing this by hand would use.

    Quantized here rather than left to the database so the stored value is the one this code decided
    on, not whatever the column's scale would have silently done to it."""
    return (Decimal(amount) * Decimal(rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


__all__ = ["RateUnavailable", "get_usd_rate", "get_usd_rates", "to_usd", "USD"]
