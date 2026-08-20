"""Credential ownership and sharing."""

from __future__ import annotations

from ..db import Db, now_ts
from ..errors import Fatal
from ..journal import Batch
from ..model import roles


def transfer_credential(db: Db, batch: Batch, cred_id: str, dest_project: str) -> None:
    """Move ownership. Rewrites the owner row rather than adding a second."""
    cred = db.one("SELECT * FROM credentials_entity WHERE id=?", cred_id)
    if not cred:
        raise Fatal(f"no credential {cred_id}")

    owner = db.one('SELECT * FROM shared_credentials '
                   "WHERE \"credentialsId\"=? AND role='credential:owner'", cred_id)
    if owner and owner["projectId"] == dest_project:
        return

    # The destination may already hold a non-owner share; its primary key
    # would collide with the row we are about to write.
    if db.one('SELECT 1 FROM shared_credentials '
              'WHERE "credentialsId"=? AND "projectId"=?', cred_id, dest_project):
        batch.delete("shared_credentials",
                     {"credentialsId": cred_id, "projectId": dest_project})

    ts = now_ts()
    if owner:
        batch.delete("shared_credentials",
                     {"credentialsId": cred_id, "projectId": owner["projectId"]})
    batch.insert("shared_credentials", {
        "credentialsId": cred_id, "projectId": dest_project,
        "role": "credential:owner",
        "createdAt": owner["createdAt"] if owner else ts, "updatedAt": ts})
    batch.update("credentials_entity", {"id": cred_id}, {"updatedAt": ts})
    batch.note(f"transferred credential {cred['name']!r}")


def share_credential(db: Db, batch: Batch, cred_id: str, project_id: str,
                     role: str = "credential:user") -> None:
    if role not in roles(db, "credential"):
        raise Fatal(f"invalid credential role {role!r}")
    if role == "credential:owner":
        raise Fatal("use transfer to change the owner")

    # Checked here rather than left to the FK so a typo'd id fails with a
    # message, not a traceback — and the name makes bulk notes readable.
    cred = db.one("SELECT name FROM credentials_entity WHERE id=?", cred_id)
    if not cred:
        raise Fatal(f"no credential {cred_id!r}")

    ts = now_ts()
    existing = db.one('SELECT role FROM shared_credentials '
                      'WHERE "credentialsId"=? AND "projectId"=?', cred_id, project_id)
    if existing and existing["role"] == "credential:owner":
        raise Fatal("that project already owns this credential")

    if existing:
        batch.update("shared_credentials",
                     {"credentialsId": cred_id, "projectId": project_id},
                     {"role": role, "updatedAt": ts})
    else:
        batch.insert("shared_credentials", {
            "credentialsId": cred_id, "projectId": project_id, "role": role,
            "createdAt": ts, "updatedAt": ts})
    batch.note(f"shared credential {cred['name']!r} as {role}")


def unshare_credential(db: Db, batch: Batch, cred_id: str, project_id: str) -> None:
    cred = db.one("SELECT name FROM credentials_entity WHERE id=?", cred_id)
    if not cred:
        raise Fatal(f"no credential {cred_id!r}")
    existing = db.one('SELECT role FROM shared_credentials '
                      'WHERE "credentialsId"=? AND "projectId"=?', cred_id, project_id)
    if not existing:
        raise Fatal("not shared with that project")
    if existing["role"] == "credential:owner":
        raise Fatal("cannot unshare the owner — transfer it instead")
    batch.delete("shared_credentials",
                 {"credentialsId": cred_id, "projectId": project_id})
    batch.note(f"unshared credential {cred['name']!r}")
