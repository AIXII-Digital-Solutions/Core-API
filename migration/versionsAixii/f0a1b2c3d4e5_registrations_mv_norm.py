"""cirium.registrations matview: + normalized column (separator-insensitive search) + operator index

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-07-02

Recreates cirium.registrations with `registration_norm` = upper(registration) with every non
[A-Z0-9] char stripped, so a search for "YLLTD" matches the stored "YL-LTD". Adds a btree index on
it (text_pattern_ops -> prefix LIKE) and an index on operator (the new "all tails of this operator"
search in GET /registrations). Owner reset to grp_aviation_write so the worker can still REFRESH it
(see e9f0a1b2c3d4).
"""
from alembic import op

revision = "f0a1b2c3d4e5"
down_revision = "e9f0a1b2c3d4"
branch_labels = None
depends_on = None

_MV_NEW = r"""
CREATE MATERIALIZED VIEW cirium.registrations AS
SELECT DISTINCT ON ("Registration")
    "Registration" AS registration,
    regexp_replace(upper("Registration"), '[^A-Z0-9]', '', 'g') AS registration_norm,
    "Operator"     AS operator,
    "Status"       AS status
FROM cirium.ciriumaircrafts
WHERE "Registration" IS NOT NULL
ORDER BY "Registration", revision_id DESC
WITH DATA
"""

_MV_OLD = r"""
CREATE MATERIALIZED VIEW cirium.registrations AS
SELECT DISTINCT ON ("Registration")
    "Registration" AS registration,
    "Operator"     AS operator,
    "Status"       AS status
FROM cirium.ciriumaircrafts
WHERE "Registration" IS NOT NULL
ORDER BY "Registration", revision_id DESC
WITH DATA
"""

_OWNER_FIX = r"""
DO $do$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grp_aviation_write') THEN
        EXECUTE 'ALTER MATERIALIZED VIEW cirium.registrations OWNER TO grp_aviation_write';
    END IF;
END $do$;
"""


def upgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS cirium.registrations")
    op.execute(_MV_NEW)
    op.execute('CREATE UNIQUE INDEX ix_cirium_registrations_registration ON cirium.registrations (registration)')
    op.execute('CREATE INDEX ix_cirium_registrations_norm ON cirium.registrations (registration_norm text_pattern_ops)')
    op.execute('CREATE INDEX ix_cirium_registrations_operator ON cirium.registrations (operator)')
    op.execute('CREATE INDEX ix_cirium_registrations_status ON cirium.registrations (status)')
    op.execute(_OWNER_FIX)


def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS cirium.registrations")
    op.execute(_MV_OLD)
    op.execute('CREATE UNIQUE INDEX ix_cirium_registrations_registration ON cirium.registrations (registration)')
    op.execute('CREATE INDEX ix_cirium_registrations_status ON cirium.registrations (status)')
    op.execute(_OWNER_FIX)
