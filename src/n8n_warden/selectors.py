"""Selector expressions for bulk operations.

    wf:*                    all workflows
    wf:active               active only
    wf:archived             archived only
    wf:orphan               owned by no project
    wf:name~regex           name matches
    wf:tag=prod             carries a tag
    wf:project=<id|name>    owned by a project
    wf:owner=<email>        owned by that user's personal project

    cred:*  cred:orphan  cred:name~re  cred:type=slackApi  cred:project=…
"""

from __future__ import annotations

import re

from .db import Db
from .errors import Fatal
from .queries import credentials, personal_project, workflows


def _regex(pattern: str) -> re.Pattern:
    try:
        return re.compile(pattern, re.I)
    except re.error as e:
        raise Fatal(f"bad regex {pattern!r}: {e}")


def resolve(db: Db, expr: str) -> tuple[str, list[dict]]:
    """Returns (kind, matching rows). Kind is 'workflow' or 'credential'."""
    if ":" not in expr:
        raise Fatal(f"bad selector {expr!r} — expected kind:filter")
    kind, rest = (part.strip() for part in expr.split(":", 1))
    kind = kind.lower()

    if kind in ("wf", "workflow"):
        return "workflow", _workflows(db, rest)
    if kind in ("cred", "credential"):
        return "credential", _credentials(db, rest)
    raise Fatal(f"unknown selector kind {kind!r} (use wf: or cred:)")


def _workflows(db: Db, rest: str) -> list[dict]:
    rows = workflows(db)
    if rest in ("*", "all"):
        return rows
    if rest == "active":
        return [r for r in rows if r["active"]]
    if rest == "archived":
        return [r for r in rows if r["isArchived"]]
    if rest == "orphan":
        return [r for r in rows if not r["projectId"]]
    if rest.startswith("name~"):
        pattern = _regex(rest[5:])
        return [r for r in rows if pattern.search(r["name"] or "")]
    if rest.startswith("tag="):
        tagged = {r["workflowId"] for r in db.q(
            '''SELECT wt."workflowId" FROM workflows_tags wt
               JOIN tag_entity t ON t.id=wt."tagId" WHERE t.name=?''', rest[4:])}
        return [r for r in rows if r["id"] in tagged]
    if rest.startswith("project="):
        key = rest[8:]
        return [r for r in rows if key in (r["projectId"], r["project"])]
    if rest.startswith("owner="):
        email = rest[6:].lower()
        user = db.one('SELECT id FROM "user" WHERE lower(email)=?', email)
        if not user:
            raise Fatal(f"no user {email}")
        project = personal_project(db, user["id"])
        return [r for r in rows if project and r["projectId"] == project["id"]]
    raise Fatal(f"unknown workflow filter {rest!r}")


def _credentials(db: Db, rest: str) -> list[dict]:
    rows = credentials(db)
    if rest in ("*", "all"):
        return rows
    if rest == "orphan":
        return [r for r in rows if not r["projectId"]]
    if rest.startswith("name~"):
        pattern = _regex(rest[5:])
        return [r for r in rows if pattern.search(r["name"] or "")]
    if rest.startswith("type="):
        return [r for r in rows if r["type"] == rest[5:]]
    if rest.startswith("project="):
        key = rest[8:]
        return [r for r in rows if key in (r["projectId"], r["project"])]
    raise Fatal(f"unknown credential filter {rest!r}")
