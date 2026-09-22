"""fleet.aircraft_type.template_url becomes {airborne, on_the_ground}

An aircraft is damaged in two states and the portal overlays the damage on a different outline for
each: gear up in the air, gear down and doors open on the stand. One URL could only ever describe
one of them, so the column becomes JSONB holding both.

    template_url  text  ->  jsonb   {"airborne": "https://...", "on_the_ground": null}

Still URLs into the image store, never bytes — that part does not change, and the reason has not
either: a grid reads a hundred types and must not drag a hundred images through the connection.

THE SHAPE IS ENFORCED, not merely documented. `ck_aircraft_type_template_url` demands an object
carrying exactly those two keys, each a string or null, and forbids the object where both are null
— that state is `template_url IS NULL`. So "nothing recorded" has ONE representation and no reader
has to test for two. The check uses `->` rather than `?&` deliberately: a missing key yields SQL
NULL while a key holding JSON null does not, which is exactly the distinction being made.

Nothing is converted in practice — all 1355 rows are NULL, because the drawings are not in Cirium
and nobody has uploaded any. The USING clause still handles a value rather than dropping it: a
stray text URL would be the airborne view, that being the one an outline drawing usually is.

Revision ID: template_url_jsonb
Revises: aircraft_type_category
Create Date: 2026-09-23
"""
from alembic import op

revision = "template_url_jsonb"
down_revision = "aircraft_type_category"
branch_labels = None
depends_on = None

_CHECK = "ck_aircraft_type_template_url"

# Kept in step with Database.FleetModels.TEMPLATE_URL_CHECK. Written out rather than imported
# because a revision must keep working after the model moves on.
_CHECK_SQL = (
    "template_url IS NULL OR ("
    "jsonb_typeof(template_url) = 'object'"
    " AND template_url -> 'airborne' IS NOT NULL"
    " AND template_url -> 'on_the_ground' IS NOT NULL"
    " AND template_url - 'airborne' - 'on_the_ground' = '{}'::jsonb"
    " AND jsonb_typeof(template_url -> 'airborne') IN ('string', 'null')"
    " AND jsonb_typeof(template_url -> 'on_the_ground') IN ('string', 'null')"
    " AND (template_url ->> 'airborne' IS NOT NULL"
    "      OR template_url ->> 'on_the_ground' IS NOT NULL))"
)


def upgrade() -> None:
    op.execute(
        "ALTER TABLE fleet.aircraft_type "
        "ALTER COLUMN template_url TYPE jsonb USING "
        "  CASE WHEN template_url IS NULL THEN NULL "
        "       ELSE jsonb_build_object('airborne', template_url, 'on_the_ground', NULL) END"
    )
    op.execute(f"ALTER TABLE fleet.aircraft_type ADD CONSTRAINT {_CHECK} CHECK ({_CHECK_SQL})")
    op.execute(
        "COMMENT ON COLUMN fleet.aircraft_type.template_url IS "
        "'The outline drawings the portal overlays damage on, as {airborne, on_the_ground}. "
        "URLs into the image store, never bytes. NULL means no drawing is recorded; an object "
        "always carries both keys and at least one non-null URL.'"
    )


def downgrade() -> None:
    # Going back to one text column keeps the airborne URL and LOSES the ground one — there is
    # nowhere to put it. Nothing is lost today (every row is NULL); if that stops being true,
    # read the ground URLs out before downgrading.
    op.execute(f"ALTER TABLE fleet.aircraft_type DROP CONSTRAINT {_CHECK}")
    op.execute(
        "ALTER TABLE fleet.aircraft_type "
        "ALTER COLUMN template_url TYPE text USING template_url ->> 'airborne'"
    )
    op.execute(
        "COMMENT ON COLUMN fleet.aircraft_type.template_url IS "
        "'URL of the outline drawing in the platform image store - a link, not bytes.'"
    )
