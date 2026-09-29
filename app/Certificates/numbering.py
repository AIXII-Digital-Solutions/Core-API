"""Certificate reference numbers: CY<yy>/<airline code>/<nnnnn>.

`nnnnn` comes from certificate.reference_counter. The counter's SCOPE is the certificate type
(`reinsurance`, `insurance`) or, in the shared mode, `shared` — settings.CERTIFICATE_COUNTER_MODE.

SWITCHING MODES NEVER REUSES A NUMBER. A scope's next value is one more than the highest value of
every scope it could collide with: the shared scope with all of them, a per-type scope with itself
and the shared one. So going to the shared counter continues after the highest number either type
has used, and coming back from it continues each type after whatever the shared counter reached.

ONE statement, row-locked by the upsert, so two certificates issued at the same instant cannot be
handed the same number.
"""
from datetime import date

from sqlalchemy import text

import settings

SHARED = "shared"
KINDS = ("reinsurance", "insurance")


def counter_scope(kind: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown certificate kind {kind!r}")
    return SHARED if settings.CERTIFICATE_COUNTER_MODE == "shared" else kind


def _related(scope: str) -> list[str]:
    return [SHARED, *KINDS] if scope == SHARED else [scope, SHARED]


_NEXT = text("""
INSERT INTO certificate.reference_counter AS c (scope, last_value)
SELECT CAST(:scope AS varchar), coalesce(max(r.last_value), 0) + 1
FROM certificate.reference_counter r
WHERE r.scope = ANY (CAST(:related AS varchar[]))
ON CONFLICT (scope) DO UPDATE
SET last_value = greatest(
        c.last_value,
        (SELECT coalesce(max(r.last_value), 0) FROM certificate.reference_counter r
         WHERE r.scope = ANY (CAST(:related AS varchar[])))) + 1,
    updated_at = now()
RETURNING last_value
""")


async def next_sequence(session, kind: str) -> tuple[str, int]:
    """(scope, sequence number) for a new certificate of `kind`, inside the caller's transaction —
    a certificate that fails to save gives its number back with the rollback."""
    scope = counter_scope(kind)
    value = (await session.execute(_NEXT, {"scope": scope, "related": _related(scope)})).scalar_one()
    return scope, value


def contract_year(period_from: date) -> int:
    return period_from.year


def reference_number(year: int, airline_code: str, sequence: int) -> str:
    """CY25/SCAT/00063."""
    return f"CY{year % 100:02d}/{airline_code}/{sequence:05d}"
