"""acys_snapshots: record WHICH PARAMETERS produced the run, so a same-day repeat can reuse it

A second request for the same operator on the same day rebuilds a dataset that already exists: the
same Cirium revision, the same FR24 history, the same model. external-worker now looks for that run
and pours it back instead (see `snapshots.find_reusable`), turning an hour into seconds.

"The same run" has to mean the same INPUTS, and the scope and the as-of date are only two of them —
the third is the model's tuning profile. Comparing profile NAMES is not enough: editing the default
profile's params and re-running is exactly how the model is tuned, and both runs name no profile at
all, so a name comparison would hand back the pre-tuning report and hide the change being tested.

`params_fingerprint` is a hash of the RESOLVED parameter values plus the model version, so it differs
whenever anything that could change the numbers changed. Nullable: a snapshot written before this
column existed has none, and NULL never equals a fingerprint, so those runs are simply never reused.

Revision ID: acys_snapshots_fingerprint
Revises: acys_claims_total_outstanding
Create Date: 2026-09-13
"""
from alembic import op

revision = "acys_snapshots_fingerprint"
down_revision = "acys_claims_total_outstanding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE forecast.acys_snapshots ADD COLUMN params_fingerprint text")


def downgrade() -> None:
    op.execute("ALTER TABLE forecast.acys_snapshots DROP COLUMN params_fingerprint")
