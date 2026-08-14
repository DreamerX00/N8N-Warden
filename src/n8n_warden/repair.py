"""Repairs for the invariants `doctor` detects.

One hand-written fixer per issue kind, selectable by slug — deliberately not a
generic repair engine. Some issues have no safe automatic answer and are left
report-only; a fixer that guesses is worse than a fixer that does not exist.

    dup-owner              keep the earliest owner, demote the rest to editor
    orphan-share           adopt into the instance owner's personal project
    cross-project-folder   clear the dangling folder reference
    folder-parent-project  detach the folder from its foreign-project parent

    unknown-role           REPORT ONLY — the correct role cannot be inferred
    personal-owner-count   REPORT ONLY — needs a human to say who owns it
"""

from __future__ import annotations

from .db import Db, now_ts
from .errors import Fatal
from .journal import Batch
from .model import Issue

# Demoting a surplus owner needs the non-owner role for that resource type.
_EDITOR_ROLE = {
    "shared_workflow": "workflow:editor",
    "shared_credentials": "credential:user",
}
_OWNER_ROLE = {
    "shared_workflow": "workflow:owner",
    "shared_credentials": "credential:owner",
}


def _instance_owner_project(db: Db) -> str:
    """The personal project of the instance owner — the safe place to park
    something that currently belongs to nobody."""
    row = db.one('''SELECT p.id FROM project p
                    JOIN project_relation pr ON pr."projectId"=p.id
                    JOIN "user" u ON u.id=pr."userId"
                    WHERE p.type='personal' AND u."roleSlug"='global:owner' ''')
    if not row:
        raise Fatal("cannot find the instance owner's personal project to adopt into")
    return row["id"]


def _fix_dup_owner(db: Db, batch: Batch, issue: Issue) -> None:
    """Keep the earliest owner row; demote the others rather than deleting
    them, so nobody silently loses access."""
    table, id_col, entity_id, owner_role = issue.target
    rows = db.q(f'SELECT * FROM "{table}" WHERE "{id_col}"=? AND role=? '
                'ORDER BY "createdAt", "projectId"', entity_id, owner_role)
    if len(rows) <= 1:
        return
    for surplus in rows[1:]:
        batch.update(table,
                     {id_col: entity_id, "projectId": surplus["projectId"]},
                     {"role": _EDITOR_ROLE[table], "updatedAt": now_ts()})
    batch.note(f"{entity_id}: kept 1 owner, demoted {len(rows) - 1} to "
               f"{_EDITOR_ROLE[table]}")


def _fix_orphan_share(db: Db, batch: Batch, issue: Issue) -> None:
    """Give an unreachable resource back to the instance owner."""
    table, col, entity_id = issue.target
    project_id = _instance_owner_project(db)
    ts = now_ts()
    batch.insert(table, {
        col: entity_id, "projectId": project_id, "role": _OWNER_ROLE[table],
        "createdAt": ts, "updatedAt": ts})
    batch.note(f"{entity_id}: adopted into the instance owner's personal project")


def _fix_cross_project_folder(db: Db, batch: Batch, issue: Issue) -> None:
    """Drop the folder reference — the same answer as --folder-policy root."""
    (workflow_id,) = issue.target
    batch.update("workflow_entity", {"id": workflow_id},
                 {"parentFolderId": None, "updatedAt": now_ts()})
    batch.note(f"{workflow_id}: cleared dangling folder reference "
               "(workflow moves to project root)")


def _fix_folder_parent_project(db: Db, batch: Batch, issue: Issue) -> None:
    (folder_id,) = issue.target
    batch.update("folder", {"id": folder_id},
                 {"parentFolderId": None, "updatedAt": now_ts()})
    batch.note(f"{folder_id}: detached from a parent in another project")


FIXERS = {
    "dup-owner": _fix_dup_owner,
    "orphan-share": _fix_orphan_share,
    "cross-project-folder": _fix_cross_project_folder,
    "folder-parent-project": _fix_folder_parent_project,
}

REPORT_ONLY = {
    "unknown-role": "the intended role cannot be inferred — set it explicitly "
                    "with share/transfer",
    "personal-owner-count": "needs a human decision about who owns the project",
}


def fixable(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.kind in FIXERS]


def apply_fixes(db: Db, batch: Batch, issues: list[Issue]) -> int:
    """Run the fixer for each issue that has one. Returns how many ran."""
    applied = 0
    for issue in issues:
        fixer = FIXERS.get(issue.kind)
        if fixer is None:
            continue
        fixer(db, batch, issue)
        applied += 1
    return applied


def select(issues: list[Issue], wanted: str) -> list[Issue]:
    """Resolve `--fix all` or `--fix kind1,kind2` against detected issues."""
    if wanted in ("all", "*"):
        return fixable(issues)

    kinds = {k.strip() for k in wanted.split(",") if k.strip()}
    unknown = kinds - set(FIXERS) - set(REPORT_ONLY)
    if unknown:
        raise Fatal(f"unknown issue kind(s): {', '.join(sorted(unknown))}\n"
                    f"      fixable: {', '.join(sorted(FIXERS))}")
    unfixable = kinds & set(REPORT_ONLY)
    if unfixable:
        raise Fatal(f"{', '.join(sorted(unfixable))} is report-only — "
                    f"{REPORT_ONLY[sorted(unfixable)[0]]}")
    return [i for i in issues if i.kind in kinds]
