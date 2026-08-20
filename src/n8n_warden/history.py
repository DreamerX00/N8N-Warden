"""Snapshot and undo-journal views, plus the undo operation itself.

Split out from the UI so that both the `history`/`undo` commands and the
interactive menu share one implementation.
"""

from __future__ import annotations

from pathlib import Path

from .console import bold, confirm, dim, green, say, step, table, warn
from .docker import Instance
from .errors import Fatal
from .journal import (UNDO_MARKER, is_undo_record, mark_undone, read_journal,
                      undo_batch, undoable)
from .runner import apply_change
from .storage import list_snapshots


def show_history() -> tuple[list[dict], list[Path]]:
    """Read-only view, shared by the command and the menu."""
    say()
    records = read_journal()
    rows = [{"id": r["id"], "at": r["at"][:19], "rows": r["rows"],
             "action": r["action"][:46],
             "state": dim("undone") if r.get("undone") else green("active")}
            for r in reversed(records[-25:])]
    say(bold("  undo journal"))
    say(table(rows, ["id", "at", "rows", "action", "state"]))

    say()
    snaps = list_snapshots()
    say(bold("  snapshots"))
    say(table([{"name": s.name, "size": f"{s.stat().st_size / 1024:.0f} KB"}
               for s in snaps[:15]], ["name", "size"]))
    say()
    return records, snaps


def revert(inst: Instance, record: dict, assume_yes: bool = False) -> None:
    """Undo one recorded batch, leaving alone any row that has since changed."""
    say()
    step(f"reverting {record['rows']} row(s) from {record['action']!r}")
    if not assume_yes and not confirm("proceed?", True):
        raise Fatal("cancelled")

    outcome: dict = {}

    def mutate(db, batch):
        reverted, skipped = undo_batch(db, record)
        outcome["skipped"] = skipped
        for item in skipped:
            batch.note(f"skipped — {item}")
        # undo_batch writes directly, so give the batch something to journal.
        # This marker is also what stops the record itself being undone later.
        batch.entries.append({"table": UNDO_MARKER, "pk": {"id": record["id"]},
                              "before": None, "after": {"undo_of": record["id"]}})
        batch.note(f"reverted {reverted} row(s)")

    apply_change(inst, f"undo {record['id']}", mutate)
    mark_undone(record["id"])

    if outcome.get("skipped"):
        warn(f"{len(outcome['skipped'])} row(s) left alone — they changed after "
             "the batch was recorded; inspect them by hand")


def last_undoable() -> dict:
    records = undoable(read_journal())
    if not records:
        raise Fatal("nothing to undo")
    return records[-1]


def find_undoable(batch_id: str) -> dict:
    """One undoable batch by id or unique prefix."""
    matches = [r for r in undoable(read_journal())
               if r["id"].startswith(batch_id)]
    if not matches:
        raise Fatal(f"no undoable batch {batch_id!r} — see `warden history`")
    if len(matches) > 1:
        raise Fatal(f"{batch_id!r} matches {len(matches)} batches — "
                    "use more of the id")
    return matches[0]
