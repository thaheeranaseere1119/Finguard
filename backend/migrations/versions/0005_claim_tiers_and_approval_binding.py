"""Claim tiers and approval binding: payee fingerprint on approval requests.

Revision ID: 0005
Revises: 0004
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("approval_requests", sa.Column("payee_ref", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("approval_requests", "payee_ref")
