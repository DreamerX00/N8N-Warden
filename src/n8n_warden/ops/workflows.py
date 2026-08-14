"""Workflow ownership, sharing, and the folder-policy question.

The folder policy is the subtle part. A folder belongs to exactly one project,
so a workflow's `parentFolderId` cannot survive a move between projects. Left
alone it becomes a dangling cross-project reference: nothing errors, the
workflow simply renders in a folder the new owner cannot see.
"""

from __future__ import annotations

from ..config import DEFAULT_FOLDER_POLICY
from ..console import yellow
from ..db import Db, nanoid, now_ts
from ..errors import Fatal
from ..journal import Batch
from ..model import roles
from ..queries import folder_path, missing_credentials


def _resolve_folder_on_move(db: Db, batch: Batch, workflow: dict,
                            dest_project: str, policy: str) -> None:
    """Apply the chosen answer to the folder problem. See module docstring."""
    folder_id = workflow["parentFolderId"]
    if not folder_id:
        return

    if policy == "keep":
        # Internal, used when the folder is being moved in the same batch.
        # Verified rather than trusted: silently keeping a reference into
        # another project is precisely the corruption this tool detects.
        folder = db.one('SELECT "projectId" AS p FROM folder WHERE id=?', folder_id)
        if not folder or folder["p"] != dest_project:
            raise Fatal(f"folder {folder_id} is not owned by the destination "
                        "project — 'keep' is only valid when the folder moves too")
        return

    if policy == "block":
        raise Fatal("workflow is in a folder; move it out first "
                    "(or use --folder-policy root|mirror)")

    if policy == "root":
        batch.update("workflow_entity", {"id": workflow["id"]},
                     {"parentFolderId": None, "updatedAt": now_ts()})
        batch.note("folder reference cleared (workflow lands at project root)")
        return

    if policy == "mirror":
        parent = None
        for node in folder_path(db, folder_id):
            match = db.one('SELECT id FROM folder WHERE "projectId"=? AND name=? '
                           'AND ("parentFolderId" IS ? OR "parentFolderId"=?)',
                           dest_project, node["name"], parent, parent)
            if match:
                parent = match["id"]
            else:
                new_id, ts = nanoid(), now_ts()
                batch.insert("folder", {
                    "id": new_id, "name": node["name"], "parentFolderId": parent,
                    "projectId": dest_project, "createdAt": ts, "updatedAt": ts})
                parent = new_id
        batch.update("workflow_entity", {"id": workflow["id"]},
                     {"parentFolderId": parent, "updatedAt": now_ts()})
        batch.note("folder path mirrored into destination project")
        return

    raise Fatal(f"unknown folder policy {policy!r}")


def transfer_workflow(db: Db, batch: Batch, workflow_id: str, dest_project: str,
                      folder_policy: str = DEFAULT_FOLDER_POLICY) -> None:
    """Move ownership. Rewrites the owner row — never inserts a second one."""
    workflow = db.one("SELECT * FROM workflow_entity WHERE id=?", workflow_id)
    if not workflow:
        raise Fatal(f"no workflow {workflow_id}")
    if not db.one("SELECT 1 FROM project WHERE id=?", dest_project):
        raise Fatal(f"no project {dest_project}")

    owner = db.one("SELECT * FROM shared_workflow "
                   "WHERE \"workflowId\"=? AND role='workflow:owner'", workflow_id)
    if owner and owner["projectId"] == dest_project:
        batch.note(f"{workflow['name']!r} already owned by that project")
        return

    # The destination may already hold a non-owner share; its primary key
    # would collide with the row we are about to write.
    if db.one('SELECT 1 FROM shared_workflow WHERE "workflowId"=? AND "projectId"=?',
              workflow_id, dest_project):
        batch.delete("shared_workflow",
                     {"workflowId": workflow_id, "projectId": dest_project})

    ts = now_ts()
    if owner:
        batch.delete("shared_workflow",
                     {"workflowId": workflow_id, "projectId": owner["projectId"]})
    batch.insert("shared_workflow", {
        "workflowId": workflow_id, "projectId": dest_project,
        "role": "workflow:owner",
        "createdAt": owner["createdAt"] if owner else ts, "updatedAt": ts})

    _resolve_folder_on_move(db, batch, workflow, dest_project, folder_policy)
    batch.update("workflow_entity", {"id": workflow_id}, {"updatedAt": ts})

    gaps = missing_credentials(db, workflow_id, dest_project)
    if gaps:
        batch.note(yellow(f"{len(gaps)} credential(s) not available to the "
                          "destination: " + ", ".join(g["name"] for g in gaps)))
    batch.note(f"transferred {workflow['name']!r}")


def share_workflow(db: Db, batch: Batch, workflow_id: str, project_id: str,
                   role: str = "workflow:editor") -> None:
    if role not in roles(db, "workflow"):
        raise Fatal(f"invalid workflow role {role!r}")
    if role == "workflow:owner":
        raise Fatal("use transfer to change the owner")

    ts = now_ts()
    existing = db.one('SELECT role FROM shared_workflow '
                      'WHERE "workflowId"=? AND "projectId"=?', workflow_id, project_id)
    if existing and existing["role"] == "workflow:owner":
        raise Fatal("that project already owns this workflow")

    if existing:
        batch.update("shared_workflow",
                     {"workflowId": workflow_id, "projectId": project_id},
                     {"role": role, "updatedAt": ts})
    else:
        batch.insert("shared_workflow", {
            "workflowId": workflow_id, "projectId": project_id, "role": role,
            "createdAt": ts, "updatedAt": ts})
    batch.note(f"shared with project as {role}")


def unshare_workflow(db: Db, batch: Batch, workflow_id: str, project_id: str) -> None:
    existing = db.one('SELECT role FROM shared_workflow '
                      'WHERE "workflowId"=? AND "projectId"=?', workflow_id, project_id)
    if not existing:
        raise Fatal("not shared with that project")
    if existing["role"] == "workflow:owner":
        raise Fatal("cannot unshare the owner — transfer it instead")
    batch.delete("shared_workflow",
                 {"workflowId": workflow_id, "projectId": project_id})
    batch.note("unshared")


def move_workflow_to_folder(db: Db, batch: Batch, workflow_id: str,
                            folder_id: str | None) -> None:
    workflow = db.one("SELECT * FROM workflow_entity WHERE id=?", workflow_id)
    if not workflow:
        raise Fatal("no such workflow")

    owner = db.one('SELECT "projectId" FROM shared_workflow '
                   "WHERE \"workflowId\"=? AND role='workflow:owner'", workflow_id)
    if folder_id:
        folder = db.one("SELECT * FROM folder WHERE id=?", folder_id)
        if not folder:
            raise Fatal("no such folder")
        if owner and folder["projectId"] != owner["projectId"]:
            raise Fatal("that folder belongs to a different project than "
                        "the workflow's owner")

    batch.update("workflow_entity", {"id": workflow_id},
                 {"parentFolderId": folder_id, "updatedAt": now_ts()})
    batch.note("moved workflow")
