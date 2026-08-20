"""The write cycle.

Every mutation in the tool goes through `apply_change`, which is the single
place that decides when it is safe to touch n8n's database:

    snapshot → stop → mutate → verify invariants → commit → push → start → health

A change that would break an invariant is rolled back and never written.
"""

from __future__ import annotations

from .console import (assume_yes, bold, confirm, dim, err, green, ok, red, say,
                      step, table, warn)
from .docker import Instance
from .errors import Fatal
from .journal import Batch
from .model import check_invariants, compat_note
from .storage import Workspace, restore, snapshot


def apply_change(inst: Instance, action: str, mutate, dry_run: bool = False,
                 skip_snapshot: bool = False) -> str | None:
    """Run `mutate(db, batch)` inside the full safety envelope.

    Returns the journal batch id, or None when nothing was written.
    """
    say()
    say(bold(f"  {action}"))

    if dry_run:
        return _preview(inst, action, mutate)

    snap = None
    if not skip_snapshot:
        snap = snapshot(inst, action)
        ok(f"snapshot {snap.name}")

    workspace = Workspace(inst, write=True)
    with workspace as db:
        _gate_on_version(db)

        batch = Batch(db, action)
        before = check_invariants(db)
        mutate(db, batch)

        regressions = [i for i in check_invariants(db) if i not in before]
        if regressions:
            db.conn.rollback()
            err("change rejected — it would break these invariants:")
            for issue in regressions:
                say(f"      {red('·')} {issue}")
            raise Fatal("nothing was written")

        if not batch.entries:
            db.conn.rollback()
            warn("no changes to make")
            return None

        batch_id = batch.save()
        for note in batch.notes:
            step(note)
        say(f"  {green('✎')} {len(batch.entries)} row(s) written")
        try:
            workspace.commit_and_push()
        except Exception:
            # Everything before this point rolls back in a transaction; a
            # failure mid-push can leave the container holding an inconsistent
            # database. That is exactly what the snapshot is for.
            _rescue(inst, snap)
            raise
        return batch_id


def _rescue(inst: Instance, snap) -> None:
    err("write-back failed — the database in the container may be inconsistent")
    if snap is None:
        warn("no snapshot was taken for this change — inspect the database by hand")
        return
    if assume_yes() or confirm(f"restore snapshot {snap.name}?", True):
        # n8n is still stopped here; the workspace restarts it on exit.
        restore(inst, snap)


def _gate_on_version(db) -> None:
    """n8n may have moved past what this tool was checked against: table shapes
    can still match while a data-shape migration changed what a value means."""
    note = compat_note(db)
    if not note:
        return
    if assume_yes():
        warn(note + " — proceeding (--yes)")
        return
    warn(note)
    if not confirm("write anyway?", False):
        db.conn.rollback()
        raise Fatal("aborted on version mismatch")


def _preview(inst: Instance, action: str, mutate) -> None:
    """Show the row-level plan without writing anything.

    Runs in `simulate` mode: the mutations really execute (operations read
    their own writes back, so a plan computed without executing would go
    stale mid-batch) and the transaction is then rolled back.
    """
    with Workspace(inst, simulate=True) as db:
        batch = Batch(db, action)
        before = check_invariants(db)
        mutate(db, batch)
        after = check_invariants(db)
        db.conn.rollback()

        if not batch.entries:
            warn("no changes")
        else:
            rows = [{"op": ("INSERT" if e["before"] is None else
                            "DELETE" if e["after"] is None else "UPDATE"),
                     "table": e["table"],
                     "key": ", ".join(f"{k}={v}" for k, v in e["pk"].items())}
                    for e in batch.entries]
            say(table(rows, ["op", "table", "key"]))
            for note in batch.notes:
                step(note)
            for issue in (i for i in after if i not in before):
                err(f"would break: {issue}")

        say(dim("\n  dry run — nothing written"))
    return None
