"""Environment and health report. Read-only."""

from __future__ import annotations

from .config import EXPECTED_COLUMNS, STATE_DIR, VERIFIED_N8N
from .console import bold, dim, err, green, ok, red, say, step, table, warn
from .docker import Instance, is_running
from .journal import read_journal
from .model import (check_invariants, compat_note, migration_state, roles,
                    schema_check)
from .ops import bcrypt_available
from .queries import credentials, folders, projects, users, workflows
from .repair import FIXERS, REPORT_ONLY, apply_fixes, fixable, select
from .runner import apply_change
from .storage import Workspace, list_snapshots


def doctor(inst: Instance) -> None:
    say()
    say(bold("  warden doctor"))
    say()
    say(table(_environment(inst), ["k", "v"], ["", ""]))
    say()

    with Workspace(inst, write=False) as db:
        problems = schema_check(db)
        for problem in problems:
            err(problem)
        if not problems:
            ok(f"schema: {len(EXPECTED_COLUMNS)}/{len(EXPECTED_COLUMNS)} tables "
               "match the known 2.x layout")

        name, _, total = migration_state(db)
        step(f"migrations: {total} applied, latest {name or '?'}")
        note = compat_note(db)
        if note:
            warn(note)
        else:
            ok(f"within the verified range (up to n8n {VERIFIED_N8N})")

        for kind in ("global", "project", "workflow", "credential"):
            found = roles(db, kind)
            step(f"{kind} roles: {', '.join(found) if found else dim('none')}")

        say()
        counts = {"projects": len(projects(db)), "users": len(users(db)),
                  "workflows": len(workflows(db)), "credentials": len(credentials(db)),
                  "folders": len(folders(db))}
        step("inventory: " + ", ".join(f"{v} {k}" for k, v in counts.items()))

        issues = check_invariants(db)
        say()
        if issues:
            err(f"{len(issues)} invariant issue(s):")
            for issue in issues:
                mark = green("fixable") if issue.kind in FIXERS else dim("report-only")
                say(f"      {red('·')} [{issue.kind}] {issue}  {mark}")
            repairable = fixable(issues)
            if repairable:
                say()
                step(f"repair {len(repairable)}: warden doctor --fix all")
        else:
            ok("all invariants hold")

    undoable = [r for r in read_journal() if not r.get("undone")]
    say()
    step(f"{len(list_snapshots())} snapshot(s), {len(undoable)} undoable batch(es)")
    say()
    say("  " + (green("✓ ready") if not problems
                else red("✗ schema mismatch — do not write")))
    say()


def repair(inst: Instance, wanted: str, dry_run: bool = False) -> int:
    """Apply fixers for the selected issue kinds, inside the normal write cycle.

    Repairs are ordinary mutations: snapshotted, transactional, journalled and
    undoable. A repair that would itself break an invariant is rolled back like
    any other change.
    """
    with Workspace(inst, write=False) as db:
        issues = check_invariants(db)
        chosen = select(issues, wanted)

    if not issues:
        ok("all invariants hold — nothing to repair")
        return 0
    if not chosen:
        warn("no fixable issues matched")
        for issue in issues:
            if issue.kind in REPORT_ONLY:
                say(f"      {dim('·')} [{issue.kind}] {REPORT_ONLY[issue.kind]}")
        return 0

    say()
    for issue in chosen:
        step(f"[{issue.kind}] {issue}")

    def mutate(db, batch):
        # Re-detect inside the write transaction: the snapshot above was taken
        # from a separate read, and issues must be addressed against live rows.
        applied = apply_fixes(db, batch, select(check_invariants(db), wanted))
        batch.note(f"applied {applied} repair(s)")

    apply_change(inst, f"doctor --fix {wanted}", mutate, dry_run=dry_run)
    return 0


def _environment(inst: Instance) -> list[dict]:
    rows = [
        {"k": "container",
         "v": f"{inst.container}  ({'running' if is_running(inst) else 'stopped'})"},
        {"k": "image", "v": inst.image},
        {"k": "version", "v": inst.version},
        {"k": "db", "v": inst.db_kind},
    ]
    if inst.offline:
        rows.append({"k": "file", "v": str(inst.db_file)})
    elif inst.db_kind == "sqlite":
        rows.append({"k": "source", "v": f"{inst.mount_type} {inst.mount_spec}"})
    else:
        rows.append({"k": "source", "v": f"{inst.pg.get('host')}:{inst.pg.get('port')}"
                                         f"/{inst.pg.get('database')}"})
    rows.append({"k": "state dir", "v": str(STATE_DIR)})
    rows.append({"k": "bcrypt",
                 "v": "available" if bcrypt_available()
                      else "absent (set-password disabled)"})
    return rows
