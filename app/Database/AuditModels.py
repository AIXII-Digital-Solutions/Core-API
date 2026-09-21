"""The change log — schema `audit` in the aixii database.

ONE table for every audited table in the insured-aircraft domain, written by ONE trigger function,
`audit.log_change()`. Every table in `ref`, `fleet`, `leasing` and `policy`, plus `api.airlines`,
carries an `AFTER INSERT OR UPDATE OR DELETE … FOR EACH ROW` trigger that calls it.

WHY ONE TABLE AND NOT A HISTORY TABLE PER SUBJECT. The previous design gave each audited table its
own `*_history` twin, which meant every new table needed a new table, a new trigger function and a
new endpoint, and "what happened to this aircraft last week" was a UNION over all of them. Here it
is one query with a WHERE. The cost is that the log is not typed — which is the right trade, since
the payload is whole-row JSONB either way.

WHOLE-ROW JSONB, not a per-column diff. A snapshot is self-contained and survives a later schema
change; a stored diff stops making sense the moment a column is renamed. The diff is the READ
layer's job: `old_row` and `new_row` are compared when the history is served, with foreign keys
resolved to names. `to_jsonb(OLD)` also means a column added tomorrow is audited without touching
anything.

NO FOREIGN KEY on `row_id`, deliberately: the audit row must outlive the row it describes, so a
DELETE leaves its own pre-image behind and the history of a deleted object still reads.

`changed_by` is the `app.actor` GUC when the API sets it (`set_config('app.actor', …, true)` per
transaction), falling back to the database login for anything done outside the API.

Alembic reads THIS file (db-contract); `app/Database/AuditModels.py` is core-api's runtime copy.
The trigger function and the triggers themselves are raw SQL in the migration — autogenerate
cannot see them, which is also what stops it from proposing to drop them.
"""
import inspect
import sys
from datetime import datetime
from typing import Optional

from sqlalchemy import String, BigInteger, DateTime, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .config import AuditBase as Base


class ChangeLog(Base):
    """One row per INSERT / UPDATE / DELETE on an audited table.

    An UPDATE that moves nothing but `updated_at` is NOT logged — the trigger compares the two
    snapshots with that key removed first, so a no-op save from the portal does not fill the log
    with rows that say nothing.
    """
    __tablename__ = "change_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    schema_name: Mapped[str] = mapped_column(String, nullable=False)
    table_name: Mapped[str] = mapped_column(String, nullable=False)
    row_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True, default=None)
    operation: Mapped[str] = mapped_column(String(10), nullable=False)   # INSERT | UPDATE | DELETE
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    changed_by: Mapped[Optional[str]] = mapped_column(String, nullable=True, default=None)
    old_row: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, default=None)
    new_row: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True, default=None)

    __table_args__ = (
        # the history of one object, newest first — the only access path the portal needs
        Index("ix_change_log_subject", "schema_name", "table_name", "row_id",
              "changed_at", postgresql_using="btree"),
        Index("ix_change_log_changed_at", "changed_at"),
        Index("ix_change_log_changed_by", "changed_by"),
    )


_current_module = sys.modules[__name__]

__all__ = [
    name
    for name, obj in globals().items()
    if inspect.isclass(obj) and obj.__module__ == __name__
]
