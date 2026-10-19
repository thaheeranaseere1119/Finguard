"""Integrity controls: append-only audit log, immutable approved contract versions and extraction records,
full-text search index for evidence retrieval.

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Audit log: append-only. UPDATE / DELETE / TRUNCATE raise.
    op.execute("""
    CREATE OR REPLACE FUNCTION fg_block_mutation() RETURNS trigger AS $$
    BEGIN
      RAISE EXCEPTION 'table % is append-only', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
    END $$ LANGUAGE plpgsql;
    """)
    for table in ("audit_logs", "extracted_fields", "field_corrections", "risk_assessments", "governance_decisions",
                  "approval_decisions", "contract_validations", "contract_approvals"):
        op.execute(f"CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE OR DELETE ON {table} "
                   f"FOR EACH ROW EXECUTE FUNCTION fg_block_mutation();")
        op.execute(f"CREATE TRIGGER trg_{table}_no_truncate BEFORE TRUNCATE ON {table} "
                   f"FOR EACH STATEMENT EXECUTE FUNCTION fg_block_mutation();")

    # Extraction results: immutable except nothing (rows are written once).
    op.execute("CREATE TRIGGER trg_extraction_results_no_update BEFORE UPDATE OR DELETE ON extraction_results "
               "FOR EACH ROW EXECUTE FUNCTION fg_block_mutation();")

    # Approved contract versions: terms are frozen. The only allowed change is status APPROVED → SUPERSEDED.
    op.execute("""
    CREATE OR REPLACE FUNCTION fg_protect_approved_version() RETURNS trigger AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN
        IF OLD.status IN ('APPROVED', 'SUPERSEDED') THEN
          RAISE EXCEPTION 'approved contract versions cannot be deleted' USING ERRCODE = 'insufficient_privilege';
        END IF;
        RETURN OLD;
      END IF;
      IF OLD.status IN ('APPROVED', 'SUPERSEDED') THEN
        IF NOT (OLD.status = 'APPROVED' AND NEW.status = 'SUPERSEDED')
           OR (to_jsonb(NEW) - 'status') IS DISTINCT FROM (to_jsonb(OLD) - 'status') THEN
          RAISE EXCEPTION 'approved contract version % is immutable', OLD.id USING ERRCODE = 'insufficient_privilege';
        END IF;
      END IF;
      RETURN NEW;
    END $$ LANGUAGE plpgsql;
    """)
    op.execute("CREATE TRIGGER trg_contract_versions_immutable BEFORE UPDATE OR DELETE ON contract_versions "
               "FOR EACH ROW EXECUTE FUNCTION fg_protect_approved_version();")

    # Completed payouts cannot be rewritten (amount/currency/idempotency are frozen once created).
    op.execute("""
    CREATE OR REPLACE FUNCTION fg_protect_attempt() RETURNS trigger AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'payment attempts cannot be deleted' USING ERRCODE = 'insufficient_privilege';
      END IF;
      IF NEW.amount <> OLD.amount OR NEW.currency <> OLD.currency OR NEW.idempotency_key <> OLD.idempotency_key
         OR NEW.scheduled_payment_id <> OLD.scheduled_payment_id OR NEW.fund_account_id <> OLD.fund_account_id THEN
        RAISE EXCEPTION 'payment attempt core fields are immutable' USING ERRCODE = 'insufficient_privilege';
      END IF;
      IF OLD.status IN ('FAILED', 'CANCELLED', 'REVERSED') AND NEW.status <> OLD.status THEN
        RAISE EXCEPTION 'final payment attempt status cannot change' USING ERRCODE = 'insufficient_privilege';
      END IF;
      RETURN NEW;
    END $$ LANGUAGE plpgsql;
    """)
    op.execute("CREATE TRIGGER trg_payment_attempts_protect BEFORE UPDATE OR DELETE ON payment_attempts "
               "FOR EACH ROW EXECUTE FUNCTION fg_protect_attempt();")

    # Full-text search over extracted document text (evidence retrieval).
    op.execute("CREATE INDEX ix_extraction_fulltext ON extraction_results USING gin (to_tsvector('english', full_text));")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_extraction_fulltext;")
    op.execute("DROP TRIGGER IF EXISTS trg_payment_attempts_protect ON payment_attempts;")
    op.execute("DROP TRIGGER IF EXISTS trg_contract_versions_immutable ON contract_versions;")
    op.execute("DROP TRIGGER IF EXISTS trg_extraction_results_no_update ON extraction_results;")
    for table in ("audit_logs", "extracted_fields", "field_corrections", "risk_assessments", "governance_decisions",
                  "approval_decisions", "contract_validations", "contract_approvals"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update ON {table};")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_truncate ON {table};")
    op.execute("DROP FUNCTION IF EXISTS fg_protect_attempt();")
    op.execute("DROP FUNCTION IF EXISTS fg_protect_approved_version();")
    op.execute("DROP FUNCTION IF EXISTS fg_block_mutation();")
