"""Operational CLI.

    python -m finguard.cli bootstrap                       # sync roles/permissions catalog (idempotent)
    python -m finguard.cli create-org acme "Acme Analytics Pvt Ltd" --admin-email admin@acme.test
    python -m finguard.cli create-user acme alice@acme.test "Alice" FINANCE_ANALYST   # prompts for password
    DEMO_PASSWORD='...' python -m finguard.cli seed-demo    # demo orgs, one user per role, scenario data
    python -m finguard.cli verify-audit
"""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys

from sqlalchemy import select

from finguard.core.db import session_scope


def bootstrap(db) -> None:
    from finguard.authz.permissions import PERMISSIONS, ROLE_DESCRIPTIONS, ROLE_PERMISSIONS, SERVICE_ROLES
    from finguard.models import Permission, Role, RolePermission

    perms = {p.code: p for p in db.execute(select(Permission)).scalars()}
    for code, desc in PERMISSIONS.items():
        if code not in perms:
            perms[code] = Permission(code=code, description=desc)
            db.add(perms[code])
    db.flush()
    for name, codes in ROLE_PERMISSIONS.items():
        role = db.execute(select(Role).where(Role.name == name)).scalar_one_or_none()
        if role is None:
            role = Role(name=name, description=ROLE_DESCRIPTIONS[name], is_service_role=name in SERVICE_ROLES)
            db.add(role)
            db.flush()
        have = {rp.permission.code for rp in role.permissions}
        for code in codes - have:
            db.add(RolePermission(role_id=role.id, permission_id=perms[code].id))
        for rp in list(role.permissions):
            if rp.permission.code not in codes:
                db.delete(rp)
    db.flush()


def create_org(db, slug: str, name: str, admin_email: str | None, admin_password: str | None, settings: dict | None = None):
    from finguard.core.security import hash_password
    from finguard.governance.service import bootstrap_policy
    from finguard.models import Organization, Role, User, UserRole
    from finguard.models.enums import RoleName
    from finguard.services.common import ensure_service_identities

    org = db.execute(select(Organization).where(Organization.slug == slug)).scalar_one_or_none()
    if org:
        return org
    org = Organization(slug=slug, name=name, settings=settings or {"business_day_rule": "FOLLOWING", "execution_hour": 9,
                                                                   "holidays": [], "date_day_first": True})
    db.add(org)
    db.flush()
    ensure_service_identities(db, org)
    creator = db.execute(select(User).where(User.org_id == org.id, User.kind == "SERVICE")).scalars().first()
    if admin_email:
        admin = User(org_id=org.id, email=admin_email.lower(), full_name="System Administrator",
                     password_hash=hash_password(admin_password))
        db.add(admin)
        db.flush()
        db.add(UserRole(user_id=admin.id, role_id=db.execute(select(Role).where(Role.name == RoleName.SYSTEM_ADMIN)).scalar_one().id))
        creator = admin
    bootstrap_policy(db, org.id, creator.id)
    db.flush()
    return org


def create_user(db, org, email: str, name: str, password: str, roles: list[str], company: str | None = None):
    from finguard.core.security import hash_password
    from finguard.models import Role, User, UserRole

    u = db.execute(select(User).where(User.org_id == org.id, User.email == email)).scalar_one_or_none()
    if u:
        return u
    u = User(org_id=org.id, email=email, full_name=name, password_hash=hash_password(password), company_name=company)
    db.add(u)
    db.flush()
    for r in roles:
        db.add(UserRole(user_id=u.id, role_id=db.execute(select(Role).where(Role.name == r)).scalar_one().id))
    db.flush()
    return u


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="finguard")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("bootstrap")
    o = sub.add_parser("create-org")
    o.add_argument("slug"), o.add_argument("name"), o.add_argument("--admin-email")
    u = sub.add_parser("create-user")
    u.add_argument("org"), u.add_argument("email"), u.add_argument("name"), u.add_argument("roles", nargs="+")
    s = sub.add_parser("seed-demo")
    s.add_argument("--skip-scheduler", action="store_true")
    sub.add_parser("verify-audit")
    args = ap.parse_args(argv)

    if args.cmd == "bootstrap":
        with session_scope() as db:
            bootstrap(db)
        print("roles and permissions synchronized")
    elif args.cmd == "create-org":
        pw = getpass.getpass("Admin password: ") if args.admin_email else None
        with session_scope() as db:
            bootstrap(db)
            org = create_org(db, args.slug, args.name, args.admin_email, pw)
            print(f"organization {org.slug} ready")
    elif args.cmd == "create-user":
        from finguard.core.config import get_settings
        from finguard.core.security import validate_password_policy
        from finguard.models import Organization

        pw = getpass.getpass("Password: ")
        problems = validate_password_policy(pw, get_settings().password_min_length)
        if problems:
            sys.exit("password " + "; ".join(problems))
        with session_scope() as db:
            org = db.execute(select(Organization).where(Organization.slug == args.org)).scalar_one()
            create_user(db, org, args.email.lower(), args.name, pw, args.roles)
        print("user created")
    elif args.cmd == "seed-demo":
        from finguard.core.config import get_settings
        from finguard.seed import seed_demo

        if get_settings().environment == "production":
            sys.exit("refusing to seed demo data in production")
        pw = os.environ.get("DEMO_PASSWORD")
        generated = False
        if not pw:
            pw = "Demo-" + secrets.token_urlsafe(9) + "!7"
            generated = True
        seed_demo(pw, run_scheduler=not args.skip_scheduler)
        if generated:
            print(f"\nGenerated demo password (shown once): {pw}")
    elif args.cmd == "verify-audit":
        from finguard.services.audit import AuditService

        with session_scope() as db:
            print(AuditService(db).verify_chain())


if __name__ == "__main__":
    main()
