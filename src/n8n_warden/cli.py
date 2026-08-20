"""Argument parsing and command dispatch."""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from datetime import datetime
from pathlib import Path

from . import ops
from .audit import access_matrix, export_access, orphans
from .config import DEFAULT_FOLDER_POLICY, FOLDER_POLICIES, VERSION
from .console import (confirm, confirm_typed, err, ok, say, set_assume_yes,
                      set_color, step, table)
from .doctor import doctor, repair
from .docker import choose_instance
from .errors import Fatal
from .history import find_undoable, last_undoable, revert, show_history
from .maintenance import disk_report, prune
from .queries import (credentials, folder_path, folder_workflows, folders,
                      personal_project, projects, users, workflows)
from .runner import apply_change
from .selectors import resolve
from .storage import Workspace, list_snapshots, restore, snapshot
from .ui import interactive
from .update import install, notice, self_update, upgrade

EPILOG = textwrap.dedent("""\
    examples
      warden                                        interactive menu
      warden doctor                                 health + drift report
      warden install --nginx                        fresh pinned stack behind a proxy
      warden upgrade both                           n8n + nginx to newest pinned tags
      warden ls workflows
      warden transfer wf w_abc123 --to "Ops Team"
      warden share wf w_abc123 --to "Ops Team" --role workflow:editor
      warden bulk "wf:tag=prod" transfer --to "Ops Team" --apply
      warden snapshot before-cleanup                manual safety snapshot
      warden undo                                   revert the last batch
      warden restore                                roll back to the newest snapshot
    """)

