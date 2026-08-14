"""Pruning execution history and workflow versions.

Execution data dominates an n8n database — commonly 99%+ of it — while the
things this tool otherwise manages (workflows, credentials, sharing) are a
rounding error. Trimming it is what keeps every other operation fast.

Deliberately NOT row-journalled. A before-image of several thousand execution
rows would be larger than the database it protects, so `undo` cannot revert a
prune; the snapshot (opt-in via --snapshot) is the recovery path. That trade is
stated plainly at the prompt rather than hidden.
"""

from __future__ import annotations

import sqlite3

from pathlib import Path

from .console import Spinner, bold, confirm, dim, ok, say, step, table, warn, yellow
from .db import Db
from .docker import Instance, start, stop, wait_healthy
from .errors import Fatal
from .journal import PRUNE_MARKER
from .runner import apply_change
from .storage import Workspace, direct_path, list_snapshots

# Children of execution_entity, deleted first. The schema declares the foreign
# keys but not ON DELETE CASCADE, so nothing removes these for us.
EXECUTION_CHILDREN = ("execution_data", "execution_annotations", "execution_metadata")


def _doomed_executions(db: Db, keep: int) -> list[str]:
    """Execution ids beyond the newest `keep` for their workflow."""
    rows = db.q("""
        SELECT id FROM (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY "workflowId" ORDER BY "startedAt" DESC, id DESC
            ) AS rn
            FROM execution_entity
        ) WHERE rn > ?""", keep)
    return [r["id"] for r in rows]


def _doomed_versions(db: Db, keep: int) -> list[str]:
    """Workflow-history versions beyond the newest `keep` for their workflow.

    Never returns a version a workflow currently points at: `workflow_entity.
    activeVersionId` references it ON DELETE RESTRICT, so deleting one would
    either fail the constraint or strand the pointer.
    """
    rows = db.q("""
        SELECT "versionId" FROM (
            SELECT "versionId", ROW_NUMBER() OVER (
                PARTITION BY "workflowId" ORDER BY "createdAt" DESC
            ) AS rn
            FROM workflow_history
        ) WHERE rn > ?""", keep)
    doomed = {r["versionId"] for r in rows}
    pinned = {r["activeVersionId"] for r in db.q(
        'SELECT "activeVersionId" FROM workflow_entity '
        'WHERE "activeVersionId" IS NOT NULL')}
    return sorted(doomed - pinned)


def survey(db: Db, keep_executions: int | None,
           keep_history: int | None) -> dict:
    """What a prune would remove, without removing anything."""
    result: dict = {"executions": [], "versions": [], "pinned_kept": 0}
    if keep_executions is not None:
        result["executions"] = _doomed_executions(db, keep_executions)
        result["executions_total"] = db.one(
            "SELECT COUNT(*) AS n FROM execution_entity")["n"]
    if keep_history is not None:
        result["versions"] = _doomed_versions(db, keep_history)
        result["versions_total"] = db.one(
            "SELECT COUNT(*) AS n FROM workflow_history")["n"]
        beyond = db.q("""
            SELECT "versionId" FROM (
                SELECT "versionId", ROW_NUMBER() OVER (
                    PARTITION BY "workflowId" ORDER BY "createdAt" DESC) AS rn
                FROM workflow_history) WHERE rn > ?""", keep_history)
        result["pinned_kept"] = len(beyond) - len(result["versions"])
    return result


def _delete_in_chunks(db: Db, sql: str, ids: list[str], size: int = 500) -> int:
    """SQLite caps host parameters, so large deletes go in batches."""
    total = 0
    for start_at in range(0, len(ids), size):
        chunk = ids[start_at:start_at + size]
        marks = ",".join("?" for _ in chunk)
        total += db.exec(sql.format(marks=marks), *chunk).rowcount or 0
    return total


