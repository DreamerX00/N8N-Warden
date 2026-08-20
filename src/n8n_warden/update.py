"""Keeping things current: warden itself, and the n8n it manages.

Two update paths live here. `notice`/`self_update` track warden's own GitHub
releases (checked at most once a day, silently skipped offline). `upgrade_n8n`
moves a compose-managed n8n to the newest release, always writing a real
version tag into the compose file — `latest` hides which version is running
and turns every container recreate into a silent, unplanned upgrade.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

from .config import STATE_DIR, VERSION
from .console import assume_yes, confirm, dim, err, ok, say, step, warn
from .docker import Instance, sh, wait_healthy
from .errors import Fatal
from .storage import snapshot

WARDEN_REPO = "DreamerX00/N8N-Warden"
N8N_REPO = "n8n-io/n8n"
PYZ_URL = f"https://github.com/{WARDEN_REPO}/releases/latest/download/n8n-warden.pyz"
CACHE = STATE_DIR / "update-check.json"
CHECK_EVERY = 24 * 3600


# --- version facts ---------------------------------------------------------

def _parse(version: str) -> tuple:
    """'v1.10.0' → (1, 10, 0); anything unparseable sorts below everything."""
    try:
        return tuple(int(p) for p in version.lstrip("v").split("."))
    except (ValueError, AttributeError):
        return ()


def _github_latest(repo: str):
    def fetch() -> str:
        with urllib.request.urlopen(
                f"https://api.github.com/repos/{repo}/releases/latest",
                timeout=5) as response:
            return json.load(response).get("tag_name", "")
    return fetch


def _pick_nginx_tag(names) -> str:
    """Highest stable-line X.Y.Z-alpine tag. nginx encodes the branch in the
    minor number — even is stable, odd is the mainline development branch —
    so 'highest tag' alone would silently move the proxy onto mainline."""
    stable = [n for n in names
              if (m := re.fullmatch(r"\d+\.(\d+)\.\d+-alpine", n))
              and int(m.group(1)) % 2 == 0]
    return max(stable, key=lambda n: _parse(n[:-len("-alpine")]), default="")


def _hub_nginx() -> str:
    with urllib.request.urlopen(
            "https://registry.hub.docker.com/v2/repositories/library/nginx/tags"
            "?page_size=100&name=-alpine", timeout=5) as response:
        data = json.load(response)
    return _pick_nginx_tag(t["name"] for t in data.get("results", []))


def _cached(key: str, fetch, max_age: int = CHECK_EVERY) -> str | None:
    """Latest version by `fetch()`, hitting the network at most once per day.
    Returns None on any failure — an update check must never break the tool."""
    cache: dict = {}
    try:
        if CACHE.exists():
            cache = json.loads(CACHE.read_text())
            if key in cache and time.time() - CACHE.stat().st_mtime < max_age:
                return cache[key]
        cache[key] = fetch()
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache))
        return cache[key]
    except Exception:
        return cache.get(key)


def latest_warden(max_age: int = CHECK_EVERY) -> str | None:
    tag = _cached("warden", _github_latest(WARDEN_REPO), max_age)
    return tag.lstrip("v") if tag else None


def latest_n8n(max_age: int = CHECK_EVERY) -> str | None:
    # n8n tags releases as "n8n@X.Y.Z".
    tag = _cached("n8n", _github_latest(N8N_REPO), max_age)
    return tag.split("@")[-1] if tag else None


def latest_nginx(max_age: int = CHECK_EVERY) -> str | None:
    """Newest stable nginx as a pinned image tag, e.g. '1.30.4-alpine'."""
    return _cached("nginx", _hub_nginx, max_age) or None


def notice() -> None:
    """One line if a newer warden exists. Quiet in every failure mode."""
    if os.environ.get("N8N_WARDEN_NO_UPDATE_CHECK"):
        return
    latest = latest_warden()
    if latest and _parse(latest) > _parse(VERSION):
        say(dim(f"  ↑ warden {latest} is available (you run {VERSION}) — "
                "upgrade with: warden update"))


# --- warden self-update ----------------------------------------------------

def self_update() -> int:
    """Replace the running n8n-warden.pyz with the latest release, atomically."""
    latest = latest_warden(max_age=0)
    if not latest:
        raise Fatal("could not reach GitHub to check the latest release")
    if _parse(latest) <= _parse(VERSION):
        ok(f"warden is already up to date ({VERSION})")
        return 0

    target = Path(sys.argv[0]).resolve()
    if not target.exists() or not zipfile.is_zipfile(target):
        raise Fatal(f"warden {latest} is available, but this is not the .pyz "
                    "distribution — update with `git pull` instead")

    step(f"downloading warden {latest}")
    staged = target.with_name(target.name + ".new")
    try:
        with urllib.request.urlopen(PYZ_URL, timeout=60) as response, \
                open(staged, "wb") as fh:
            shutil.copyfileobj(response, fh)
        if not zipfile.is_zipfile(staged):
            raise Fatal("downloaded file is not a valid archive — aborted, "
                        "nothing replaced")
        mode = target.stat().st_mode
        staged.replace(target)          # atomic on the same filesystem
        target.chmod(mode)
    finally:
        staged.unlink(missing_ok=True)
    ok(f"updated {target.name}: {VERSION} → {latest}")
    return 0


# --- compose templates -------------------------------------------------------
# Both variants pin real version tags on purpose: `latest` turns every
# container recreate into a silent, unplanned upgrade. `warden upgrade`
# is what moves the pins forward. Data lives in the named volume `n8n_data`,
# so upgrades and reinstalls keep workflows, credentials and the encryption
# key. `${VAR:-default}` values can be overridden from a `.env` file.

_N8N_SERVICE = """\
  n8n:
    image: docker.n8n.io/n8nio/n8n:@N8N_VERSION@
    container_name: n8n
    restart: unless-stopped
    stop_grace_period: 30s      # let running executions finish on shutdown
    ports:
      - "@PORTS@"
    environment:
      - N8N_HOST=${N8N_HOST:-localhost}
      - N8N_PORT=5678
      - N8N_PROTOCOL=${N8N_PROTOCOL:-http}
      - N8N_EDITOR_BASE_URL=${N8N_URL:-@URL@}
      - WEBHOOK_URL=${N8N_URL:-@URL@}
      - GENERIC_TIMEZONE=${GENERIC_TIMEZONE:-UTC}
      - TZ=${TZ:-UTC}
      - N8N_RUNNERS_ENABLED=true
      - N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS=true
      - N8N_SECURE_COOKIE=${N8N_SECURE_COOKIE:-false}   # set true behind HTTPS
      - DB_SQLITE_POOL_SIZE=10
