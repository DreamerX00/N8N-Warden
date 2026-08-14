"""Project lifecycle and membership."""

from __future__ import annotations

import json

from ..db import Db, nanoid, now_ts
from ..errors import Fatal
from ..journal import Batch
from ..model import roles
from .credentials import transfer_credential
from .workflows import transfer_workflow


def create_project(db: Db, batch: Batch, name: str, description: str = "",
                   icon: dict | None = None) -> str:
    project_id, ts = nanoid(), now_ts()
    batch.insert("project", {
        "id": project_id, "name": name, "type": "team",
        "createdAt": ts, "updatedAt": ts,
        "icon": json.dumps(icon) if icon else None,
        "description": description or None, "creatorId": None,
        "customTelemetryTags": "[]"})
    batch.note(f"created team project {name!r} ({project_id})")
    return project_id


def rename_project(db: Db, batch: Batch, project_id: str, name: str) -> None:
    batch.update("project", {"id": project_id}, {"name": name, "updatedAt": now_ts()})
    batch.note(f"renamed project to {name!r}")


def delete_project(db: Db, batch: Batch, project_id: str,
                   reassign_to: str | None) -> None:
    """Delete a team project, moving anything it owns somewhere safe first."""
    project = db.one("SELECT * FROM project WHERE id=?", project_id)
    if not project:
        raise Fatal("no such project")
    if project["type"] == "personal":
        raise Fatal("refusing to delete a personal project — delete the user instead")

    owned_workflows = db.q('SELECT "workflowId" FROM shared_workflow '
                           "WHERE \"projectId\"=? AND role='workflow:owner'", project_id)
    owned_credentials = db.q('SELECT "credentialsId" FROM shared_credentials '
                             "WHERE \"projectId\"=? AND role='credential:owner'",
                             project_id)
    if (owned_workflows or owned_credentials) and not reassign_to:
        raise Fatal(f"project owns {len(owned_workflows)} workflow(s) and "
                    f"{len(owned_credentials)} credential(s); "
                    "choose a project to reassign them to")

    for row in owned_workflows:
        transfer_workflow(db, batch, row["workflowId"], reassign_to)
    for row in owned_credentials:
        transfer_credential(db, batch, row["credentialsId"], reassign_to)

    _purge_project_rows(db, batch, project_id)
    batch.delete("project", {"id": project_id})
    batch.note(f"deleted project {project['name']!r}")


def _purge_project_rows(db: Db, batch: Batch, project_id: str) -> None:
    """Remove everything hanging off a project. Explicit rather than relying on
    ON DELETE CASCADE, so each removal lands in the undo journal."""
    for folder in db.q('SELECT id FROM folder WHERE "projectId"=?', project_id):
        batch.delete("folder", {"id": folder["id"]})
    for row in db.q('SELECT "workflowId" FROM shared_workflow WHERE "projectId"=?',
                    project_id):
        batch.delete("shared_workflow",
                     {"workflowId": row["workflowId"], "projectId": project_id})
    for row in db.q('SELECT "credentialsId" FROM shared_credentials '
                    'WHERE "projectId"=?', project_id):
        batch.delete("shared_credentials",
                     {"credentialsId": row["credentialsId"], "projectId": project_id})
    for row in db.q('SELECT "userId" FROM project_relation WHERE "projectId"=?',
                    project_id):
        batch.delete("project_relation",
                     {"projectId": project_id, "userId": row["userId"]})


def add_member(db: Db, batch: Batch, project_id: str, user_id: str, role: str) -> None:
    if role not in roles(db, "project"):
        raise Fatal(f"invalid project role {role!r}")
    if role == "project:personalOwner":
        raise Fatal("project:personalOwner is reserved for personal projects")

    ts = now_ts()
    existing = db.one('SELECT * FROM project_relation '
                      'WHERE "projectId"=? AND "userId"=?', project_id, user_id)
    if existing:
        batch.update("project_relation", {"projectId": project_id, "userId": user_id},
                     {"role": role, "updatedAt": ts})
        batch.note(f"role changed to {role}")
    else:
        batch.insert("project_relation", {
            "projectId": project_id, "userId": user_id, "role": role,
            "createdAt": ts, "updatedAt": ts})
        batch.note(f"added member with role {role}")


def remove_member(db: Db, batch: Batch, project_id: str, user_id: str) -> None:
    project = db.one("SELECT type FROM project WHERE id=?", project_id)
    relation = db.one('SELECT role FROM project_relation '
                      'WHERE "projectId"=? AND "userId"=?', project_id, user_id)
    if not relation:
        raise Fatal("that user is not a member")
    if project and project["type"] == "personal":
        raise Fatal("cannot remove the owner of a personal project")
    batch.delete("project_relation", {"projectId": project_id, "userId": user_id})
    batch.note("removed member")