LISTINGS = {
    "projects": (projects, ["id", "name", "type", "members", "workflows", "credentials"]),
    "users": (users, ["id", "email", "roleSlug", "disabled", "pending"]),
    "workflows": (workflows, ["id", "name", "project", "folder", "active", "shares"]),
    "credentials": (credentials, ["id", "name", "type", "project", "shares"]),
    "folders": (folders, ["id", "name", "project", "parentFolderId"]),
    "matrix": (access_matrix, ["email", "global", "workflows", "credentials", "projects"]),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="warden",
        description="Access control for self-hosted n8n — "
                    "projects, sharing, folders, users.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG)
    parser.add_argument("--version", action="version", version=f"warden {VERSION}")
    parser.add_argument("--container", help="n8n container name (auto-detected)")
    parser.add_argument("--db-file", help="operate on a SQLite file directly, no docker")
    # Accepted before or after the subcommand — see `_strip_global_flags`.
    parser.add_argument("--yes", "-y", action="store_true", help="skip confirmations")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable output for read commands")
    parser.add_argument("--no-color", action="store_true",
                        help="disable colour and animation "
                             "(also honours NO_COLOR)")

    sub = parser.add_subparsers(dest="cmd")

    health = sub.add_parser("doctor", help="environment and health report")
    health.add_argument("--fix", metavar="KIND",
                        help="repair issues: 'all' or a comma-separated list of "
                             "kinds shown by doctor")
    health.add_argument("--dry-run", action="store_true",
                        help="with --fix, show the plan without writing")

    sub.add_parser("selftest", help="verify the tool against a synthetic database")

    undo_cmd = sub.add_parser("undo", help="revert a write batch")
    undo_cmd.add_argument("id", nargs="?",
                          help="batch id or prefix (default: the last batch)")

    sub.add_parser("history", help="show snapshots and the undo journal")

    snap_cmd = sub.add_parser("snapshot", help="take a manual snapshot now")
    snap_cmd.add_argument("label", nargs="?", default="manual",
                          help="name to remember it by")

    restore_cmd = sub.add_parser("restore",
                                 help="roll the whole database back to a snapshot")
    restore_cmd.add_argument("name", nargs="?",
                             help="snapshot name or prefix (default: newest; "
                                  "see `warden history`)")

    sub.add_parser("update", help="update warden itself to the latest release")

    upgrade_cmd = sub.add_parser(
        "upgrade", help="upgrade n8n and/or nginx to the newest release, "
                        "pinned to a real tag",
        description="Rewrites the compose file to the newest version tags "
                    "(never `latest`), pulls, recreates, and health-checks — "
                    "n8n gets a snapshot and automatic revert on failure; "
                    "nginx tracks the stable line.")
    upgrade_cmd.add_argument("target", nargs="?", choices=["n8n", "nginx", "both"],
                             default="n8n")
    upgrade_cmd.add_argument("--dry-run", action="store_true")

    install_cmd = sub.add_parser(
        "install", help="write a fresh pinned compose stack and start it",
        description="Generates docker-compose.yml (or docker-compose-nginx.yml "
                    "plus nginx.conf with --nginx) with the newest pinned "
                    "version tags and a named data volume, so every later "
                    "upgrade keeps your workflows and credentials.")
    install_cmd.add_argument("--nginx", action="store_true",
                             help="front n8n with an nginx reverse proxy on :80")
    install_cmd.add_argument("--dir", default=".",
                             help="where to write the files (default: here)")
    install_cmd.add_argument("--no-start", action="store_true",
                             help="write the files without starting the stack")

    _add_project_commands(sub)
    _add_user_commands(sub)
    _add_folder_commands(sub)

    listing = sub.add_parser("ls", help="list entities")
    listing.add_argument("what", choices=[*LISTINGS, "orphans"])

    for name, help_text in (("transfer", "move ownership"),
                            ("share", "grant access"),
                            ("unshare", "revoke access")):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("kind", choices=["wf", "cred"])
        cmd.add_argument("id")
        cmd.add_argument("--to", required=True, help="destination project (id or name)")
        cmd.add_argument("--role")
        cmd.add_argument("--folder-policy", choices=FOLDER_POLICIES,
                         default=DEFAULT_FOLDER_POLICY)
        cmd.add_argument("--dry-run", action="store_true")

    bulk = sub.add_parser("bulk", help="act on a selector")
    bulk.add_argument("selector")
    bulk.add_argument("action", choices=["transfer", "share", "unshare"])
    bulk.add_argument("--to", required=True)
    bulk.add_argument("--role")
    bulk.add_argument("--folder-policy", choices=FOLDER_POLICIES,
                      default=DEFAULT_FOLDER_POLICY)
    bulk.add_argument("--apply", action="store_true",
                      help="write (default is dry run)")

    export = sub.add_parser("export", help="write the full access map to JSON")
    export.add_argument("-o", "--out")

    prune_cmd = sub.add_parser(
        "prune", help="trim execution history and workflow versions",
        description="Execution data is typically 99%% of an n8n database. "
                    "Trimming it is what keeps every other operation fast.")
    prune_cmd.add_argument("--executions", type=int, metavar="N",
                           help="keep the newest N executions per workflow")
    prune_cmd.add_argument("--history", type=int, metavar="N",
                           help="keep the newest N saved versions per workflow "
                                "(a workflow's active version is always kept)")
    prune_cmd.add_argument("--no-vacuum", action="store_true",
                           help="skip reclaiming the freed space")
    prune_cmd.add_argument("--snapshot", action="store_true",
                           help="snapshot first (slow at multi-GB sizes)")
    prune_cmd.add_argument("--dry-run", action="store_true")

    return parser


def _add_project_commands(sub) -> None:
    """Team projects. The tool's headline operation, and the one the REST API
    refuses on Community Edition."""
    project = sub.add_parser("project", help="manage team projects")
    actions = project.add_subparsers(dest="action", required=True)

    create = actions.add_parser("create", help="create a team project")
    create.add_argument("name")
    create.add_argument("--description", default="")

    rename = actions.add_parser("rename", help="rename a project")
    rename.add_argument("project")
    rename.add_argument("--name", required=True)

    delete = actions.add_parser("delete", help="delete a team project")
    delete.add_argument("project")
    delete.add_argument("--reassign-to",
                        help="project to receive anything it owns")

    members = actions.add_parser("members", help="list members")
    members.add_argument("project")

    add = actions.add_parser("add-member", help="add or re-role a member")
    add.add_argument("project")
    add.add_argument("--user", required=True, help="email")
    add.add_argument("--role", default="project:editor")

    remove = actions.add_parser("remove-member", help="remove a member")
    remove.add_argument("project")
    remove.add_argument("--user", required=True, help="email")

    for cmd in (create, rename, delete, add, remove):
        cmd.add_argument("--dry-run", action="store_true")


def _add_user_commands(sub) -> None:
    user = sub.add_parser("user", help="manage users")
    actions = user.add_subparsers(dest="action", required=True)

    create = actions.add_parser("create", help="create a user and personal project")
    create.add_argument("email")
    create.add_argument("--first", default="")
    create.add_argument("--last", default="")
    create.add_argument("--role", default="global:member")

    delete = actions.add_parser("delete", help="delete a user")
    delete.add_argument("email")
    delete.add_argument("--reassign-to", help="project to receive anything they own")

    set_role = actions.add_parser("set-role", help="set the global role")
    set_role.add_argument("email")
    set_role.add_argument("--role", required=True)

    disable = actions.add_parser("disable", help="disable sign-in")
    disable.add_argument("email")
    enable = actions.add_parser("enable", help="re-enable sign-in")
    enable.add_argument("email")

    mfa = actions.add_parser("clear-mfa", help="clear two-factor authentication")
    mfa.add_argument("email")

    clear_pw = actions.add_parser("clear-password",
                                  help="clear the password, forcing n8n's invite flow")
    clear_pw.add_argument("email")

    for cmd in (create, delete, set_role, disable, enable, mfa, clear_pw):
        cmd.add_argument("--dry-run", action="store_true")


def _add_folder_commands(sub) -> None:
    """Folders.

    n8n cannot share a folder — `folder.projectId` is a single value and there
    is no shared_folder table — so `share` shares the workflows inside it.
    """
    folder = sub.add_parser(
        "folder", help="manage folders and share their contents",
        description="n8n has no folder sharing: a folder belongs to exactly "
                    "one project. 'share' therefore shares every workflow "
                    "inside the folder; 'transfer' moves the folder itself.")
    actions = folder.add_subparsers(dest="action", required=True)

    listing = actions.add_parser("list", help="show the folder tree")

    share = actions.add_parser("share", help="share the workflows inside a folder")
    share.add_argument("folder")
    share.add_argument("--to", required=True,
                       help="destination project name/id, or a user's email")
    share.add_argument("--role", default="workflow:editor")
    share.add_argument("--with-credentials", action="store_true",
                       help="also share the credentials those workflows need, "
                            "without which the recipient can open but not run them")
    share.add_argument("--direct-only", action="store_true",
                       help="skip subfolders (default is the whole subtree)")

    unshare = actions.add_parser("unshare", help="revoke access to a folder's workflows")
    unshare.add_argument("folder")
    unshare.add_argument("--to", required=True)
    unshare.add_argument("--direct-only", action="store_true")

    transfer = actions.add_parser("transfer",
                                  help="move the folder and its contents to a project")
    transfer.add_argument("folder")
    transfer.add_argument("--to", required=True)

    create = actions.add_parser("create", help="create a folder")
    create.add_argument("name")
    create.add_argument("--project", required=True)
    create.add_argument("--parent")

    rename = actions.add_parser("rename", help="rename a folder")
    rename.add_argument("folder")
    rename.add_argument("--name", required=True)

    delete = actions.add_parser("delete", help="delete a folder")
    delete.add_argument("folder")
    delete.add_argument("--recursive", action="store_true",
                        help="lift contents to the parent instead of refusing")

    for cmd in (share, unshare, transfer, create, rename, delete):
        cmd.add_argument("--dry-run", action="store_true")


GLOBAL_FLAGS = ("--yes", "-y", "--json", "--no-color")


def _strip_global_flags(argv: list[str]) -> tuple[list[str], set[str]]:
    """argparse binds top-level flags only *before* the subcommand. Pull them
    out first so they work in either position."""
    present = {flag for flag in GLOBAL_FLAGS if flag in argv}
    return [a for a in argv if a not in GLOBAL_FLAGS], present


def _find_project(db, key: str) -> dict:
    known = projects(db)
    for project in known:
        if key in (project["id"], project["name"]):
            return project
    matches = [p for p in known if key.lower() in p["name"].lower()]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise Fatal(f"no project matching {key!r}")
    raise Fatal(f"{key!r} matches {len(matches)} projects — use the id")


def main(argv: list[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    cleaned, flags = _strip_global_flags(raw)
    args = build_parser().parse_args(cleaned)
    args.yes = bool(getattr(args, "yes", False)) or bool({"--yes", "-y"} & flags)
    args.json = bool(getattr(args, "json", False)) or "--json" in flags
    if bool(getattr(args, "no_color", False)) or "--no-color" in flags:
        set_color(False)
    set_assume_yes(args.yes)

    if args.cmd == "selftest":
        from .selftest import selftest
        return selftest()
    if args.cmd == "update":
        return self_update()
    if args.cmd == "install":
        return install(nginx=args.nginx, directory=args.dir,
                       start=not args.no_start)

    if not args.json:
        notice()
    inst = choose_instance(args.container, args.db_file)

    if args.cmd is None:
        interactive(inst)
        return 0
    if args.cmd == "doctor":
        if args.fix:
            return repair(inst, args.fix, dry_run=args.dry_run)
        doctor(inst)
        return 0
    if args.cmd == "project":
        return _cmd_project(inst, args)
    if args.cmd == "user":
        return _cmd_user(inst, args)
    if args.cmd == "folder":
        return _cmd_folder(inst, args)
    if args.cmd == "history":
        show_history()
        return 0
    if args.cmd == "undo":
        record = find_undoable(args.id) if args.id else last_undoable()
        revert(inst, record, assume_yes=args.yes)
        return 0
    if args.cmd == "snapshot":
        ok(f"snapshot {snapshot(inst, args.label).name}")
        return 0
    if args.cmd == "restore":
        return _cmd_restore(inst, args)
    if args.cmd == "upgrade":
        return upgrade(inst, args.target, dry_run=args.dry_run)
    if args.cmd == "ls":
        return _cmd_ls(inst, args)
    if args.cmd == "export":
        return _cmd_export(inst, args)
    if args.cmd in ("transfer", "share", "unshare"):
        return _cmd_single(inst, args)
    if args.cmd == "bulk":
        return _cmd_bulk(inst, args)
    if args.cmd == "prune":
        if args.executions is None and args.history is None:
            # Bare `prune` shows what is reclaimable instead of erroring.
            with Workspace(inst, write=False) as db:
                say()
                say(table(disk_report(inst, db),
                          ["what", "detail", "size", "prune"]))
            say()
            step("prune with: warden prune --executions N and/or --history N")
            return 0
        return prune(inst, args.executions, args.history,
                     vacuum=not args.no_vacuum, dry_run=args.dry_run,
                     take_snapshot=args.snapshot)
    return 0


def _find_target_project(db, key: str) -> dict:
    """Resolve a destination given either a project or a user's email.

    "Share with another account" is the natural phrasing, but n8n shares with
    projects — so an email resolves to that user's personal project.
    """
    if "@" in key:
        user = _find_user(db, key)
        project = personal_project(db, user["id"])
        if not project:
            raise Fatal(f"{key} has no personal project")
        return project
    return _find_project(db, key)


def _find_folder(db, key: str) -> dict:
    matches = [f for f in folders(db) if key in (f["id"], f["name"])]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise Fatal(f"no folder matching {key!r}")
    listing = "\n".join(f"        {f['id']}  {f['name']}  [{f['project']}]"
                        for f in matches)
    raise Fatal(f"{key!r} matches {len(matches)} folders — use the id:\n{listing}")


def _find_user(db, email: str) -> dict:
    for user in users(db):
        if user["email"].lower() == email.lower():
            return user
    raise Fatal(f"no user {email!r}")


def _cmd_project(inst, args) -> int:
    dry = getattr(args, "dry_run", False)

    if args.action == "create":
        apply_change(inst, f"create team project {args.name!r}",
                     lambda db, b: ops.create_project(db, b, args.name,
                                                      args.description),
                     dry_run=dry)
        return 0

    with Workspace(inst, write=False) as db:
        project = _find_project(db, args.project)
        if args.action == "members":
            rows = [dict(r) for r in db.q(
                '''SELECT u.email, pr.role, u."roleSlug" AS global FROM project_relation pr
                   JOIN "user" u ON u.id=pr."userId" WHERE pr."projectId"=?''',
                project["id"])]
            if args.json:
                say(json.dumps(rows, indent=2, default=str))
            else:
                say()
                say(table(rows, ["email", "role", "global"]))
                say()
            return 0
        if args.action in ("add-member", "remove-member"):
            user = _find_user(db, args.user)
        if args.action == "delete":
            owns = project["workflows"] or project["credentials"]
            if owns and not args.reassign_to:
                raise Fatal(f"{project['name']!r} owns {project['workflows']} "
                            f"workflow(s) and {project['credentials']} credential(s) "
                            "— pass --reassign-to")
            destination = (_find_project(db, args.reassign_to)["id"]
                           if args.reassign_to else None)

    if args.action == "rename":
        apply_change(inst, f"rename project {project['name']!r} → {args.name!r}",
                     lambda db, b: ops.rename_project(db, b, project["id"], args.name),
                     dry_run=dry)
    elif args.action == "delete":
        if not dry and not confirm_typed(project["name"],
                                         f"project {project['name']!r}"):
            return 1
        apply_change(inst, f"delete project {project['name']!r}",
                     lambda db, b: ops.delete_project(db, b, project["id"], destination),
                     dry_run=dry)
    elif args.action == "add-member":
        apply_change(inst,
                     f"add {user['email']} to {project['name']!r} as {args.role}",
                     lambda db, b: ops.add_member(db, b, project["id"], user["id"],
                                                  args.role),
                     dry_run=dry)
    elif args.action == "remove-member":
        apply_change(inst, f"remove {user['email']} from {project['name']!r}",
                     lambda db, b: ops.remove_member(db, b, project["id"], user["id"]),
                     dry_run=dry)
    return 0


def _cmd_user(inst, args) -> int:
    dry = getattr(args, "dry_run", False)

    if args.action == "create":
        apply_change(inst, f"create user {args.email}",
                     lambda db, b: ops.create_user(db, b, args.email, args.first,
                                                   args.last, args.role),
                     dry_run=dry)
        return 0

    with Workspace(inst, write=False) as db:
        user = _find_user(db, args.email)
        destination = (_find_project(db, args.reassign_to)["id"]
                       if getattr(args, "reassign_to", None) else None)

    handlers = {
        "set-role": (f"set {user['email']} to {args.__dict__.get('role')}",
                     lambda db, b: ops.set_global_role(db, b, user["id"], args.role)),
        "disable": (f"disable {user['email']}",
                    lambda db, b: ops.set_disabled(db, b, user["id"], True)),
        "enable": (f"enable {user['email']}",
                   lambda db, b: ops.set_disabled(db, b, user["id"], False)),
        "clear-mfa": (f"clear 2FA for {user['email']}",
                      lambda db, b: ops.clear_mfa(db, b, user["id"])),
        "clear-password": (f"clear password for {user['email']}",
                           lambda db, b: ops.set_password(db, b, user["id"], None)),
    }

    if args.action == "delete":
        if not dry and not confirm_typed(user["email"], f"user {user['email']}"):
            return 1
        apply_change(inst, f"delete user {user['email']}",
                     lambda db, b: ops.delete_user(db, b, user["id"], destination),
                     dry_run=dry)
        return 0

    label, mutate = handlers[args.action]
    apply_change(inst, label, mutate, dry_run=dry)
    return 0


def _cmd_folder(inst, args) -> int:
    dry = getattr(args, "dry_run", False)
    recursive = not getattr(args, "direct_only", False)

    if args.action == "list":
        with Workspace(inst, write=False) as db:
            rows = []
            for folder in folders(db):
                depth = len(folder_path(db, folder["id"])) - 1
                rows.append({"id": folder["id"],
                             "name": "  " * depth + folder["name"],
                             "workflows": len(folder_workflows(db, folder["id"])),
                             "project": folder["project"]})
            if args.json:
                say(json.dumps(rows, indent=2, default=str))
            else:
                say()
                say(table(rows, ["id", "name", "workflows", "project"]))
                say()
        return 0

    if args.action == "create":
        with Workspace(inst, write=False) as db:
            project = _find_project(db, args.project)
            parent = _find_folder(db, args.parent)["id"] if args.parent else None
        apply_change(inst, f"create folder {args.name!r}",
                     lambda db, b: ops.create_folder(db, b, args.name, project["id"],
                                                     parent),
                     dry_run=dry)
        return 0

    with Workspace(inst, write=False) as db:
        folder = _find_folder(db, args.folder)
        target = (_find_target_project(db, args.to)
                  if getattr(args, "to", None) else None)
        contents = folder_workflows(db, folder["id"], recursive)

    if args.action in ("share", "unshare", "transfer"):
        say()
        step(f"{folder['name']!r} → {len(contents)} workflow(s)"
             + ("" if recursive else " (direct children only)"))

    if args.action == "share":
        apply_change(inst,
                     f"share folder {folder['name']!r} → {target['name']!r}",
                     lambda db, b: ops.share_folder(db, b, folder["id"], target["id"],
                                                    args.role, recursive,
                                                    args.with_credentials),
                     dry_run=dry)
    elif args.action == "unshare":
        apply_change(inst,
                     f"unshare folder {folder['name']!r} from {target['name']!r}",
                     lambda db, b: ops.unshare_folder(db, b, folder["id"],
                                                      target["id"], recursive),
                     dry_run=dry)
    elif args.action == "transfer":
        apply_change(inst,
                     f"move folder {folder['name']!r} → {target['name']!r}",
                     lambda db, b: ops.transfer_folder(db, b, folder["id"],
                                                       target["id"]),
                     dry_run=dry)
    elif args.action == "rename":
        apply_change(inst, f"rename folder {folder['name']!r} → {args.name!r}",
                     lambda db, b: ops.rename_folder(db, b, folder["id"], args.name),
                     dry_run=dry)
    elif args.action == "delete":
        apply_change(inst, f"delete folder {folder['name']!r}",
                     lambda db, b: ops.delete_folder(db, b, folder["id"],
                                                     args.recursive),
                     dry_run=dry)
    return 0


def _cmd_restore(inst, args) -> int:
    snaps = list_snapshots()          # newest first
    if not snaps:
        raise Fatal("no snapshots — one is taken before every write")
    if args.name:
        matches = [s for s in snaps if s.name.startswith(args.name)]
        if not matches:
            raise Fatal(f"no snapshot matching {args.name!r} — see `warden history`")
        if len(matches) > 1:
            listing = "\n".join(f"        {s.name}" for s in matches)
            raise Fatal(f"{args.name!r} matches {len(matches)} snapshots:\n{listing}")
        snap = matches[0]
    else:
        snap = snaps[0]

    say()
    step(f"restore {snap.name} — this overwrites the current database")
    if not args.yes and not confirm("proceed?", False):
        raise Fatal("cancelled")
    restore(inst, snap)
    return 0


def _cmd_ls(inst, args) -> int:
    with Workspace(inst, write=False) as db:
        if args.what == "orphans":
            say(json.dumps(orphans(db), indent=2, default=str))
            return 0
        fetch, cols = LISTINGS[args.what]
        rows = fetch(db)
        if args.json:
            say(json.dumps(rows, indent=2, default=str))
        else:
            say()
            say(table(rows, cols))
            say()
    return 0


def _cmd_export(inst, args) -> int:
    with Workspace(inst, write=False) as db:
        out = (Path(args.out) if args.out
               else Path.cwd() / f"n8n-access-{datetime.now():%Y%m%d-%H%M%S}.json")
        export_access(db, out)
        ok(f"wrote {out}")
    return 0


def _cmd_single(inst, args) -> int:
    with Workspace(inst, write=False) as db:
        destination = _find_project(db, args.to)
    is_workflow = args.kind == "wf"
    role = args.role or ("workflow:editor" if is_workflow else "credential:user")

    def mutate(db, batch):
        if args.cmd == "transfer":
            if is_workflow:
                ops.transfer_workflow(db, batch, args.id, destination["id"],
                                      args.folder_policy)
            else:
                ops.transfer_credential(db, batch, args.id, destination["id"])
        elif args.cmd == "share":
            if is_workflow:
                ops.share_workflow(db, batch, args.id, destination["id"], role)
            else:
                ops.share_credential(db, batch, args.id, destination["id"], role)
        else:
            if is_workflow:
                ops.unshare_workflow(db, batch, args.id, destination["id"])
            else:
                ops.unshare_credential(db, batch, args.id, destination["id"])

    apply_change(inst, f"{args.cmd} {args.kind} {args.id} → {destination['name']!r}",
                 mutate, dry_run=args.dry_run)
    return 0


def _cmd_bulk(inst, args) -> int:
    from .ui import _bulk_mutation

    with Workspace(inst, write=False) as db:
        kind, rows = resolve(db, args.selector)
        destination = _find_project(db, args.to)
        if args.json:
            say(json.dumps(rows, indent=2, default=str))
        else:
            say()
            say(table(rows, ["id", "name", "project"]))
            step(f"{len(rows)} {kind}(s) matched")
    if not rows:
        return 0

    ids = [r["id"] for r in rows]
    role = args.role or ("workflow:editor" if kind == "workflow" else "credential:user")
    mutate = _bulk_mutation(kind, args.action, ids, destination["id"], role,
                            args.folder_policy)
    label = f"bulk {args.action} {len(ids)} {kind}(s) → {destination['name']!r}"
    apply_change(inst, label, mutate, dry_run=not args.apply)
    return 0


def run() -> int:
    """Entry point wrapper: Fatal prints cleanly, everything else traces."""
    try:
        return main()
    except Fatal as e:
        say()
        err(str(e))
        say()
        return 1
    except KeyboardInterrupt:
        say()
        return 130
