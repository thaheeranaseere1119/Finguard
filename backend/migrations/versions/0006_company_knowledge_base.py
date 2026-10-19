"""Company knowledge base for retrieval-augmented answering: documents and chunks (full-text + embeddings).

Revision ID: 0006
Revises: 0005
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kb_documents",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("doc_key", sa.String(80), nullable=False),
        sa.Column("doc_type", sa.String(32), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("period_start", sa.Date),
        sa.Column("period_end", sa.Date),
        sa.Column("entities", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("org_id", "doc_key", name="uq_kb_doc_key"),
    )
    op.create_index("ix_kb_docs_org_type", "kb_documents", ["org_id", "doc_type"])
    op.create_table(
        "kb_chunks",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("org_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", sa.BigInteger, sa.ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chunk_no", sa.Integer, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("embedding", postgresql.ARRAY(sa.REAL)),
        sa.Column("tsv", postgresql.TSVECTOR, sa.Computed("to_tsvector('english', text)", persisted=True)),
    )
    op.create_index("ix_kb_chunks_org", "kb_chunks", ["org_id"])
    op.create_index("ix_kb_chunks_tsv", "kb_chunks", ["tsv"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_table("kb_chunks")
    op.drop_table("kb_documents")
