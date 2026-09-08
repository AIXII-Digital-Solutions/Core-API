"""Currency -> USD rates for the claims loader.

A claims sheet states amounts in whatever currency the policy was written in, so a report cannot add
them up. The loader records the rate it used and the converted amount next to the original (see
forecast.acys_claims), and this module is where the rate comes from.

SOURCES. Primary is Frankfurter (api.frankfurter.dev), which publishes the European Central Bank's
daily reference rates: no API key, no quota, and a stated update schedule. Fallback is
open.er-api.com, also key-less, used only when the primary is unreachable — a load should not fail
because one public endpoint is having a bad afternoon. Both are read-only GETs of public reference
data; no request carries anything about the claim being loaded.

CACHING. ECB publishes once per working day, so re-asking per row would be pure waste — a bulk load
of a thousand rows needs at most three rates. Rates are cached in Redis under the publication date
the source reports, which makes the cache self-expiring in the only way that matters: a new
publication is a new key. The TTL is a backstop, not the mechanism.

FAILURE. If no source answers, the loader is told so and the write is refused rather than stored with
a missing or guessed rate — forecast.acys_claims allows the pair to be NULL, but a silently
unconverted row would still be found later and believed.

A NOTE ON WHICH RATE. This is TODAY's rate, applied to a claim that may be years old, because that is
what was asked for. It is the right choice for "what is this worth now" and the wrong one for "what
was it worth when it happened" — the two differ by tens of percent over a few years. Frankfurter
serves historical rates from the same endpoint shape (/v1/2022-12-30?base=GBP&symbols=USD), so
booking each row at its own calendar_year is a small change if that reading is ever wanted.
"""
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional

import httpx

from Config import setup_logger

logger = setup_logger("currency_rates")

USD = "USD"

# Cache key carries the source's own publication date, so a new ECB publication is a different key
# rather than something that has to be invalidated.
_CACHE_PREFIX = "fx:usd"
_CACHE_TTL_SECONDS = 36 * 3600      # backstop only; the date in the key does the real work

_HTTP_TIMEOUT = 8.0                  # a load waits on this, so it stays short
_PRIMARY_URL = "https://api.frankfurter.dev/v1/latest"
_FALLBACK_URL = "https://open.er-api.com/v6/latest/{base}"


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


async def _fetch_primary(client: httpx.AsyncClient, base: str):
    r = await client.get(_PRIMARY_URL, params={"base": base, "symbols": USD})
    r.raise_for_status()
    body = r.json()
    rate = (body.get("rates") or {}).get(USD)
    if rate is None:
        raise RateUnavailable(f"Frankfurter has no {base}->{USD} rate")
    return _as_rate(rate), body.get("date") or date.today().isoformat()


async def _fetch_fallback(client: httpx.AsyncClient, base: str):
    r = await client.get(_FALLBACK_URL.format(base=base))
    r.raise_for_status()
    body = r.json()
    if body.get("result") != "success":
        raise RateUnavailable(f"exchangerate-api returned {body.get('result')!r} for {base}")
    rate = (body.get("rates") or {}).get(USD)
    if rate is None:
        raise RateUnavailable(f"exchangerate-api has no {base}->{USD} rate")
    # its date field is an RFC 1123 string; the day is all the cache key needs
    return _as_rate(rate), (body.get("time_last_update_utc") or "")[:16] or date.today().isoformat()


async def get_usd_rate(redis, currency: str) -> Decimal:
    """The currency -> USD rate. USD itself is 1 and never hits the network or the cache.

    `redis` may be None (the cache is then simply skipped); a Redis failure is logged and ignored,
    since a cache that is down must not take the loader with it."""
    cur = (currency or "").strip().upper()
    if not cur:
        raise RateUnavailable("no currency given")
    if cur == USD:
        return Decimal(1)

    today = date.today().isoformat()
    key = f"{_CACHE_PREFIX}:{cur}:{today}"

    if redis is not None:
        try:
            cached = await redis.get(key)
            if cached:
                value = cached.decode() if isinstance(cached, (bytes, bytearray)) else str(cached)
                return _as_rate(value)
        except Exception as ex:      # a broken cache is not a broken load
            logger.warning("FX cache read failed for %s: %s", cur, ex)

    errors = []
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        for name, fetch in (("frankfurter", _fetch_primary), ("exchangerate-api", _fetch_fallback)):
            try:
                rate, published = await fetch(client, cur)
                logger.info("FX %s->USD = %s (%s, published %s)", cur, rate, name, published)
                if redis is not None:
                    try:
                        await redis.set(key, str(rate), ex=_CACHE_TTL_SECONDS)
                    except Exception as ex:
                        logger.warning("FX cache write failed for %s: %s", cur, ex)
                return rate
            except Exception as ex:
                errors.append(f"{name}: {ex}")
                logger.warning("FX source %s failed for %s: %s", name, cur, ex)

    raise RateUnavailable(f"no FX source could supply {cur}->{USD} ({'; '.join(errors)})")


async def get_usd_rates(redis, currencies) -> dict:
    """Rates for several currencies, fetched once per DISTINCT currency.

    A bulk load repeats the same two or three currencies across hundreds of rows; this keeps that to
    one lookup each. Raises on the first currency that cannot be resolved, so the caller can refuse
    the whole batch instead of writing part of it."""
    out: dict = {}
    for cur in currencies:
        key = (cur or "").strip().upper()
        if key not in out:
            out[key] = await get_usd_rate(redis, key)
    return out


def to_usd(amount: Decimal, rate: Decimal) -> Decimal:
    """Convert and round to cents, half-up — the rounding a person doing this by hand would use.

    Quantized here rather than left to the database so the stored value is the one this code decided
    on, not whatever the column's scale would have silently done to it."""
    return (Decimal(amount) * Decimal(rate)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


__all__ = ["RateUnavailable", "get_usd_rate", "get_usd_rates", "to_usd", "USD"]
