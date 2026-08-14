"""The rules n8n's own code assumes but its schema does not enforce.

This module is the reason the tool is safer than a hand-typed UPDATE. Roles
are read from the live `role` table rather than hardcoded, and every write is
checked against a set of invariants before it is allowed to commit.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from .config import (EXPECTED_COLUMNS, VERIFIED_MIGRATION_TS, VERIFIED_N8N)
from .db import Db

# Fallback for n8n < 2.0, which had no `role` table.
_LEGACY_ROLES = {
    "project": ["project:admin", "project:editor", "project:viewer",
                "project:personalOwner"],
    "workflow": ["workflow:owner", "workflow:editor"],
    "credential": ["credential:owner", "credential:user"],
    "global": ["global:owner", "global:admin", "global:member"],
}


def roles(db: Db, kind: str) -> list[str]:
    """Valid role slugs, read from the live `role` table — never hardcoded.

    This matters: `shared_workflow.role` has no foreign key, so a typo like
    'workflow:Owner' inserts silently and produces a workflow nobody can open.
    """
    try:
        return [r["slug"] for r in db.q(
            'SELECT slug FROM role WHERE "roleType"=? ORDER BY slug', kind)]
    except sqlite3.OperationalError:
        return _LEGACY_ROLES.get(kind, [])


# --- version compatibility ----------------------------------------------

def migration_state(db: Db) -> tuple[str | None, int | None, int]:
    """(latest migration name, its timestamp, total applied), from n8n's table."""
    try:
        row = db.one("SELECT name FROM migrations ORDER BY id DESC LIMIT 1")
        total = db.one("SELECT COUNT(*) AS n FROM migrations")["n"]
    except Exception:
        return None, None, 0
    if not row:
        return None, None, total
    name = row["name"]
    found = re.search(r"(\d{10,})$", name)
    return name, (int(found.group(1)) if found else None), total


def compat_note(db: Db) -> str | None:
    """None when this n8n is within the range the tool has been verified on."""
    name, ts, _ = migration_state(db)
    if name is None:
        return "cannot read n8n's `migrations` table — version compatibility unknown"
    if ts is None:
        return f"unrecognised migration name {name!r} — compatibility unknown"
    if ts > VERIFIED_MIGRATION_TS:
        return (f"this n8n has run migrations newer than the tool was verified "
                f"against (verified up to n8n {VERIFIED_N8N}); latest here is {name}")
    return None


def schema_check(db: Db) -> list[str]:
    """Fast fingerprint of the tables we write to."""
    problems = []
    present = db.tables()
    for table, expected in EXPECTED_COLUMNS.items():
        if table not in present:
            problems.append(f"missing table {table!r}")
            continue
        missing = expected - db.columns(table)
        if missing:
            problems.append(f"{table}: missing columns {sorted(missing)}")
    return problems


# --- invariants ----------------------------------------------------------

@dataclass(frozen=True)
class Issue:
    """One broken rule.

    `kind` is a stable slug so `doctor --fix <kind>` can select it, and
    `target` carries whatever the repair needs to act. Frozen so that the
    write cycle can compare before/after sets by equality.
    """

    kind: str
    message: str
    target: tuple = ()

    def __str__(self) -> str:
        return self.message


def check_invariants(db: Db) -> list[Issue]:
    """Every rule that must hold for n8n to behave sanely."""
    return [issue for check in (
        _single_owner, _no_orphan_shares, _known_roles,
        _personal_project_owners, _folders_match_project,
    ) for issue in check(db)]


def _single_owner(db: Db) -> list[Issue]:
    issues = []
    for table, id_col, owner in (
            ("shared_workflow", "workflowId", "workflow:owner"),
            ("shared_credentials", "credentialsId", "credential:owner")):
        for r in db.q(f'''SELECT "{id_col}" AS id, COUNT(*) AS n FROM "{table}"
                          WHERE role=? GROUP BY "{id_col}" HAVING n<>1''', owner):
            issues.append(Issue(
                "dup-owner",
                f"{table}: {r['id']} has {r['n']} owner rows (must be exactly 1)",
                (table, id_col, r["id"], owner)))
    return issues


def _no_orphan_shares(db: Db) -> list[Issue]:
    issues = []
    for table, entity, col in (
            ("shared_workflow", "workflow_entity", "workflowId"),
            ("shared_credentials", "credentials_entity", "credentialsId")):
        for r in db.q(f'''SELECT e.id FROM "{entity}" e
                          LEFT JOIN "{table}" s ON s."{col}"=e.id
                          WHERE s."{col}" IS NULL'''):
            issues.append(Issue(
                "orphan-share",
                f"{entity}: {r['id']} is shared with no project "
                "(invisible to everyone)",
                (table, col, r["id"])))
    return issues


def _known_roles(db: Db) -> list[Issue]:
    """Neither shared_* role column has a foreign key, so we police them here."""
    issues = []
    for table, kind in (("shared_workflow", "workflow"),
                        ("shared_credentials", "credential")):
        valid = set(roles(db, kind))
        for row in db.q(f"SELECT DISTINCT role FROM {table}"):
            if row["role"] not in valid:
                issues.append(Issue(
                    "unknown-role",
                    f"{table}: unknown role {row['role']!r} "
                    "(no FK protects this column)",
                    (table, row["role"])))
    return issues


def _personal_project_owners(db: Db) -> list[Issue]:
    rows = db.q('''SELECT p.id, p.name, COUNT(pr."userId") AS n
                   FROM project p
                   LEFT JOIN project_relation pr
                     ON pr."projectId"=p.id AND pr.role='project:personalOwner'
                   WHERE p.type='personal' GROUP BY p.id HAVING n<>1''')
    return [Issue("personal-owner-count",
                  f"project: personal project {r['name']!r} has {r['n']} owners "
                  "(must be 1)",
                  (r["id"],))
            for r in rows]


def _folders_match_project(db: Db) -> list[Issue]:
    """The failure a naive UPDATE produces: a workflow sitting in a folder
    that belongs to a different project than its owner. Nothing errors — the
    workflow just renders in a folder the new owner cannot see."""
    issues = []
    for r in db.q('''SELECT w.id, w.name, f."projectId" AS fp, s."projectId" AS sp
                     FROM workflow_entity w
                     JOIN folder f ON f.id = w."parentFolderId"
                     JOIN shared_workflow s
                       ON s."workflowId"=w.id AND s.role='workflow:owner'
                     WHERE f."projectId" <> s."projectId"'''):
        issues.append(Issue(
            "cross-project-folder",
            f"workflow {r['name']!r}: folder belongs to project {r['fp']} "
            f"but workflow is owned by {r['sp']} (dangling cross-project folder)",
            (r["id"],)))
    for r in db.q('''SELECT c.id, c.name FROM folder c JOIN folder p
                     ON p.id=c."parentFolderId" WHERE p."projectId"<>c."projectId"'''):
        issues.append(Issue(
            "folder-parent-project",
            f"folder {r['name']!r}: parent folder is in a different project",
            (r["id"],)))
    return issues
