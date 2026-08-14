# n8n-warden — manual

Access control for self-hosted n8n Community Edition — projects, workflow and
credential sharing, folders, users and roles — by writing directly to n8n's
database.

Verified against **n8n 2.34.5** (SQLite). The six tables it touches are
byte-identical between 2.32.7 and 2.34.5.

```bash
./warden.py              # interactive menu
./warden.py doctor       # environment + health report
./warden.py selftest     # 32 checks against a synthetic DB — touches nothing real
./warden.py --help
```

Requires Python 3.9+ and `docker`. No pip install. `bcrypt` is used if present
(only for `set-password`) and is never required.

## Display

Colour, spinners and progress bars appear only on a terminal. Piped output,
`--json`, CI logs and `less` get plain text — nothing here can corrupt a machine
reader, and the self-test asserts it.

Spinners are used only where the tool genuinely blocks: stopping n8n, waiting
for it to come back, taring gigabytes, vacuuming. Each shows elapsed time, so a
long wait is visibly progressing rather than possibly hung.

Disable with `--no-color`, `NO_COLOR=1`, or `TERM=dumb`. `N8NADM_NO_ANIMATION=1`
keeps colour but drops motion — useful over a slow SSH link.

## Layout

```
src/
  __main__.py           zipapp entry point
  n8n_warden/
    errors.py           the one expected-exception type
    config.py           constants and the n8n version facts we pin to
    console.py          terminal output and operator prompts
    db.py               SQLite/Postgres behind one interface
    docker.py           container discovery and lifecycle
    storage.py          snapshots, restore, writable workspace
    journal.py          mutation recording and row-level undo
    model.py            roles, invariants, schema/version compatibility
    queries.py          read-only views
    ops/                mutations, grouped by entity
      projects.py  users.py  workflows.py  credentials.py  folders.py
    selectors.py        selector expressions for bulk work
    audit.py            access matrix and orphan reporting
    runner.py           the write cycle every mutation passes through
    doctor.py           health report
    history.py          journal/snapshot views and undo
    ui.py               interactive menu
    cli.py              argument parsing and dispatch
    selftest.py         verification against a synthetic database
```

Dependencies run strictly downward — `ops/` never imports `ui`, and nothing
below `runner` knows a write cycle exists. Every function in `ops/` takes
`(db, batch, ...)` and records through the batch rather than writing directly,
so the package is undoable by construction.

### Distribution

The source is modular; the artefact is not. `zipapp` bundles it into one
executable file with no install step:

```bash
./build.sh                       # → warden.pyz (~48 KB, self-verifying)
scp warden.pyz you@host:
./warden.pyz doctor
```

`./warden.py` still works from a source checkout — it just prepends `src/`
to the path and calls the same entry point.

---

## Why not just run the UPDATE

The widely-circulated advice — "update `shared_workflow` to point at the other
project" — is written for n8n **1.x** and breaks in four ways on 2.x.

| What the old advice does                      | What actually happens on 2.34.5                                                                                                                        |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `SELECT u.role FROM "user" u`               | **Fails.** The column is now `roleSlug`, with a foreign key to a new `role` table.                                                           |
| Writes any string to`project_relation.role` | **Rejected** — that column now has an FK to `role.slug`.                                                                                      |
| Writes any string to`shared_workflow.role`  | **Silently accepted.** No FK guards it. `workflow:Owner` inserts fine and produces a workflow nobody can open.                                 |
| `cp database.sqlite backup.sqlite`          | **Captures a stale database.** The DB runs in WAL mode; this instance had 4 MB of recent data living only in `database.sqlite-wal`.            |
| Ignores`parentFolderId`                     | **Leaves a dangling reference.** A folder belongs to exactly one project, so a transferred workflow points at a folder the new owner cannot see. |

The last one is the quiet killer: nothing errors, the workflow just renders in a
folder that doesn't belong to the project. `warden.py` detects and repairs it
on every transfer.

---

## What it enforces

Five rules n8n's own code assumes but the schema does not enforce:

1. **Roles are read from the live `role` table**, never hardcoded — so a typo is
   caught rather than inserted.
2. **`PRAGMA foreign_keys=ON`.** SQLite ships FK enforcement *off*, so even the
   one constraint you do get is inert by default.
3. **Ownership is an update, not an insert.** Exactly one `workflow:owner` row
   per workflow, always.
4. **Cross-project folder repair** on every transfer (see `--folder-policy`).
5. **Credential co-dependency check** — reports which credentials the destination
   project cannot see, *before* the move.

Every write runs in one transaction, re-reads the invariants, and **rolls back
if the change would break any of them**.

---

## Safety model

```
snapshot → stop n8n → write → verify invariants → commit → push back → start → healthcheck
```

Two independent ways back:

- **`undo`** — the journal stores a full before-image of every row touched, so a
  batch reverts row-by-row. It also stores the *after* image, and **skips any row
  that no longer matches it** — if you edited a workflow in the UI after the
  batch, undo leaves it alone and tells you, rather than silently discarding
  your change.