@EXTRA_ENV@    volumes:
      - n8n_data:/home/node/.n8n
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://127.0.0.1:5678/healthz"]
      interval: 30s
      timeout: 5s
      retries: 5
      start_period: 60s
"""

_HEADER = """\
# n8n — generated by warden.
# Version tags are pinned deliberately (`latest` would upgrade you on every
# recreate); `warden upgrade` checks for new releases and moves the pins.
# Data persists across upgrades in the named volume `n8n_data`. Prefer a host
# directory? Swap the volume line for `- /your/path:/home/node/.n8n` — that
# also enables warden's faster in-place database access.

services:
"""

_VOLUMES = """
volumes:
  n8n_data:
"""

COMPOSE = (_HEADER
           + _N8N_SERVICE.replace("@PORTS@", "5678:5678")
                         .replace("@URL@", "http://localhost:5678")
                         .replace("@EXTRA_ENV@", "")
           + _VOLUMES)

COMPOSE_NGINX = (_HEADER
                 + _N8N_SERVICE
                 .replace("@PORTS@", "127.0.0.1:5678:5678")  # warden's health
                 # probe still reaches n8n, but only nginx faces the network
                 .replace("@URL@", "http://localhost")
                 .replace("@EXTRA_ENV@",
                          "      - N8N_PROXY_HOPS=1        "
                          "# trust one proxy for client IPs\n")
                 + """\

  nginx:
    image: nginx:@NGINX_VERSION@
    container_name: n8n-nginx
    restart: unless-stopped
    ports:
      - "80:80"
    volumes:
      - ./nginx.conf:/etc/nginx/conf.d/default.conf:ro
    depends_on:
      n8n:
        condition: service_healthy
"""
                 + _VOLUMES)

NGINX_CONF = """\
# n8n behind nginx — generated by warden.