def prune(inst: Instance, keep_executions: int | None, keep_history: int | None,
          vacuum: bool = True, dry_run: bool = False,
          take_snapshot: bool = False) -> int:
    if keep_executions is None and keep_history is None:
        raise Fatal("nothing selected — pass --executions N and/or --history N")
    for value in (keep_executions, keep_history):
        if value is not None and value < 0:
            raise Fatal("--executions/--history must be zero or more")

    with Workspace(inst, write=False) as db:
        plan = survey(db, keep_executions, keep_history)

    say()
    say(bold("  prune plan"))
    rows = []
    if keep_executions is not None:
        rows.append({"what": "executions", "keep": f"{keep_executions} per workflow",
                     "delete": len(plan["executions"]),
                     "remain": plan["executions_total"] - len(plan["executions"])})
    if keep_history is not None:
        rows.append({"what": "workflow versions", "keep": f"{keep_history} per workflow",
                     "delete": len(plan["versions"]),
                     "remain": plan["versions_total"] - len(plan["versions"])})
    say(table(rows, ["what", "keep", "delete", "remain"]))
    if plan["pinned_kept"]:
        say()
        step(f"{plan['pinned_kept']} version(s) kept beyond the limit because a "
             "workflow still points at them")

    if not plan["executions"] and not plan["versions"]:
        say()
        ok("nothing to prune")
        return 0

    if dry_run:
        say(dim("\n  dry run — nothing deleted"))
        return 0

    say()
    warn("this cannot be reverted with `undo` — a row-level journal of this "
         "much data would exceed the database itself")
    if not take_snapshot:
        warn("no snapshot will be taken (pass --snapshot to take one first; "
             "at multi-GB sizes it takes minutes)")
    if not confirm("proceed?", False):
        raise Fatal("cancelled")

    doomed_exec = plan["executions"]
    doomed_vers = plan["versions"]
    counts: dict = {}

    def mutate(db, batch):
        removed = 0
        if doomed_exec:
            for child in EXECUTION_CHILDREN:
                try:
                    _delete_in_chunks(
                        db, f'DELETE FROM "{child}" WHERE "executionId" IN ({{marks}})',
                        doomed_exec)
                except sqlite3.OperationalError:
                    continue          # table absent on this n8n version
            removed += _delete_in_chunks(
                db, "DELETE FROM execution_entity WHERE id IN ({marks})", doomed_exec)
        if doomed_vers:
            removed += _delete_in_chunks(
                db, 'DELETE FROM workflow_history WHERE "versionId" IN ({marks})',
                doomed_vers)
        counts["removed"] = removed
        # One bookkeeping row so the batch is journalled as a record of what
        # happened. It carries no before-images, so `undo` will skip it.
        batch.entries.append({"table": PRUNE_MARKER,
                              "pk": {"executions": len(doomed_exec),
                                     "versions": len(doomed_vers)},
                              "before": None, "after": {"removed": removed}})
        batch.note(f"deleted {len(doomed_exec)} execution(s) and "
                   f"{len(doomed_vers)} workflow version(s)")

    apply_change(inst, f"prune executions>{keep_executions} history>{keep_history}",
                 mutate, skip_snapshot=not take_snapshot)

    if vacuum:
        _vacuum(inst)
    return 0


# --- other reclaimable space --------------------------------------------

def disk_report(inst: Instance, db: Db) -> list[dict]:
    """Everything worth pruning, database and filesystem alike, with sizes."""
    rows: list[dict] = []

    try:
        sizes = {n: b for n, b in db.q(
            "SELECT name, SUM(pgsize) FROM dbstat GROUP BY name")}
    except Exception:
        sizes = {}

    def mb(table: str) -> str:
        return f"{sizes.get(table, 0) / 2**20:.1f} MB" if sizes else "?"

    def count(table: str) -> int:
        try:
            return db.one(f'SELECT COUNT(*) AS n FROM "{table}"')["n"]
        except Exception:
            return 0

    rows.append({"what": "execution history", "detail": f"{count('execution_entity'):,} runs",
                 "size": mb("execution_data"), "prune": "executions"})
    rows.append({"what": "workflow versions", "detail": f"{count('workflow_history'):,} versions",
                 "size": mb("workflow_history"), "prune": "history"})
    rows.append({"what": "insights (dashboard)", "detail": f"{count('insights_by_period'):,} periods",
                 "size": mb("insights_by_period"), "prune": "insights"})

    snaps = list_snapshots()
    snap_bytes = sum(s.stat().st_size for s in snaps)
    rows.append({"what": "warden snapshots", "detail": f"{len(snaps)} file(s)",
                 "size": f"{snap_bytes / 2**20:.1f} MB", "prune": "snapshots"})

    logs = _event_logs(inst)
    log_bytes = sum(p.stat().st_size for p in logs)
    rows.append({"what": "n8n event logs", "detail": f"{len(logs)} file(s)",
                 "size": f"{log_bytes / 2**20:.1f} MB", "prune": "event-logs"})

    return rows


