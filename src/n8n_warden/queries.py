"""Read-only views over n8n's data.

Pure functions returning plain dicts, so they can feed a text table, a JSON
export, or an assertion in the self-test without change.
"""

from __future__ import annotations

import json

from .db import Db


def projects(db: Db) -> list[dict]:
    return [dict(r) for r in db.q('''
        SELECT p.id, p.name, p.type,
               (SELECT COUNT(*) FROM project_relation pr
                 WHERE pr."projectId"=p.id) AS members,
               (SELECT COUNT(*) FROM shared_workflow s
                 WHERE s."projectId"=p.id AND s.role='workflow:owner') AS workflows,
               (SELECT COUNT(*) FROM shared_credentials s
                 WHERE s."projectId"=p.id AND s.role='credential:owner') AS credentials
        FROM project p ORDER BY p.type DESC, p.name''')]


def users(db: Db) -> list[dict]:
    return [dict(r) for r in db.q('''
        SELECT u.id, u.email, u."firstName", u."lastName", u."roleSlug", u.disabled,
               (u.password IS NULL) AS pending
        FROM "user" u ORDER BY u."roleSlug", u.email''')]


def workflows(db: Db) -> list[dict]:
    return [dict(r) for r in db.q('''
        SELECT w.id, w.name, w.active, w."isArchived", w."parentFolderId",
               p.name AS project, p.id AS "projectId", f.name AS folder,
               (SELECT COUNT(*) FROM shared_workflow s2
                 WHERE s2."workflowId"=w.id) AS shares
        FROM workflow_entity w
        LEFT JOIN shared_workflow s
          ON s."workflowId"=w.id AND s.role='workflow:owner'
        LEFT JOIN project p ON p.id=s."projectId"
        LEFT JOIN folder f ON f.id=w."parentFolderId"
        ORDER BY w.name''')]


def credentials(db: Db) -> list[dict]:
    return [dict(r) for r in db.q('''
        SELECT c.id, c.name, c.type, p.name AS project, p.id AS "projectId",
               (SELECT COUNT(*) FROM shared_credentials s2
                 WHERE s2."credentialsId"=c.id) AS shares
        FROM credentials_entity c
        LEFT JOIN shared_credentials s
          ON s."credentialsId"=c.id AND s.role='credential:owner'
        LEFT JOIN project p ON p.id=s."projectId"
        ORDER BY c.name''')]


def folders(db: Db, project_id: str | None = None) -> list[dict]:
    sql = '''SELECT f.id, f.name, f."parentFolderId", f."projectId", p.name AS project
             FROM folder f JOIN project p ON p.id=f."projectId"'''
    if project_id:
        return [dict(r) for r in db.q(sql + ' WHERE f."projectId"=? ORDER BY f.name',
                                      project_id)]
    return [dict(r) for r in db.q(sql + " ORDER BY p.name, f.name")]


def folder_path(db: Db, folder_id: str) -> list[dict]:
    """Root-first chain of folders leading to `folder_id`.

    Guards against a cycle in `parentFolderId` — the schema permits one, and an
    unguarded walk would hang the CLI rather than report the problem.
    """
    chain, seen = [], set()
    current = folder_id
    while current and current not in seen:
        seen.add(current)
        row = db.one('SELECT id, name, "parentFolderId", "projectId" '
                     "FROM folder WHERE id=?", current)
        if not row:
            break
        chain.append(dict(row))
        current = row["parentFolderId"]
    return list(reversed(chain))


def folder_descendants(db: Db, folder_id: str, include_self: bool = True) -> list[str]:
    """Folder ids in the subtree rooted at `folder_id`, self first."""
    rows = db.q('''WITH RECURSIVE tree(id) AS (
                       SELECT ?
                       UNION
                       SELECT f.id FROM folder f JOIN tree t ON f."parentFolderId"=t.id
                   ) SELECT id FROM tree''', folder_id)
    ids = [r["id"] for r in rows]
    return ids if include_self else [i for i in ids if i != folder_id]


def folder_workflows(db: Db, folder_id: str, recursive: bool = True) -> list[dict]:
    """Workflows filed under a folder.

    Recursive by default: a folder holding one workflow directly may hold many
    more in subfolders, and sharing only the direct children is almost never
    what someone means by "share this folder".
    """
    scope = folder_descendants(db, folder_id) if recursive else [folder_id]
    marks = ",".join("?" for _ in scope)
    return [dict(r) for r in db.q(
        f'''SELECT w.id, w.name, w."parentFolderId", f.name AS folder
            FROM workflow_entity w
            LEFT JOIN folder f ON f.id = w."parentFolderId"
            WHERE w."parentFolderId" IN ({marks})
            ORDER BY w.name''', *scope)]


def personal_project(db: Db, user_id: str) -> dict | None:
    row = db.one('''SELECT p.* FROM project p
                    JOIN project_relation pr ON pr."projectId"=p.id
                    WHERE pr."userId"=? AND p.type='personal' ''', user_id)
    return dict(row) if row else None


def workflow_credential_ids(db: Db, workflow_id: str) -> set[str]:
    """Credential ids referenced by the workflow's nodes."""
    row = db.one("SELECT nodes FROM workflow_entity WHERE id=?", workflow_id)
    if not row or not row["nodes"]:
        return set()
    try:
        nodes = json.loads(row["nodes"])
    except (json.JSONDecodeError, TypeError):
        return set()
    found = set()
    for node in nodes if isinstance(nodes, list) else []:
        for cred in (node.get("credentials") or {}).values():
            if isinstance(cred, dict) and cred.get("id"):
                found.add(str(cred["id"]))
    return found


def missing_credentials(db: Db, workflow_id: str, dest_project: str) -> list[dict]:
    """Credentials the workflow needs that the destination project cannot see.

    Catching this before a transfer is the difference between a clean move and
    a workflow whose nodes fail at the next run.
    """
    gaps = []
    for cred_id in workflow_credential_ids(db, workflow_id):
        cred = db.one("SELECT id, name FROM credentials_entity WHERE id=?", cred_id)
        if not cred:
            continue
        visible = db.one('SELECT 1 FROM shared_credentials '
                         'WHERE "credentialsId"=? AND "projectId"=?',
                         cred_id, dest_project)
        if not visible:
            gaps.append(dict(cred))
    return gaps