# The editor needs WebSockets: without the Upgrade/Connection dance the UI
# loads but live execution updates and collaboration silently break.
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 80;
    server_name _;

    client_max_body_size 64m;          # workflow imports and binary uploads

    # Resolve `n8n` through Docker's DNS at request time, not once at boot.
    # A static proxy_pass caches the container IP forever — after an n8n
    # upgrade recreates the container, that stale IP means 502s until nginx
    # is restarted too.
    resolver 127.0.0.11 valid=10s ipv6=off;

    location / {
        set $upstream http://n8n:5678;
        proxy_pass $upstream;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_buffering off;           # SSE/push updates must stream
        proxy_read_timeout 3600s;      # long-running executions
        proxy_send_timeout 3600s;

        # n8n's session cookies overflow nginx's default 4k/8k header
        # buffers, which shows up as intermittent 502s on login.
        proxy_buffer_size 16k;
        proxy_buffers 8 16k;
    }
}
"""


def install(nginx: bool = False, directory: str = ".", start: bool = True) -> int:
    """Write a fresh pinned compose stack (and nginx.conf for the proxy
    variant), then optionally bring it up and health-check it."""
    dest = Path(directory).expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    compose_path = dest / ("docker-compose-nginx.yml" if nginx
                           else "docker-compose.yml")
    conf_path = dest / "nginx.conf"
    for existing in ([compose_path, conf_path] if nginx else [compose_path]):
        if existing.exists():
            raise Fatal(f"{existing} already exists — "
                        "use `warden upgrade` to move it forward")

    n8n_ver = latest_n8n(max_age=0)
    if not n8n_ver:
        raise Fatal("could not fetch the latest n8n release from GitHub")
    text = (COMPOSE_NGINX if nginx else COMPOSE).replace("@N8N_VERSION@", n8n_ver)

    nginx_ver = None
    if nginx:
        nginx_ver = latest_nginx(max_age=0)
        if not nginx_ver:
            raise Fatal("could not fetch the latest stable nginx tag "
                        "from Docker Hub")
        text = text.replace("@NGINX_VERSION@", nginx_ver)
        conf_path.write_text(NGINX_CONF)
    compose_path.write_text(text)

    say()
    ok(f"wrote {compose_path.name}" + (" and nginx.conf" if nginx else ""))
    step(f"n8n pinned to {n8n_ver}"
         + (f", nginx pinned to {nginx_ver} (stable line)" if nginx else ""))

    if not start:
        say(dim(f"\n  start it with: docker compose -f {compose_path} up -d"))
        return 0
    if not assume_yes() and not confirm("start the stack now?", True):
        say(dim(f"  start it later with: docker compose -f {compose_path} up -d"))
        return 0

    _compose({"file": str(compose_path)}, "up", "-d")
    inst = Instance(container="n8n", image=f"docker.n8n.io/n8nio/n8n:{n8n_ver}",
                    running=True, host_port="5678")
    wait_healthy(inst, timeout=300)
    return 0


# --- n8n upgrade (compose, pinned tag) --------------------------------------

def pin_image(text: str, repo: str, version: str) -> str:
    """Rewrite every `image: <repo>[:tag]` line in a compose file to a pinned
    version tag. Raises if no line matches — a compose file that references the
    image through a variable must be edited by hand, not guessed at."""
    pattern = re.compile(rf"(image:\s*{re.escape(repo)})(:[^\s#]+)?(?=\s|$)")
    if not pattern.search(text):
        raise Fatal(f"no `image: {repo}` line found in the compose file — "
                    "is the tag set through a variable? pin it by hand")
    return pattern.sub(rf"\g<1>:{version}", text)


def _compose_of(container: str) -> dict | None:
    """Compose project facts from the container's labels, or None."""
    raw = sh("docker", "inspect", container, "--format",
             "{{json .Config.Labels}}", check=False)
    try:
        labels = json.loads(raw) or {}
    except json.JSONDecodeError:
        return None
    files = labels.get("com.docker.compose.project.config_files", "")
    service = labels.get("com.docker.compose.service", "")
    if not files or not service:
        return None
    file = files.split(",")[0]
    workdir = labels.get("com.docker.compose.project.working_dir", "")
    if not Path(file).is_absolute():
        file = str(Path(workdir) / file)
    return {"file": file, "service": service}


def _compose(info: dict, *args: str) -> str:
    if sh("docker", "compose", "version", check=False):
        return sh("docker", "compose", "-f", info["file"], *args)
    if shutil.which("docker-compose"):
        return sh("docker-compose", "-f", info["file"], *args)
    raise Fatal("neither `docker compose` nor `docker-compose` is available")


