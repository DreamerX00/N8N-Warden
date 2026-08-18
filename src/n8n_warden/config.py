"""Constants, paths, and the schema/version facts the tool is pinned to.

Everything here is a statement about n8n's shape, deliberately kept in one
place so that supporting a new n8n release is an edit to this file rather than
an archaeology expedition through the codebase.
"""

from __future__ import annotations

import os
import string
from pathlib import Path

VERSION = "1.2.0"

# --- n8n container layout ------------------------------------------------
N8N_DIR = "/home/node/.n8n"
DB_NAME = "database.sqlite"
SIDECARS = (f"{DB_NAME}-wal", f"{DB_NAME}-shm")

# --- our own state -------------------------------------------------------
STATE_DIR = Path(os.environ.get("N8N_WARDEN_HOME", Path.home() / ".warden"))
SNAP_DIR = STATE_DIR / "snapshots"
JOURNAL = STATE_DIR / "journal.jsonl"
MAX_SNAPSHOTS = 2   # current + previous; older snapshots are deleted after each new one

# --- schema facts --------------------------------------------------------
# Tables this tool writes to, with their primary keys. Drives the undo journal:
# a before-image is worthless without knowing how to address the row again.
PK = {
    "user": ("id",),
    "project": ("id",),
    "project_relation": ("projectId", "userId"),
    "shared_workflow": ("workflowId", "projectId"),
    "shared_credentials": ("credentialsId", "projectId"),
    "folder": ("id",),
    "workflow_entity": ("id",),
    "credentials_entity": ("id",),
}

# Fast fail-fast fingerprint. Verified identical across n8n 2.32.7 and 2.34.5.
EXPECTED_COLUMNS = {
    "project": {"id", "name", "type"},
    "project_relation": {"projectId", "userId", "role"},
    "shared_workflow": {"workflowId", "projectId", "role"},
    "shared_credentials": {"credentialsId", "projectId", "role"},
    "folder": {"id", "name", "parentFolderId", "projectId"},
    "user": {"id", "email", "roleSlug"},
}

# n8n records its own migrations on boot. The trailing digits are a sortable
# timestamp, making the newest row a sharper compatibility signal than column
# presence — it also catches data-shape migrations that leave columns untouched
# but change what a value means.
VERIFIED_N8N = "2.34.5"
VERIFIED_MIGRATION = "AddSetupCompletedAtToAgents1785500832626"
VERIFIED_MIGRATION_TS = 1785500832626

# --- policy --------------------------------------------------------------
# A folder belongs to exactly one project, so a workflow's folder reference
# cannot survive a move between projects. Three defensible answers.
FOLDER_POLICIES = ("root", "mirror", "block")
DEFAULT_FOLDER_POLICY = "root"

NANOID_ALPHABET = string.ascii_letters + string.digits + "-_"
