"""Snapshots, restore, and the writable workspace.

Two independent recovery paths live here: a full snapshot taken before every
write batch (coarse but always correct) and, in `journal.py`, a row-level undo
log. Either alone would be a single point of failure.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from .config import DB_NAME, MAX_SNAPSHOTS, N8N_DIR, SIDECARS, SNAP_DIR
from .console import Spinner, ok, warn
from .db import Db, PgDb
from .docker import Instance, helper, sh, start, stop, wait_healthy
from .errors import Fatal


# --- snapshots -----------------------------------------------------------

def snapshot(inst: Instance, label: str) -> Path:
    """Full copy of n8n's data. The coarse but always-correct way back."""
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    snap_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", label)[:40]

    if inst.offline:
        out = SNAP_DIR / f"{snap_id}__{safe}.sqlite"
        # The backup API, not copy2: a plain file copy misses whatever still
        # sits in the -wal sidecar, silently snapshotting an older state.
        src = sqlite3.connect(f"file:{inst.db_file}?mode=ro", uri=True)
        dst = sqlite3.connect(str(out))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    elif inst.db_kind == "postgres":
        out = SNAP_DIR / f"{snap_id}__{safe}.sql"
        _pg_dump(inst, out)
    else:
        out = SNAP_DIR / f"{snap_id}__{safe}.tgz"
        mounts = [(str(SNAP_DIR), "/backup")]
        with Spinner("taking snapshot", f"snapshot {snap_id}"):
            helper(inst, f"tar czf /backup/{out.name} -C /data .", extra_mounts=mounts)
            # tar runs as root, so the artefact lands root-owned; make it ours.
            helper(inst, f"chown {os.getuid()}:{os.getgid()} /backup/{out.name}",
                   extra_mounts=mounts)

    _prune()
    return out


def _pg_dump(inst: Instance, out: Path) -> None:
    pg = inst.pg
    env = {**os.environ, "PGPASSWORD": pg["password"]}
    with open(out, "w") as fh:
        proc = subprocess.run(
            ["docker", "exec", "-e", f"PGPASSWORD={pg['password']}", inst.container,
             "pg_dump", "-h", pg["host"], "-p", str(pg["port"]),
             "-U", pg["user"], "-d", pg["database"]],
            stdout=fh, stderr=subprocess.PIPE, text=True, env=env)
    if proc.returncode != 0:
        raise Fatal(f"pg_dump failed: {proc.stderr}")


def _prune() -> None:
    snaps = sorted(SNAP_DIR.glob("*"), key=lambda p: p.name)
    for old in snaps[:-MAX_SNAPSHOTS]:
        old.unlink(missing_ok=True)


def list_snapshots() -> list[Path]:
    if not SNAP_DIR.exists():
        return []
    return sorted(SNAP_DIR.glob("*"), key=lambda p: p.name, reverse=True)


def restore(inst: Instance, snap: Path) -> None:
    """Roll the whole database back. Destructive by definition."""
    was_running = stop(inst)
    if inst.offline:
        shutil.copy2(snap, inst.db_file)
    elif snap.suffix == ".sql":
        raise Fatal("restore of a Postgres dump must be done manually with psql "
                    f"(dump is at {snap})")
    else:
        helper(inst,
               "rm -rf /data/* /data/.[!.]* 2>/dev/null; "
               f"tar xzf /backup/{snap.name} -C /data && chown -R node:node /data",
               extra_mounts=[(str(SNAP_DIR), "/backup")])
    ok(f"restored from {snap.name}")
    if was_running:
        start(inst)
        wait_healthy(inst)


# --- workspace -----------------------------------------------------------

def direct_path(inst: Instance, write: bool) -> Path | None:
    """The database's real path on the host, when we can use it directly.

    A bind-mounted data dir owned by the invoking user needs no `docker cp` at
    all — and copying a multi-gigabyte database twice per operation is the
    difference between seconds and minutes. Named volumes stay on the copy
    path, since `/var/lib/docker/volumes` is root-only.

    Reading while n8n runs is safe: SQLite's WAL mode permits concurrent
    readers, and the connection is opened read-only.
    """
    if inst.offline or inst.db_kind != "sqlite" or inst.mount_type != "bind":
        return None
    if not inst.mount_spec:
        return None
    path = Path(inst.mount_spec) / DB_NAME
    if not path.exists() or not os.access(path, os.R_OK):
        return None
    if write and not (os.access(path, os.W_OK) and os.access(path.parent, os.W_OK)):
        return None
    return path