def _event_logs(inst: Instance) -> list[Path]:
    """n8n's own rotating event log files, when the data dir is reachable."""
    path = direct_path(inst, write=False)
    if path is None:
        return []
    return sorted(path.parent.glob("n8nEventLog*.log"))


def prune_snapshots(keep: int) -> tuple[int, int]:
    """Drop all but the newest `keep` snapshots. Returns (removed, bytes freed).

    These are ours, not n8n's — no database involvement, so no downtime.
    """
    snaps = list_snapshots()          # newest first
    doomed = snaps[keep:]
    freed = sum(s.stat().st_size for s in doomed)
    for snap in doomed:
        snap.unlink(missing_ok=True)
    return len(doomed), freed


def prune_event_logs(inst: Instance, keep_current: bool = True) -> tuple[int, int]:
    """Delete rotated n8n event logs. The active log is left alone by default."""
    removed, freed = 0, 0
    for log in _event_logs(inst):
        if keep_current and log.name == "n8nEventLog.log":
            continue
        freed += log.stat().st_size
        log.unlink(missing_ok=True)
        removed += 1
    return removed, freed


def prune_insights(inst: Instance, keep_days: int | None, dry_run: bool = False) -> int:
    """Trim the insights the dashboard is drawn from.

    Nothing references these rows, so removing them is safe — but the charts
    lose that history permanently, which is why it is never bundled into
    `--executions`.
    """
    with Workspace(inst, write=False) as db:
        total = db.one("SELECT COUNT(*) AS n FROM insights_by_period")["n"]
        if keep_days is None:
            doomed = total
        else:
            doomed = db.one(
                "SELECT COUNT(*) AS n FROM insights_by_period "
                "WHERE \"periodStart\" < date('now', ?)", f"-{keep_days} days")["n"]

    say()
    step(f"insights: {doomed} of {total} period rows would go")
    if not doomed:
        ok("nothing to prune")
        return 0
    if dry_run:
        say(dim("  dry run — nothing deleted"))
        return 0

    warn("the insights dashboard loses this history permanently")
    if not confirm("proceed?", False):
        raise Fatal("cancelled")

    def mutate(db, batch):
        if keep_days is None:
            n = db.exec("DELETE FROM insights_by_period").rowcount
            db.exec("DELETE FROM insights_raw")
        else:
            n = db.exec("DELETE FROM insights_by_period "
                        "WHERE \"periodStart\" < date('now', ?)",
                        f"-{keep_days} days").rowcount
        batch.entries.append({"table": PRUNE_MARKER, "pk": {"insights": n},
                              "before": None, "after": {"removed": n}})
        batch.note(f"deleted {n} insight period row(s)")

    apply_change(inst, "prune insights", mutate, skip_snapshot=True)
    return 0


def _vacuum(inst: Instance) -> None:
    """Reclaim the freed pages. Deleting rows alone does not shrink the file."""
    path = direct_path(inst, write=True)
    if path is None:
        warn("skipping vacuum — needs direct access to a bind-mounted database")
        return

    was_running = stop(inst)
    before = path.stat().st_size
    say()
    with Spinner(f"vacuuming {before / 2**20:.0f} MB — rewrites the file, "
                 "this is what reclaims space", "vacuum complete"):
        conn = sqlite3.connect(str(path), isolation_level=None)  # VACUUM needs autocommit
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()
    after = path.stat().st_size
    ok(f"database {before / 2**30:.2f} GB → {after / 2**30:.2f} GB "
       f"({yellow(f'{(before - after) / 2**30:.2f} GB reclaimed')})")

    if was_running:
        start(inst)
        wait_healthy(inst)
