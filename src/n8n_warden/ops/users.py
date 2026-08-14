"""User accounts, global roles, and credentials-of-last-resort.

Creating a user also creates their personal project and the relation binding
them to it, because n8n treats those three rows as one indivisible fact.
"""

from __future__ import annotations

import uuid

from ..db import Db, nanoid, now_ts
from ..errors import Fatal
from ..journal import Batch
from ..model import roles
from ..queries import personal_project
from .credentials import transfer_credential
from .projects import _purge_project_rows
from .workflows import transfer_workflow

try:
    import bcrypt as _bcrypt
except ImportError:            # optional: only needed to set a password
    _bcrypt = None


def bcrypt_available() -> bool:
    return _bcrypt is not None


def create_user(db: Db, batch: Batch, email: str, first: str = "", last: str = "",
                global_role: str = "global:member") -> str:
    if global_role not in roles(db, "global"):
        raise Fatal(f"invalid global role {global_role!r}")
    if db.one('SELECT 1 FROM "user" WHERE email=?', email.lower()):
        raise Fatal("a user with that email already exists")

    user_id, ts = str(uuid.uuid4()), now_ts()
    batch.insert("user", {
        "id": user_id, "email": email.lower(), "firstName": first or None,
        "lastName": last or None, "password": None, "personalizationAnswers": None,
        "createdAt": ts, "updatedAt": ts, "settings": None, "disabled": 0,
        "mfaEnabled": 0, "mfaSecret": None, "mfaRecoveryCodes": None,
        "lastActiveAt": None, "roleSlug": global_role})

    project_id = nanoid()
    label = f"{first} {last}".strip() or email
    batch.insert("project", {
        "id": project_id, "name": f"{label} <{email.lower()}>", "type": "personal",
        "createdAt": ts, "updatedAt": ts, "icon": None, "description": None,
        "creatorId": user_id, "customTelemetryTags": "[]"})
    batch.insert("project_relation", {
        "projectId": project_id, "userId": user_id, "role": "project:personalOwner",
        "createdAt": ts, "updatedAt": ts})

    batch.note(f"created user {email} with personal project {project_id}")
    batch.note("password is unset — the user must be invited from "
               "Settings → Users, or given a password with `set-password`")
    return user_id


def delete_user(db: Db, batch: Batch, user_id: str, reassign_to: str | None) -> None:
    user = db.one('SELECT * FROM "user" WHERE id=?', user_id)
    if not user:
        raise Fatal("no such user")
    if user["roleSlug"] == "global:owner":
        raise Fatal("refusing to delete the instance owner")

    project = personal_project(db, user_id)
    if project:
        owned_workflows = db.q('SELECT "workflowId" FROM shared_workflow '
                               "WHERE \"projectId\"=? AND role='workflow:owner'",
                               project["id"])
        owned_credentials = db.q('SELECT "credentialsId" FROM shared_credentials '
                                 "WHERE \"projectId\"=? AND role='credential:owner'",
                                 project["id"])
        if (owned_workflows or owned_credentials) and not reassign_to:
            raise Fatal(f"user owns {len(owned_workflows)} workflow(s) and "
                        f"{len(owned_credentials)} credential(s); "
                        "choose a project to reassign them to")
        for row in owned_workflows:
            transfer_workflow(db, batch, row["workflowId"], reassign_to)
        for row in owned_credentials:
            transfer_credential(db, batch, row["credentialsId"], reassign_to)
        _purge_project_rows(db, batch, project["id"])

    for relation in db.q('SELECT "projectId" FROM project_relation WHERE "userId"=?',
                         user_id):
        batch.delete("project_relation",
                     {"projectId": relation["projectId"], "userId": user_id})
    if project:
        batch.delete("project", {"id": project["id"]})
    batch.delete("user", {"id": user_id})
    batch.note(f"deleted user {user['email']}")


def set_global_role(db: Db, batch: Batch, user_id: str, role: str) -> None:
    if role not in roles(db, "global"):
        raise Fatal(f"invalid global role {role!r}")
    user = db.one('SELECT * FROM "user" WHERE id=?', user_id)
    if not user:
        raise Fatal("no such user")

    if user["roleSlug"] == "global:owner" and role != "global:owner":
        others = db.q('SELECT id FROM "user" WHERE "roleSlug"=? AND id<>?',
                      "global:owner", user_id)
        if not others:
            raise Fatal("that is the only instance owner — promote someone else first")

    batch.update("user", {"id": user_id}, {"roleSlug": role, "updatedAt": now_ts()})
    batch.note(f"global role set to {role}")


def set_disabled(db: Db, batch: Batch, user_id: str, disabled: bool) -> None:
    batch.update("user", {"id": user_id},
                 {"disabled": 1 if disabled else 0, "updatedAt": now_ts()})
    batch.note("user " + ("disabled" if disabled else "enabled"))


def clear_mfa(db: Db, batch: Batch, user_id: str) -> None:
    batch.update("user", {"id": user_id},
                 {"mfaEnabled": 0, "mfaSecret": None, "mfaRecoveryCodes": None,
                  "updatedAt": now_ts()})
    batch.note("two-factor authentication cleared")


def set_password(db: Db, batch: Batch, user_id: str, plaintext: str | None) -> None:
    """None clears the password, which forces n8n's own invite/reset flow."""
    if plaintext is None:
        batch.update("user", {"id": user_id},
                     {"password": None, "updatedAt": now_ts()})
        batch.note("password cleared — user must be re-invited from Settings → Users")
        return

    if _bcrypt is None:
        raise Fatal("setting a password needs bcrypt (pip install bcrypt), "
                    "or clear it instead and use n8n's invite flow")
    if len(plaintext) < 8:
        raise Fatal("n8n requires at least 8 characters")

    hashed = _bcrypt.hashpw(plaintext.encode(), _bcrypt.gensalt(10)).decode()
    batch.update("user", {"id": user_id}, {"password": hashed, "updatedAt": now_ts()})
    batch.note("password set")
