"""Certificate wording moves off the policy: company defaults in settings, overrides per certificate

The wording a certificate quotes — period wording, geographical limits and the clauses (LSW 555D,
AVN 48B and its exception, AVN 52E, AVS103A) — is the broker's, not the policy's. It now resolves as:
the certificate's own override (its `request`) → certificate.settings (the company default, edited in
the portal's admin) → the market-standard text.

1. certificate.settings gains the seven columns, NOT NULL, defaulting to the market text; the one
   existing row takes those defaults. `war_exclusion_exception = ''` means "no exception".

2. policy.policy keeps its seven columns for one release but nothing maps or reads them any more;
   they are dropped in a later revision. A policy whose wording differs from the market text is NOT
   copied into settings — that value belonged to one policy, not to the company. Such policies are
   listed in this migration's log so the wording can be set on their certificates by hand.

Revision ID: certificate_wording
Revises: certificates_lifecycle
Create Date: 2026-09-30
"""
import logging

from alembic import op
from sqlalchemy import text

revision = "certificate_wording"
down_revision = "certificates_lifecycle"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

_GEO = ("Worldwide excluding Ukraine and the region of Crimea, Iran, North Korea and Syria. However, "
        "coverage is granted (a) for the overflight of any excluded country where the flight is within "
        "an internationally recognised air corridor and is performed in accordance with I.C.A.O. "
        "recommendations; or (b) in circumstances where an insured Aircraft has landed in an excluded "
        "country as a direct consequence and exclusively as a result of force majeure. However "
        "Worldwide in respect of Products Legal Liability")
# column, type, market text, comment
_WORDING = (
    ("period_wording", "text", "both days inclusive, local standard time at the address of the Insured",
     "How the policy period is qualified after its two dates."),
    ("geographical_limits", "text", _GEO, "The Geographical Limits paragraph."),
    ("hull_war_clause", "varchar", "LSW 555D",
     "The hull war and allied perils wording the cover is in accordance with."),
    ("war_exclusion_clause", "varchar", "AVN 48B",
     "The war and allied perils exclusion the liability cover writes back."),
    ("war_exclusion_exception", "varchar", "sub-paragraph(s) (b) of AVN48B",
     "What of the exclusion is NOT written back; empty = no exception."),
    ("war_liability_clause", "varchar", "AVN 52E", "The extended coverage endorsement for war liability."),
    ("fifty_fifty_clause", "varchar", "AVS103A", "The 50/50 provisional claims settlement clause."),
)


def _q(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def upgrade() -> None:
    for column, kind, market, comment in _WORDING:
        op.execute(f"ALTER TABLE certificate.settings ADD COLUMN {column} {kind} NOT NULL "
                   f"DEFAULT {_q(market)}")
        op.execute(f"COMMENT ON COLUMN certificate.settings.{column} IS "
                   f"{_q(comment + ' Company default; a certificate may override it.')}")
        op.execute(f"COMMENT ON COLUMN policy.policy.{column} IS 'DEPRECATED, not read: certificate "
                   f"wording lives in certificate.settings and on the certificate (revision "
                   f"certificate_wording). To be dropped.'")

    # IS DISTINCT FROM also catches a NULL war_exclusion_exception ("no exception" on the policy)
    differs = " OR ".join(f"{column} IS DISTINCT FROM {_q(market)}"
                          for column, _kind, market, _comment in _WORDING)
    rows = op.get_bind().execute(text(
        f"SELECT id, {', '.join(c for c, *_ in _WORDING)} FROM policy.policy WHERE {differs} "
        f"ORDER BY id")).mappings().all()
    if not rows:
        log.info("certificate_wording: every policy uses the market wording; nothing to review")
    for row in rows:
        changed = {c: row[c] for c, _k, market, _m in _WORDING if row[c] != market}
        log.warning("certificate_wording: policy %s has non-market wording, NOT carried over — set it "
                    "on its certificates if still needed: %s", row["id"], changed)


def downgrade() -> None:
    for column, *_ in _WORDING:
        op.execute(f"ALTER TABLE certificate.settings DROP COLUMN {column}")
        op.execute(f"COMMENT ON COLUMN policy.policy.{column} IS NULL")
