"""Read-only reporting: who can reach what, and what has come loose."""

from __future__ import annotations

import json
from pathlib import Path

from .db import Db, now_ts
from .model import check_invariants
from .queries import credentials, folders, projects, users, workflows


def access_matrix(db: Db) -> list[dict]:
    """Effective reach per user, resolved through project membership."""
    matrix = []
    for user in users(db):
        relations = db.q('''SELECT pr."projectId", pr.role, p.name, p.type
                            FROM project_relation pr
                            JOIN project p ON p.id=pr."projectId"
                            WHERE pr."userId"=?''', user["id"])
        project_ids = [r["projectId"] for r in relations]

        if user["roleSlug"] in ("global:owner", "global:admin"):
            workflow_count = db.one("SELECT COUNT(*) AS n FROM workflow_entity")["n"]
            credential_count = db.one("SELECT COUNT(*) AS n FROM credentials_entity")["n"]
            scope = f"ALL (instance {user['roleSlug'].split(':')[1]})"
        elif project_ids:
            marks = ",".join("?" for _ in project_ids)
            workflow_count = db.one(
                f'SELECT COUNT(DISTINCT "workflowId") AS n FROM shared_workflow '
                f'WHERE "projectId" IN ({marks})', *project_ids)["n"]
            credential_count = db.one(
                f'SELECT COUNT(DISTINCT "credentialsId") AS n FROM shared_credentials '
                f'WHERE "projectId" IN ({marks})', *project_ids)["n"]
            scope = ", ".join(f"{r['name']}({r['role'].split(':')[1]})"
                              for r in relations)
        else:
            workflow_count = credential_count = 0
            scope = "—"

        matrix.append({"email": user["email"], "global": user["roleSlug"],
                       "workflows": workflow_count, "credentials": credential_count,
                       "projects": scope})
    return matrix


def orphans(db: Db) -> dict:
    """Things that exist but nobody can reach, or that never finished setup."""
    return {
        "workflows_unshared": [dict(r) for r in db.q('''
            SELECT w.id, w.name FROM workflow_entity w
            LEFT JOIN shared_workflow s ON s."workflowId"=w.id
            WHERE s."workflowId" IS NULL''')],
        "credentials_unshared": [dict(r) for r in db.q('''
            SELECT c.id, c.name, c.type FROM credentials_entity c
            LEFT JOIN shared_credentials s ON s."credentialsId"=c.id
            WHERE s."credentialsId" IS NULL''')],
        "projects_memberless": [dict(r) for r in db.q('''
            SELECT p.id, p.name, p.type FROM project p
            LEFT JOIN project_relation pr ON pr."projectId"=p.id
            WHERE pr."projectId" IS NULL''')],
        "users_pending": [dict(r) for r in db.q(
            'SELECT id, email FROM "user" WHERE password IS NULL')],
    }


def export_access(db: Db, path: Path) -> Path:
    payload = {
        "exported_at": now_ts(),
        "projects": projects(db),
        "users": users(db),
        "workflows": workflows(db),
        "credentials": credentials(db),
        "folders": folders(db),
        "matrix": access_matrix(db),
        "orphans": orphans(db),
        "invariant_issues": [{"kind": i.kind, "message": i.message}
                             for i in check_invariants(db)],
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path
