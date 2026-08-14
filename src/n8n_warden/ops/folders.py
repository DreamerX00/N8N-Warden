"""Folder operations.

A folder belongs to exactly one project. Every rule here follows from that.
"""

from __future__ import annotations

from ..db import Db, nanoid, now_ts
from ..errors import Fatal
from ..journal import Batch
from ..queries import folder_descendants, folder_workflows, workflow_credential_ids


def create_folder(db: Db, batch: Batch, name: str, project_id: str,
                  parent_id: str | None = None) -> str:
    if parent_id:
        parent = db.one("SELECT * FROM folder WHERE id=?", parent_id)
        if not parent:
            raise Fatal("no such parent folder")
        if parent["projectId"] != project_id:
            raise Fatal("parent folder belongs to a different project")

    folder_id = nanoid()
    ts = now_ts()
    batch.insert("folder", {
        "id": folder_id, "name": name, "parentFolderId": parent_id,
        "projectId": project_id, "createdAt": ts, "updatedAt": ts})
    batch.note(f"created folder {name!r}")
    return folder_id


def delete_folder(db: Db, batch: Batch, folder_id: str, recursive: bool = False) -> None:
    folder = db.one("SELECT * FROM folder WHERE id=?", folder_id)
    if not folder:
        raise Fatal("no such folder")

    children = db.q('SELECT id FROM folder WHERE "parentFolderId"=?', folder_id)
    contents = db.q('SELECT id FROM workflow_entity WHERE "parentFolderId"=?', folder_id)
    if (children or contents) and not recursive:
        raise Fatal(f"folder has {len(children)} subfolder(s) and "
                    f"{len(contents)} workflow(s); pass recursive to lift them "
                    "to the parent")

    parent = folder["parentFolderId"]
    for workflow in contents:
        batch.update("workflow_entity", {"id": workflow["id"]},
                     {"parentFolderId": parent, "updatedAt": now_ts()})
    for child in children:
        batch.update("folder", {"id": child["id"]},
                     {"parentFolderId": parent, "updatedAt": now_ts()})
    batch.delete("folder", {"id": folder_id})
    batch.note(f"deleted folder {folder['name']!r}")


def rename_folder(db: Db, batch: Batch, folder_id: str, name: str) -> None:
    batch.update("folder", {"id": folder_id}, {"name": name, "updatedAt": now_ts()})
    batch.note(f"renamed folder to {name!r}")


# --- folder-wide access --------------------------------------------------
#
# n8n has no concept of sharing a folder: `folder.projectId` is a single value
# and there is no shared_folder table. So "share this folder" means sharing
# every workflow inside it. The recipient gets the workflows; the folder itself
# stays with its owning project, and the workflows appear at the recipient's
# root because a folder is only visible inside the project that owns it.

def share_folder(db: Db, batch: Batch, folder_id: str, project_id: str,
                 role: str = "workflow:editor", recursive: bool = True,
                 with_credentials: bool = False) -> None:
    from .credentials import share_credential
    from .workflows import share_workflow

    folder = db.one("SELECT * FROM folder WHERE id=?", folder_id)
    if not folder:
        raise Fatal("no such folder")
    if folder["projectId"] == project_id:
        raise Fatal("that project already owns this folder")

    contents = folder_workflows(db, folder_id, recursive)
    if not contents:
        raise Fatal("folder contains no workflows")

    for workflow in contents:
        share_workflow(db, batch, workflow["id"], project_id, role)

    if with_credentials:
        needed: set[str] = set()
        for workflow in contents:
            needed |= workflow_credential_ids(db, workflow["id"])
        shared = 0
        for cred_id in sorted(needed):
            if not db.one("SELECT 1 FROM credentials_entity WHERE id=?", cred_id):
                continue
            existing = db.one('SELECT 1 FROM shared_credentials '
                              'WHERE "credentialsId"=? AND "projectId"=?',
                              cred_id, project_id)
            if existing:
                continue
            share_credential(db, batch, cred_id, project_id, "credential:user")
            shared += 1
        batch.note(f"shared {shared} credential(s) the workflows depend on")

    batch.note(f"shared {len(contents)} workflow(s) from {folder['name']!r} as {role}")


def unshare_folder(db: Db, batch: Batch, folder_id: str, project_id: str,
                   recursive: bool = True) -> None:
    from .workflows import unshare_workflow

    folder = db.one("SELECT * FROM folder WHERE id=?", folder_id)
    if not folder:
        raise Fatal("no such folder")

    removed = 0
    for workflow in folder_workflows(db, folder_id, recursive):
        existing = db.one('SELECT role FROM shared_workflow '
                          'WHERE "workflowId"=? AND "projectId"=?',
                          workflow["id"], project_id)
        if not existing or existing["role"] == "workflow:owner":
            continue          # never owned here, or owned — leave it alone
        unshare_workflow(db, batch, workflow["id"], project_id)
        removed += 1
    batch.note(f"revoked {removed} workflow share(s) from {folder['name']!r}")


def transfer_folder(db: Db, batch: Batch, folder_id: str, project_id: str) -> None:
    """Move the folder subtree and everything filed in it to another project.

    Folders move first so that when the workflows follow, their
    `parentFolderId` already points somewhere the destination owns — which is
    why this uses the internal 'keep' policy rather than clearing the folder.
    """
    from .workflows import transfer_workflow

    folder = db.one("SELECT * FROM folder WHERE id=?", folder_id)
    if not folder:
        raise Fatal("no such folder")
    if folder["projectId"] == project_id:
        raise Fatal("that project already owns this folder")
    if not db.one("SELECT 1 FROM project WHERE id=?", project_id):
        raise Fatal("no such destination project")

    subtree = folder_descendants(db, folder_id)
    contents = folder_workflows(db, folder_id, recursive=True)
    ts = now_ts()

    for index, sub_id in enumerate(subtree):
        changes = {"projectId": project_id, "updatedAt": ts}
        if index == 0:
            # The root of the moved subtree cannot keep a parent that stays
            # behind in the old project; it becomes a top-level folder.
            changes["parentFolderId"] = None
        batch.update("folder", {"id": sub_id}, changes)

    for workflow in contents:
        transfer_workflow(db, batch, workflow["id"], project_id, "keep")

    batch.note(f"moved folder {folder['name']!r} "
               f"({len(subtree)} folder(s), {len(contents)} workflow(s))")
