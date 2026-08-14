"""Mutation recording and row-level undo.

Every write goes through `Batch`, which captures a before-image *and* an
after-image of each row it touches. The after-image is what makes undo safe:
without it, reverting would silently discard any change made in the n8n UI
after the batch was recorded.
"""

from __future__ import annotations

import json
from datetime import datetime

from .config import JOURNAL, PK, STATE_DIR
from .db import Db, now_ts
from .errors import Fatal

# Pseudo-tables recorded so a batch that writes no ordinary rows still leaves a
# journal entry. Any table starting with '_' is bookkeeping: never replayed, and
# never itself revertible.
UNDO_MARKER = "_undo"
PRUNE_MARKER = "_prune"


class Batch:
    """Records a before-image of every row it touches, so the batch can be
    reversed without restoring the whole database."""

    def __init__(self, db: Db, action: str):
        self.db = db
        self.action = action
        self.entries: list[dict] = []
        self.notes: list[str] = []

    # -- internals -------------------------------------------------------

    def _fetch(self, table: str, pk: dict) -> dict | None:
        where = " AND ".join(f'"{k}"=?' for k in pk)
        row = self.db.one(f'SELECT * FROM "{table}" WHERE {where}', *pk.values())
        return dict(row) if row else None

    def _record(self, table, pk, before, after) -> None:
        self.entries.append({"table": table, "pk": pk, "before": before, "after": after})

    # -- mutations -------------------------------------------------------

    def insert(self, table: str, row: dict) -> None:
        pk = {k: row[k] for k in PK[table]}
        if self._fetch(table, pk):
            raise Fatal(f"{table} row already exists: {pk}")
        cols = ",".join(f'"{c}"' for c in row)
        marks = ",".join("?" for _ in row)
        self.db.exec(f'INSERT INTO "{table}" ({cols}) VALUES ({marks})', *row.values())
        self._record(table, pk, None, row)

    def update(self, table: str, pk: dict, changes: dict) -> None:
        before = self._fetch(table, pk)
        if before is None:
            raise Fatal(f"{table} row not found: {pk}")
        sets = ",".join(f'"{c}"=?' for c in changes)
        where = " AND ".join(f'"{k}"=?' for k in pk)
        self.db.exec(f'UPDATE "{table}" SET {sets} WHERE {where}',
                     *changes.values(), *pk.values())
        self._record(table, pk, before, {**before, **changes})

    def delete(self, table: str, pk: dict) -> None:
        before = self._fetch(table, pk)
        if before is None:
            return
        where = " AND ".join(f'"{k}"=?' for k in pk)
        self.db.exec(f'DELETE FROM "{table}" WHERE {where}', *pk.values())
        self._record(table, pk, before, None)

    def note(self, message: str) -> None:
        self.notes.append(message)

    # -- persistence -----------------------------------------------------

    def save(self) -> str | None:
        if not self.entries:
            return None
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        batch_id = datetime.now().strftime("%Y%m%dT%H%M%S%f")[:18]
        record = {"id": batch_id, "at": now_ts(), "action": self.action,
                  "rows": len(self.entries),
                  "kind": "undo" if _has_undo_marker(self.entries) else "write",
                  "entries": self.entries}
        with open(JOURNAL, "a") as fh:
            fh.write(json.dumps(record) + "\n")
        return batch_id


def _has_undo_marker(entries: list[dict]) -> bool:
    return any(e["table"] == UNDO_MARKER for e in entries)


def is_undo_record(record: dict) -> bool:
    """Undo runs through the normal write cycle, so it lands in the journal
    like any other batch. Without this, a second `undo` would revert the first
    undo's marker row instead of stepping back to the previous real batch.

    The `kind` field covers records written since this was fixed; the marker
    scan covers older ones.
    """
    return record.get("kind") == "undo" or _has_undo_marker(record.get("entries", []))


def has_real_rows(entries: list[dict]) -> bool:
    """False for a batch made entirely of bookkeeping markers."""
    return any(not e["table"].startswith("_") for e in entries)


def undoable(records: list[dict]) -> list[dict]:
    """Real writes, oldest first, that have not already been reverted.

    Excludes undo records and marker-only batches such as prunes — without
    this, repeated `undo` would consume its own journal entries instead of
    stepping back through actual changes.
    """
    return [r for r in records
            if not r.get("undone")
            and not is_undo_record(r)
            and has_real_rows(r.get("entries", []))]


def read_journal() -> list[dict]:
    if not JOURNAL.exists():
        return []
    records = []
    for line in JOURNAL.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def mark_undone(batch_id: str) -> None:
    records = read_journal()
    with open(JOURNAL, "w") as fh:
        for record in records:
            if record["id"] == batch_id:
                record["undone"] = True
            fh.write(json.dumps(record) + "\n")


def _same(a, b) -> bool:
    """Compare column values tolerantly.

    SQLite hands back 0/1 for booleans and the journal round-trips through
    JSON, so exact identity is too strict to be useful here.
    """
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return str(a) == str(b)


def undo_batch(db: Db, record: dict) -> tuple[int, list[str]]:
    """Replay before-images in reverse, skipping rows that moved on.

    Staleness matters: if someone edited a workflow in the UI after this batch
    was recorded, blindly restoring the before-image would discard their
    change. Each row is checked against the after-image we wrote; if reality
    no longer matches, the row is left alone and reported.

    Returns (rows reverted, descriptions of rows skipped).
    """
    reverted, skipped = 0, []

    for entry in reversed(record["entries"]):
        table, pk = entry["table"], entry["pk"]
        before, after = entry["before"], entry.get("after")
        if table.startswith("_"):          # bookkeeping marker, not a real row
            continue

        where = " AND ".join(f'"{k}"=?' for k in pk)
        key = ", ".join(f"{k}={v}" for k, v in pk.items())
        row = db.one(f'SELECT * FROM "{table}" WHERE {where}', *pk.values())
        current = dict(row) if row else None

        if after is None:
            # We deleted it; it should still be gone.
            if current is not None:
                skipped.append(f"{table}[{key}] was recreated after the batch")
                continue
        else:
            # We inserted or updated it; it should still look like we left it.
            if current is None:
                skipped.append(f"{table}[{key}] was deleted after the batch")
                continue
            drifted = [c for c in after if not _same(current.get(c), after[c])]
            if drifted:
                skipped.append(f"{table}[{key}] changed after the batch "
                               f"({', '.join(drifted[:3])})")
                continue

        if before is None:
            db.exec(f'DELETE FROM "{table}" WHERE {where}', *pk.values())
        elif current is not None:
            sets = ",".join(f'"{c}"=?' for c in before)
            db.exec(f'UPDATE "{table}" SET {sets} WHERE {where}',
                    *before.values(), *pk.values())
        else:
            cols = ",".join(f'"{c}"' for c in before)
            marks = ",".join("?" for _ in before)
            db.exec(f'INSERT INTO "{table}" ({cols}) VALUES ({marks})', *before.values())
        reverted += 1

    return reverted, skipped