def upgrade_n8n(inst: Instance, dry_run: bool = False) -> int:
    """Upgrade a compose-managed n8n to the newest release, pinned.

    snapshot → pin tag in compose file → pull → up → health. Any failure
    reverts the compose file and recreates the old container; the snapshot
    covers the one thing compose cannot undo — n8n's forward-only database
    migrations.
    """
    if inst.offline:
        raise Fatal("no container here — pointed at a database file")

    latest = latest_n8n(max_age=0)
    if not latest:
        raise Fatal("could not fetch the latest n8n release from GitHub")
    if inst.version == latest:
        ok(f"n8n is already at the latest release ({latest})")
        return 0

    compose = _compose_of(inst.container)
    if not compose:
        raise Fatal(f"{inst.container} is not compose-managed — recreate it "
                    f"yourself with a pinned image tag "
                    f"(newest release: {latest})")
    path = Path(compose["file"])
    if not path.exists():
        raise Fatal(f"compose file not found: {path}\n"
                    "      the container remembers where it was created from — "
                    "if the project moved, run `docker compose up -d` from its "
                    "new home once, then retry")

    repo = inst.image.rsplit(":", 1)[0] if ":" in inst.image.split("/")[-1] \
        else inst.image
    original = path.read_text()
    pinned = pin_image(original, repo, latest)

    say()
    step(f"n8n {inst.version} → {latest}  ({repo}:{latest})")
    step(f"compose file: {path}")
    if dry_run:
        say(dim("\n  dry run — nothing changed"))
        return 0

    warn("n8n migrates its database forward on boot — going back afterwards "
         "needs the snapshot taken now")
    if not assume_yes() and not confirm("upgrade?", False):
        raise Fatal("cancelled")

    snap = snapshot(inst, f"pre-upgrade-{inst.version}-to-{latest}")
    ok(f"snapshot {snap.name}")

    path.write_text(pinned)
    try:
        step("pulling the new image")
        _compose(compose, "pull", compose["service"])
        step("recreating the container")
        _compose(compose, "up", "-d", compose["service"])
        if not wait_healthy(inst, timeout=600):
            raise Fatal("n8n did not come up healthy on the new version")
    except Exception:
        err("upgrade failed — reverting the compose file and container")
        path.write_text(original)
        try:
            _compose(compose, "up", "-d", compose["service"])
            wait_healthy(inst)
        except Exception:
            pass
        warn("if the database was already migrated, roll it back with: "
             "warden restore")
        raise

    ok(f"n8n upgraded to {latest} (pinned as {repo}:{latest})")
    inst.version, inst.image = latest, f"{repo}:{latest}"
    return 0


def upgrade_nginx(inst: Instance, dry_run: bool = False) -> int:
    """Pin the compose file's nginx to the newest stable release and recreate.

    No snapshot: nginx holds no state — a failure reverts the compose file and
    brings the old proxy back, and n8n's data is never in play.
    """
    if inst.offline:
        raise Fatal("no container here — pointed at a database file")

    latest = latest_nginx(max_age=0)
    if not latest:
        raise Fatal("could not fetch the latest stable nginx tag from Docker Hub")

    compose = _compose_of(inst.container)
    if not compose:
        raise Fatal(f"{inst.container} is not compose-managed — "
                    f"pin nginx:{latest} by hand")
    path = Path(compose["file"])
    if not path.exists():
        raise Fatal(f"compose file not found: {path}")

    original = path.read_text()
    current = re.search(r"image:\s*nginx:(\S+)", original)
    if not current:
        raise Fatal(f"no `image: nginx` line in {path.name} — this stack has "
                    "no nginx service (install one with `warden install --nginx`)")
    if current.group(1) == latest:
        ok(f"nginx is already at the newest stable release ({latest})")
        return 0

    say()
    step(f"nginx {current.group(1)} → {latest}  (stable line)")
    step(f"compose file: {path}")
    if dry_run:
        say(dim("\n  dry run — nothing changed"))
        return 0
    if not assume_yes() and not confirm("upgrade nginx?", True):
        raise Fatal("cancelled")

    path.write_text(pin_image(original, "nginx", latest))
    try:
        _compose(compose, "pull")
        _compose(compose, "up", "-d")
    except Exception:
        err("nginx upgrade failed — reverting the compose file and container")
        path.write_text(original)
        try:
            _compose(compose, "up", "-d")
        except Exception:
            pass
        raise
    ok(f"nginx upgraded to {latest} (pinned as nginx:{latest})")
    return 0


def upgrade(inst: Instance, target: str = "n8n", dry_run: bool = False) -> int:
    """The three-way entry point: n8n, nginx, or both."""
    if target in ("n8n", "both"):
        upgrade_n8n(inst, dry_run=dry_run)
    if target in ("nginx", "both"):
        upgrade_nginx(inst, dry_run=dry_run)
    return 0
