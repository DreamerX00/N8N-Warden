"""Finding the n8n container, and driving its lifecycle.

Everything that shells out to `docker` lives here. Note that the database is
never read from its host path: `/var/lib/docker/volumes/` is root-only, so an
in-place approach would demand sudo for every operation. See `storage.py`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import N8N_DIR
from .console import Spinner, err, ok, warn
from .errors import Fatal


def sh(*args, check: bool = True, stdin: str | None = None) -> str:
    """Run a command and return stdout. Never goes through a shell."""
    proc = subprocess.run(args, capture_output=True, text=True, input=stdin)
    if check and proc.returncode != 0:
        raise Fatal(f"command failed: {' '.join(args)}\n{proc.stderr.strip()}")
    return proc.stdout.strip()


@dataclass
class Instance:
    """Where one n8n lives and how to reach it."""

    container: str
    image: str
    running: bool
    version: str = "?"
    db_kind: str = "sqlite"        # sqlite | postgres
    mount_spec: str = ""           # volume name or host bind path
    mount_type: str = ""           # volume | bind
    pg: dict = field(default_factory=dict)
    db_file: Path | None = None    # set when operating on a raw file
    host_port: str = ""            # published port, for the health probe

    @property
    def offline(self) -> bool:
        """True when pointed at a database file with no container involved."""
        return self.db_file is not None


# --- discovery -----------------------------------------------------------

def discover() -> list[Instance]:
    if shutil.which("docker") is None:
        raise Fatal("docker not found on PATH")
    raw = sh("docker", "ps", "-a", "--format",
             "{{.Names}}\t{{.Image}}\t{{.State}}", check=False)
    found = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and "n8n" in parts[1].lower():
            found.append(inspect(parts[0], parts[1], parts[2] == "running"))
    return found


def inspect(name: str, image: str, running: bool) -> Instance:
    inst = Instance(container=name, image=image, running=running)
    env = _env_of(name)

    if env.get("DB_TYPE", "sqlite").startswith("postgres"):
        inst.db_kind = "postgres"
        inst.pg = {
            "host": env.get("DB_POSTGRESDB_HOST", "localhost"),
            "port": int(env.get("DB_POSTGRESDB_PORT", "5432")),
            "database": env.get("DB_POSTGRESDB_DATABASE", "n8n"),
            "user": env.get("DB_POSTGRESDB_USER", "postgres"),
            "password": env.get("DB_POSTGRESDB_PASSWORD", ""),
            "schema": env.get("DB_POSTGRESDB_SCHEMA", "public"),
        }
    else:
        inst.mount_type, inst.mount_spec = _data_mount(name)

    inst.host_port = _published_port(name)
    inst.version = _version(name, image, running)
    return inst


def _env_of(name: str) -> dict:
    raw = sh("docker", "inspect", name, "--format",
             "{{range .Config.Env}}{{println .}}{{end}}", check=False)
    env = {}
    for line in raw.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            env[key] = value
    return env


def _data_mount(name: str) -> tuple[str, str]:
    raw = sh("docker", "inspect", name, "--format",
             "{{range .Mounts}}{{.Type}}\t{{.Name}}\t{{.Source}}\t{{.Destination}}\n{{end}}",
             check=False)
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) == 4 and parts[3].rstrip("/") == N8N_DIR:
            kind, volume, source, _ = parts
            return kind, (volume if kind == "volume" and volume else source)
    return "", ""


def _published_port(name: str) -> str:
    raw = sh("docker", "inspect", name, "--format",
             "{{json .HostConfig.PortBindings}}", check=False)
    try:
        for spec, binds in (json.loads(raw) or {}).items():
            if spec.startswith("5678") and binds:
                return binds[0].get("HostPort", "")
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass
    return ""


def _version(name: str, image: str, running: bool) -> str:
    if running:
        found = re.search(r"\d+\.\d+\.\d+", sh("docker", "exec", name, "n8n", "--version",
                                               check=False))
        if found:
            return found.group(0)
    tagged = re.search(r":(\d+\.\d+\.\d+)", image)
    return tagged.group(1) if tagged else "?"


def choose_instance(name: str | None = None, db_file: str | None = None) -> Instance:
    from .console import pick, say

    if db_file:
        path = Path(db_file).expanduser().resolve()
        if not path.exists():
            raise Fatal(f"no such file: {path}")
        return Instance(container="(offline)", image="-", running=False, db_file=path)

    found = discover()
    if not found:
        raise Fatal("no n8n container found "
                    "(use --db-file to work on a database directly)")
    if name:
        for inst in found:
            if inst.container == name:
                return inst
        raise Fatal(f"no container named {name!r}")
    if len(found) == 1:
        return found[0]

    say("\n  Multiple n8n containers found:")
    return pick(found, lambda i: f"{i.container:20} {i.image:40} "
                                 f"{'running' if i.running else 'stopped'}")


# --- lifecycle -----------------------------------------------------------

def helper(inst: Instance, script: str,
           extra_mounts: list[tuple[str, str]] | None = None) -> str:
    """Run a root shell against n8n's data dir, reusing the n8n image itself
    so no extra image has to be present."""
    args = ["docker", "run", "--rm", "-u", "0:0", "-v", f"{inst.mount_spec}:/data"]
    for src, dst in (extra_mounts or []):
        args += ["-v", f"{src}:{dst}"]
    args += ["--entrypoint", "sh", inst.image, "-c", script]
    return sh(*args)


def is_running(inst: Instance) -> bool:
    if inst.offline:
        return False
    return sh("docker", "inspect", inst.container, "--format",
              "{{.State.Running}}", check=False) == "true"


def stop(inst: Instance) -> bool:
    """Returns whether we actually stopped it, so the caller knows to restart."""
    if inst.offline or not is_running(inst):
        return False
    with Spinner("stopping n8n", "stopped n8n"):
        sh("docker", "stop", inst.container)
    return True


def start(inst: Instance) -> None:
    if inst.offline:
        return
    with Spinner("starting n8n", "started n8n"):
        sh("docker", "start", inst.container)


def wait_healthy(inst: Instance, timeout: int = 180) -> bool:
    """Poll n8n over HTTP in two stages.

    Deliberately not log-scraping: `docker logs` still holds the *previous*
    boot's "Editor is now accessible" line, so a tail match reports ready the
    instant the container starts and hides a genuinely failed startup.
    """
    if inst.offline:
        return True
    if not inst.host_port:
        warn("no published port — cannot verify health; check `docker logs` yourself")
        return False

    import urllib.error
    import urllib.request

    base = f"http://127.0.0.1:{inst.host_port}"

    def probe(path: str) -> int:
        try:
            with urllib.request.urlopen(base + path, timeout=3) as response:
                return response.status
        except urllib.error.HTTPError as e:
            return e.code
        except (urllib.error.URLError, OSError):
            return 0

    deadline = time.time() + timeout

    with Spinner("waiting for the database", "n8n healthy — editor serving") as spin:
        # Stage 1: database connected and migrations complete — the part that
        # matters after rewriting the database underneath n8n.
        while time.time() < deadline:
            if probe("/healthz/readiness") == 200:
                break
            if not is_running(inst):
                spin.failed = True
                spin.update("container exited during startup — check `docker logs`")
                return False
            time.sleep(1)
        else:
            spin.failed = True
            spin.update(f"not ready after {timeout}s — check "
                        f"`docker logs {inst.container}`")
            return False

        # Stage 2: the editor actually serving. It comes up a few seconds after
        # readiness, so reporting success at stage 1 alone overstates things.
        spin.update("waiting for the editor")
        while time.time() < deadline:
            if probe("/") < 400:
                return True
            time.sleep(1)

        spin.done = (f"database is live, but the editor did not answer on {base} "
                     "(normal behind a base path or proxy)")
        return True
