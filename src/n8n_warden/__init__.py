"""warden — access control for self-hosted n8n Community Edition.

Manages projects, workflow and credential sharing, folders, users and roles by
writing directly to n8n's database, because those endpoints are licence-gated
on Community Edition (`403 Plan lacks license for this feature`).

Layer map, bottom up:

    errors      the one expected-exception type
    config      constants and the n8n version facts we are pinned to
    console     terminal output and operator prompts
    db          SQLite/Postgres access behind one interface
    docker      container discovery and lifecycle
    storage     snapshots, restore, and the writable workspace
    journal     mutation recording and row-level undo
    model       roles, invariants, schema and version compatibility
    queries     read-only views
    ops/        mutations, grouped by entity
    selectors   selector expressions for bulk work
    audit       access matrix and orphan reporting
    runner      the write cycle every mutation passes through
    doctor      health report
    history     journal/snapshot views and undo
    update      warden self-update and pinned n8n upgrades
    ui          interactive menu
    cli         argument parsing and dispatch
    selftest    verification against a synthetic database
"""

from .config import VERSION

__version__ = VERSION
__all__ = ["VERSION", "__version__"]
