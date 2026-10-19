# Security controls and scope

This prototype implements the controls below. It is **not** certified or verified as compliant with PCI DSS, SOC 2,
ISO 27001, RBI/SEBI guidance or any other framework. Formal compliance needs organization-specific legal, security and
audit work. No card data is processed; bank account numbers are passed once to the payout provider and are not stored.

| Area | Control | Where |
|---|---|---|
| Passwords | Argon2id (m=19 MiB, t=2), policy ≥12 chars & 3 classes, constant-work verification, lockout after 5 failures (15 min) | `core/security.py`, `services/auth.py` |
| Tokens | 15-min HS256 JWT access tokens (iss/aud/exp/nbf/jti, session-bound); opaque rotating refresh tokens (SHA-256 at rest), reuse detection revokes the session; 5-s concurrent-refresh grace | `services/auth.py` |
| CSRF | Refresh cookie HttpOnly + SameSite=Strict + path-scoped; refresh requires double-submit `X-CSRF-Token`; API calls use Bearer headers | `api/v1/auth.py` |
| CORS | Explicit allowlist (`CORS_ORIGINS`) | `main.py` |
| Authorization | Deny-by-default RBAC + ABAC in services (tenant scope ⇒ 404, ownership, SoD, approval limits, dual approval); failed checks audited | `authz/`, services |
| Admin separation | SYSTEM_ADMIN has no financial permissions and cannot self-grant financial roles; contractor roles cannot be combined with internal roles; service roles cannot be assigned | `services/auth.py` |
| AI agent | Separate machine identity; allowlisted tools only; tools run with the delegating user's permissions; plan built before retrieval; no amounts from chat; injection detection; rate limit; audited tool calls | `agent/` |
| Prompt injection | Document text scanned (override, role markers, approval bypass, exfiltration, policy tamper, base64/zero-width obfuscation); detection forces ESCALATE; text is never executed as instructions | `extraction/injection.py` |
| Uploads | Magic-byte type detection that must match the extension, size/page limits, encrypted-PDF rejection, zip-bomb guard for DOCX, image decompression limits, sanitized names, random storage keys, path-traversal guard, private storage, SHA-256 integrity re-check before payment | `documents/storage.py` |
| SQL injection | SQLAlchemy parameterized queries only (one `text()` query with bound parameters) | — |
| Output encoding | React escaping; explicit JSON serializers; no HTML rendering of document text | — |
| Payments | Sandbox default; live mode gated by environment + acknowledgement; server-derived amounts; idempotency keys; durable intent before provider calls; unknown outcomes reconciled before retry; webhook HMAC + event-id dedupe + amount/currency/reference check | `payments/` |
| Integrity | Append-only tables (DB triggers), immutable approved versions, hash-chained audit log with verification endpoint | `migrations/versions/0002_*` |
| Logging | Structured JSON logs with request IDs; secrets, JWTs, keys and long digit runs (account numbers) redacted in logs and audit payloads | `core/logging.py`, `core/redact.py` |
| Rate limits | Login (per IP and per account), registration, uploads, agent, webhooks (Redis-backed, in-process fallback) | `core/ratelimit.py` |
| Errors | Consistent error envelope without stack traces; unhandled errors return a generic message with the request ID | `main.py` |
| Headers | nosniff, X-Frame-Options DENY, Referrer-Policy, CSP (nginx), no-store on API responses | `main.py`, `frontend/nginx.conf` |
| Containers | Non-root, read-only root FS, no-new-privileges, all capabilities dropped, API not published directly | `docker-compose.yml` |

## Remaining work before any production use

* TLS termination, HSTS, a secret manager (Vault or a cloud KMS) instead of `.env`, key rotation procedures.
* Database role separation, so that the application role lacks `ALTER TABLE` / `DISABLE TRIGGER` / `TRUNCATE`
  privileges (the triggers protect against application-level tampering, not a superuser).
* Malware scanning of uploads (e.g. ClamAV) and a PDF sanitizer for hostile files.
* External anchoring of the audit hash chain (WORM storage) and periodic verification.
* MFA for privileged roles; SSO/SAML integration.
* Dependency scanning (pip-audit, npm audit) in CI; penetration testing.
* Data retention and privacy policy (DPDP Act / GDPR) and backup encryption.

## Backup and restore

```bash
docker compose exec db pg_dump -U finguard -Fc finguard > finguard-$(date +%F).dump        # backup
docker compose exec -T db pg_restore -U finguard -d finguard --clean < finguard-YYYY-MM-DD.dump   # restore
docker run --rm -v fyp_documents:/data -v "$PWD":/backup alpine tar czf /backup/documents.tgz /data   # documents volume
```

Restore the database and the documents volume from the same point in time. After restoring, run
`python -m finguard.cli verify-audit` to confirm the hash chain is intact.
