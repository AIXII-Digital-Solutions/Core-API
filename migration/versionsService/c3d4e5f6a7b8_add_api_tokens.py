"""add api_tokens (gateway API-key auth)

Creates the api_tokens table backing the X-Api-Key scoped credentials that core-api issues
to external callers via the /tokens admin router.

Revision ID: c3d4e5f6a7b8
Revises: b7e1c2d3a4f5
Create Date: 2026-06-27 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, Sequence[str], None] = 'b7e1c2d3a4f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'api_tokens',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('token_prefix', sa.String(length=32), nullable=False),
        sa.Column('token_hash', sa.String(length=128), nullable=False),
        sa.Column('scopes', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_by', sa.String(length=128), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
    )
    op.create_index('ix_api_tokens_token_prefix', 'api_tokens', ['token_prefix'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_api_tokens_token_prefix', table_name='api_tokens')
    op.drop_table('api_tokens')
