---
name: n8n-warden
description: Use when managing a self-hosted n8n Community Edition instance — creating team projects, sharing workflows or credentials, managing users/folders, upgrading n8n or nginx, pruning executions, or recovering with snapshots/undo. Also use when the user says "warden", asks to run access-control operations the n8n CE API refuses (403 licence errors), or needs to inspect who can access what.
---

# n8n-warden

CLI that manages self-hosted n8n CE by writing directly to its database —
projects, sharing, users, folders — because those REST endpoints are
licence-gated on Community Edition. Run as `warden` (installed),
`./n8n-warden.pyz` (bundle), or `./warden.py` (source checkout).

**Safety model:** every write takes a snapshot first, runs inside a
transaction, is checked against invariants, journalled row-by-row, and the
container is health-checked after restart. Prefer `--dry-run` first on any
write; it prints the exact row-level plan.

## Quick reference

| Task | Command |
|---|---|
| Interactive menu | `warden` |
| Health + drift report | `warden doctor` (repair: `warden doctor --fix all`) |
| List things | `warden ls projects\|users\|workflows\|credentials\|folders\|matrix\|orphans` (`--json` for machine output) |
| Team project | `warden project create "Ops"` / `rename` / `delete` / `add-member --user a@b.com --role project:editor` |
| Users | `warden user create a@b.com` / `set-role` / `disable` / `clear-mfa` / `delete --reassign-to <project>` |
| Share / move one thing | `warden share\|unshare\|transfer wf\|cred <id> --to <project-or-email>` |
| Bulk by selector | `warden bulk "<selector>" share\|unshare\|transfer --to <dest> --apply` — **without `--apply` it only previews**; `share` grants access, `transfer` moves ownership |
| Folder contents | `warden folder share <name> --to <dest> --with-credentials` |
| Manual snapshot | `warden snapshot <label>` |
| Revert last write | `warden undo` (specific: `warden undo <batch-id>`; list: `warden history`) |
| Full DB rollback | `warden restore [snapshot-prefix]` (newest if omitted) |
| Upgrade n8n / nginx | `warden upgrade n8n\|nginx\|both` (pins real version tags, never `latest`) |
| Fresh stack | `warden install [--nginx] [--dir PATH]` |
| Update warden itself | `warden update` |
| Reclaim disk | `warden prune` (report) then `warden prune --executions N --history N` |
| Offline DB file | any command plus `--db-file path/to/database.sqlite` |

Selectors: `wf:*` `wf:active` `wf:orphan` `wf:name~regex` `wf:tag=x`
`wf:project=x` `wf:owner=email` · `cred:*` `cred:type=slackApi` `cred:name~re`
`cred:project=x`. `--to` accepts a project id, name, or a user's email
(resolves to their personal project). `--yes` skips confirmations in scripts.

## Gotchas

- `project delete` / `user delete` require **retyping the name** — `--yes`
  deliberately does not skip that.
- Folders cannot be shared in n8n; `folder share` shares the workflows
  *inside* — pass `--with-credentials` or recipients can open but not run.
- `prune` cannot be undone with `undo`; take `--snapshot` first if unsure.
- `upgrade` needs the container to be compose-managed; after a big n8n jump,
  `doctor` may warn "newer than verified" — check schema, then bump
  `VERIFIED_*` in `src/n8n_warden/config.py`.
- Writes stop n8n briefly; batch work (bulk, multi-select in the menu) into
  one command so there is one snapshot and one restart.