- **`restore`** — a complete DB snapshot precedes every batch, so you can roll
  back wholesale even if the undo logic itself is wrong. Auto-pruned past 20.

Before any write, the tool checks n8n's own `migrations` table against the
version range it has been verified on, and asks before proceeding if the
instance has moved past it. `delete project` and `delete user` require typing
the name — `--yes` does not skip that, because their blast radius extends past
the thing you named.

State lives in `~/.n8n-warden/` (override with `N8N_WARDEN_HOME`).

Two details worth knowing, because getting them wrong corrupts the database:

- **The write-back is atomic.** Stale `-wal`/`-shm` files are deleted and the new
  file swapped in a *single* step. A leftover WAL would be replayed over the
  fresh database on next boot.
- **The healthcheck is a real HTTP probe**, in two stages: `/healthz/readiness`
  (database connected, migrations complete) then `/` (editor serving). It does
  not scrape `docker logs` — the log tail still holds the *previous* boot's
  "Editor is now accessible" line, which reports ready instantly and hides a
  failed startup.

No root needed. The database is pulled out with `docker cp`, edited locally, and
pushed back — `/var/lib/docker/volumes/` is root-only, so reading it in place
would mean `sudo` for every operation. Works identically for named volumes, bind
mounts, and raw files (`--db-file`).

---

## Commands

```bash
# inspect            (--json on any listing for machine-readable output)
n8n-warden ls projects|users|workflows|credentials|folders|matrix|orphans
n8n-warden export -o access.json

# team projects      (the operations the REST API refuses on CE)
n8n-warden project create "Ops Team" --description "shared automation"
n8n-warden project rename "Ops Team" --name "Platform"
n8n-warden project members "Ops Team"
n8n-warden project add-member "Ops Team" --user bob@co.com --role project:editor
n8n-warden project remove-member "Ops Team" --user bob@co.com
n8n-warden project delete "Ops Team" --reassign-to "Platform"

# users
n8n-warden user create bob@co.com --first Bob --last Member
n8n-warden user set-role bob@co.com --role global:admin
n8n-warden user disable|enable|clear-mfa|clear-password bob@co.com
n8n-warden user delete bob@co.com --reassign-to "Ops Team"

# folders            (see "Sharing a folder" below — n8n cannot share folders)
n8n-warden folder list
n8n-warden folder share COST_REPORT --to bob@co.com --with-credentials
n8n-warden folder share COST_REPORT --to "Ops Team" --role workflow:editor
n8n-warden folder unshare COST_REPORT --to bob@co.com
n8n-warden folder transfer COST_REPORT --to "Ops Team"   # moves ownership
n8n-warden folder create Reports --project "Ops Team" [--parent <id>]
n8n-warden folder rename <id> --name Archive
n8n-warden folder delete <id> [--recursive]

# single entity      (kind is wf | cred)
n8n-warden transfer wf J23uyafuyc40IC2O --to "Ops Team"
n8n-warden share    wf J23uyafuyc40IC2O --to "Ops Team" --role workflow:editor
n8n-warden unshare  cred abc123         --to "Ops Team"

# bulk — dry run by default, --apply to write
n8n-warden bulk "wf:tag=prod" transfer --to "Ops Team" --apply

# health and repair
n8n-warden doctor
n8n-warden doctor --fix all [--dry-run]
n8n-warden doctor --fix dup-owner,cross-project-folder

# recovery
n8n-warden history
n8n-warden undo [--yes]
```

`--dry-run` works on every write command and shows the row-level plan without
touching anything. Folders, and picking through entities interactively, live in
the menu (`n8n-warden` with no arguments).

### Sharing a folder

**n8n has no folder sharing.** `folder.projectId` is a single value and there is
no `shared_folder` table — a folder belongs to exactly one project. So:

- **`folder share`** shares every workflow filed inside the folder. The folder
  itself stays where it is, and the recipient sees those workflows at their own
  project root, because a folder is only visible inside the project that owns it.
- **`folder transfer`** moves the folder subtree *and* its workflows to another
  project. Ownership changes; the structure survives.

Recursive by default. A folder holding one workflow directly may hold many more
in subfolders, and sharing only the direct children is rarely what anyone means
— pass `--direct-only` for the narrow reading.

`--with-credentials` also shares the credentials those workflows reference.
Without it the recipient can open the workflows but not run them, which is the
most common surprise when handing work to a colleague.

Destinations accept a **user's email** as well as a project name or id; an email
resolves to that user's personal project.

### Repair

`doctor` labels each issue with a stable kind, and `--fix` takes those kinds.
Repairs are ordinary mutations — snapshotted, transactional, journalled and
undoable like any other write.

| Kind | Repair |
|---|---|
| `dup-owner` | keep the earliest owner, demote the rest to editor (nobody loses access) |
| `orphan-share` | adopt into the instance owner's personal project |
| `cross-project-folder` | clear the dangling folder reference |
| `folder-parent-project` | detach the folder from its foreign-project parent |
| `unknown-role` | **report only** — the intended role cannot be inferred |
| `personal-owner-count` | **report only** — needs a human to say who owns it |

