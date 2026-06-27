"""cleanup unused service tables + add schedule_registry

Drops the no-longer-used service-DB tables (pbirequestfrsummarydatas, pdf_queues,
documentembeddings, globalembeddings, fieldsynonyms) and creates schedule_registry —
the runtime scheduler control plane that core-api manages and the workers' dispatcher
reads.

Revision ID: b7e1c2d3a4f5
Revises: db2d65e08195
Create Date: 2026-06-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector


# revision identifiers, used by Alembic.
revision: str = 'b7e1c2d3a4f5'
down_revision: Union[str, Sequence[str], None] = 'db2d65e08195'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # --- new: schedule_registry (scheduler control plane) ---
    op.create_table(
        'schedule_registry',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('queue', sa.String(length=64), nullable=False),
        sa.Column('func_name', sa.String(length=128), nullable=False),
        sa.Column('kwargs', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('interval_seconds', sa.Integer(), nullable=True),
        sa.Column('cron_expr', sa.String(length=128), nullable=True),
        sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('paused', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('run_now', sa.Boolean(), server_default=sa.text('false'), nullable=False),
        sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_run_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_status', sa.String(length=32), nullable=True),
        sa.Column('description', sa.String(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_schedule_registry_name', 'schedule_registry', ['name'], unique=True)
    op.create_index('ix_schedule_registry_next_run_at', 'schedule_registry', ['next_run_at'], unique=False)

    # --- drop: tables no longer used by any service ---
    # IF EXISTS / CASCADE so the migration is robust to partially-applied environments.
    for _table in (
        'pbirequestfrsummarydatas',
        'pdf_queues',
        'documentembeddings',
        'globalembeddings',
        'fieldsynonyms',
    ):
        op.execute(f'DROP TABLE IF EXISTS {_table} CASCADE')


def downgrade() -> None:
    """Downgrade schema — recreate the dropped tables and remove schedule_registry."""
    op.create_table(
        'pbirequestfrsummarydatas',
        sa.Column('correlation_id', sa.UUID(), nullable=False),
        sa.Column('user', sa.String(), nullable=False),
        sa.Column('rows_fetched', sa.Integer(), nullable=True),
        sa.Column('current_date_from', sa.String(), nullable=True),
        sa.Column('current_date_to', sa.String(), nullable=True),
        sa.Column('current_regs', sa.String(), nullable=True),
        sa.Column('current_airlines', sa.String(), nullable=True),
        sa.Column('estimate_time', sa.Float(), nullable=True),
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('correlation_id'),
    )

    op.create_table(
        'pdf_queues',
        sa.Column('filename', sa.String(), nullable=False),
        sa.Column('type', sa.String(), nullable=False),
        sa.Column('queue_position', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('status_description', sa.String(), nullable=False),
        sa.Column('user_email', sa.String(), nullable=False),
        sa.Column('progress', sa.Float(), nullable=False),
        sa.Column('progress_total', sa.Integer(), nullable=False),
        sa.Column('progress_done', sa.Integer(), nullable=False),
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('filename'),
    )

    op.create_table(
        'documentembeddings',
        sa.Column('file_name', sa.String(), nullable=False),
        sa.Column('chunk_index', sa.Integer(), nullable=False),
        sa.Column('text', sa.String(), nullable=False),
        sa.Column('embedding', Vector(1024), nullable=False),
        sa.Column('meta_data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('file_name', 'chunk_index', name='uq_document_chunk'),
    )
    op.create_index('ix_document_file_name', 'documentembeddings', ['file_name'], unique=False)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_document_embedding_ivf "
        "ON documentembeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )

    op.create_table(
        'globalembeddings',
        sa.Column('text', sa.String(), nullable=False),
        sa.Column('text_hash', sa.UUID(), sa.Computed('md5(text)::uuid', persisted=True), nullable=False),
        sa.Column('embedding', Vector(1024), nullable=False),
        sa.Column('meta_data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('text_hash'),
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_global_embedding_ivf "
        "ON globalembeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )

    op.create_table(
        'fieldsynonyms',
        sa.Column('field_name', sa.String(length=255), nullable=False),
        sa.Column('synonym', sa.String(length=255), nullable=False),
        sa.Column('embedding', Vector(1024), nullable=False),
        sa.Column('created_source', sa.String(length=255), nullable=True),
        sa.Column('extra', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('field_name', 'synonym', name='uq_field_synonym'),
    )
    op.create_index('ix_field_synonym_field_name', 'fieldsynonyms', ['field_name'], unique=False)

    op.drop_index('ix_schedule_registry_next_run_at', table_name='schedule_registry')
    op.drop_index('ix_schedule_registry_name', table_name='schedule_registry')
    op.drop_table('schedule_registry')