class Workspace:
    """A writable local copy of n8n's database.

    Guarantees:
      * n8n is stopped for the whole write window
      * the WAL is checkpointed into the main file before push
      * stale -wal/-shm are removed *and* the main file swapped in one step,
        because a leftover write-ahead log would be replayed over the new file
        on next boot and corrupt it
    """

    def __init__(self, inst: Instance, write: bool = False, simulate: bool = False):
        """
        write     — stop n8n, mutate, push back
        simulate  — mutate a live connection and always roll back (dry run).
                    Needs a writable connection because operations read their
                    own writes back, but never commits and never stops n8n.
        otherwise — read-only
        """
        self.inst = inst
        self.write = write
        self.simulate = simulate
        self.tmp: Path | None = None
        self.local: Path | None = None
        self.db: Db | None = None
        self._restart = False
        self._in_place = False

    def __enter__(self) -> Db:
        inst = self.inst
        if inst.db_kind == "postgres":
            self.db = PgDb(inst)
            return self.db

        in_place = None if inst.offline else direct_path(inst, self.write or self.simulate)
        if inst.offline:
            self.local = inst.db_file
        elif in_place:
            # Bind mount we can reach on the host: skip the copy entirely.
            # At multi-gigabyte database sizes this is the difference between
            # a couple of seconds and a couple of minutes per operation.
            if self.write:
                self._restart = stop(inst)
            self.local = in_place
            self._in_place = True
        else:
            if self.write:
                self._restart = stop(inst)
            self.tmp = Path(tempfile.mkdtemp(prefix="n8nadm-"))
            self.local = self.tmp / DB_NAME
            sh("docker", "cp", f"{inst.container}:{N8N_DIR}/{DB_NAME}", str(self.local))
            for side in SIDECARS:
                # Absent sidecars are normal after a clean shutdown.
                subprocess.run(["docker", "cp", f"{inst.container}:{N8N_DIR}/{side}",
                                str(self.tmp / side)], capture_output=True)

        readonly = self._in_place and not self.write and not self.simulate
        self.db = Db(self.local, readonly=readonly)
        if inst.offline and "user" not in self.db.tables():
            self.db.conn.close()    # __exit__ never runs when __enter__ raises
            raise Fatal(f"{inst.db_file} does not look like an n8n database "
                        "(no 'user' table)")
        if self.simulate and self._in_place:
            # n8n is still running. Fail fast rather than queue behind its
            # writer; a dry run must never stall the live instance.
            self.db.conn.execute("PRAGMA busy_timeout = 2000")
        return self.db

    def commit_and_push(self) -> None:
        """Checkpoint, close, and swap the file back into the container."""
        if self.db is None or self.local is None:
            raise Fatal("workspace is not open")
        self.db.conn.commit()
        self.db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.conn.close()
        self.db = None

        if self.inst.offline or self._in_place:
            # Edited where n8n reads it — there is nothing to copy back.
            if self._in_place:
                ok("database written in place")
            return

        staged = f"{N8N_DIR}/{DB_NAME}.n8nadm-new"
        sh("docker", "cp", str(self.local), f"{self.inst.container}:{staged}")
        # One shell invocation: drop the stale WAL and swap, so the container
        # never holds a new database file beside an old log.
        helper(self.inst,
               f"cd /data && rm -f {SIDECARS[0]} {SIDECARS[1]} "
               f"&& mv {DB_NAME}.n8nadm-new {DB_NAME} "
               f"&& chown node:node {DB_NAME} && chmod 644 {DB_NAME}")
        ok("database written back")

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.db is not None:
            try:
                if exc_type:
                    self.db.conn.rollback()
                self.db.conn.close()
            except Exception:
                pass
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)
        if self._restart:
            start(self.inst)
            if not wait_healthy(self.inst) and self.write:
                warn("if n8n stays unhealthy, roll the database back with: "
                     "warden restore")
        return False