The last two have no fixer on purpose. A repair that guesses is worse than one
that does not exist.

### Selectors

```
wf:*            wf:active         wf:archived        wf:orphan
wf:name~^Slack  wf:tag=prod       wf:project=Ops     wf:owner=a@b.com
cred:*          cred:type=slackApi                   cred:name~aws
```

### `--folder-policy`

A folder belongs to exactly one project, so a workflow's folder cannot survive a
move. Three defensible answers:

| Policy                 | Behaviour                                                       |
| ---------------------- | --------------------------------------------------------------- |
| `root` *(default)* | Clear the folder; the workflow lands at the destination's root. |
| `mirror`             | Recreate the folder path inside the destination project.        |
| `block`              | Refuse the transfer if the workflow is in a folder.             |

---

## Worked example

```
$ ./warden.py transfer wf J23uyafuyc40IC2O --to Ops --dry-run

  transfer wf J23uyafuyc40IC2O → 'Ops'
  op      table            key
  ──────────────────────────────────────────────────────────────────
  DELETE  shared_workflow  workflowId=J23uy…, projectId=K7v314…
  INSERT  shared_workflow  workflowId=J23uy…, projectId=30DFuu…
  UPDATE  workflow_entity  id=J23uyafuyc40IC2O
  · 2 credential(s) not available to the destination: N8N Cost Bot, AWS (IAM) account
  · transferred 'Slack Cost Bot — Lambda Direct Invoke'

  dry run — nothing written
```

Drop `--dry-run` to apply. Then `./warden.py undo` puts it back.

---

## Why SQL and not n8n's own API

There is an official `@n8n/cli` npm package with `project create`, `project
add-member` and `workflow transfer --project` — on paper, exactly this tool's
job. **It does not work on unlicensed Community Edition.** It is a thin wrapper
over the REST API, and those endpoints are licence-gated server-side.

Measured on a clean n8n 2.34.5 CE instance:

| Action | via REST API | via this tool |
|---|---|---|
| Create team project | `403 Plan lacks license for this feature` | works |
| Share workflow with project | `403 Plan lacks license for this feature` | works |

So the API route is real, supported, and blocked. That is the standing reason
this tool writes SQL — please don't "helpfully" migrate it onto the API without
re-testing the gate first.

Use the API, not this tool, for anything **not** gated: workflow and credential
CRUD, activate/deactivate, tags, variables, and export/import all work fine on
CE and are better done through supported interfaces.

## What actually takes effect

n8n's licence gate sits on the **mutation endpoints and the UI**, not on
permission resolution. Access is resolved by reading `project_relation` and
`shared_workflow` at request time, and that path has no licence check in it.

Verified end-to-end on CE (member created purely via DB writes, never touched
the UI):

- logs in, sees the team project, sees the workflow it owns
- receives 8 scopes including `workflow:update`, `workflow:execute`, `workflow:delete`
- an unrelated workflow correctly returns `404` — authorisation is still enforced

Meanwhile the same server reports `enterprise.sharing = false` and
`enterprise.projects.team.limit = 0` to the browser. **Access works; buttons
don't.** Managing those shares is the job this CLI takes over.

**One confirmed exception.** `WorkflowSharingService.getSharedWorkflowIdsForScopes()`
branches on the sharing licence and collapses to owner-only when unlicensed. It
backs the **public API execution listing**, so `/api/v1/executions` stays
owner-filtered no matter what rows exist. Editor access and the workflow list
are unaffected. Enforcement is per-endpoint rather than central, so treat this
as "verified for these surfaces on 2.34.5", not a blanket guarantee.

## Scope and limits

- **Postgres** is detected and supported through `psycopg2` if installed, but has
  not been exercised against a live Postgres n8n. SQLite is the tested path.
- **Licence checks are not touched.** This writes rows through the documented
  schema. It does not patch the n8n binary, forge a certificate, or execute any
  `.ee.`-licensed code — which is the line that matters, since files marked
  `.ee.` are carved out of the Sustainable Use Licence under separate
  proprietary terms.
- **No official n8n position exists** on managing these tables directly. A
  targeted search of docs, GitHub issues and staff forum posts found neither
  blessing nor prohibition. Internal business use of self-hosted CE *is*
  clearly permitted by the licence. If this matters for compliance, ask n8n
  rather than relying on inferred permission.
- **Upgrade fragility is the real risk.** These tables are internal
  implementation details with no stability contract. The tool reads n8n's own
  `migrations` table and warns when the instance has moved past the verified
  range — re-verify after every upgrade.
- **Restoring a Postgres dump** is a manual `psql` step; the tool will tell you
  where the dump is rather than guess at your setup.
- `set-password` needs `bcrypt`. Without it, clear the password instead and use
  n8n's own invite flow from Settings → Users.
