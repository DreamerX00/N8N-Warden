"""The interactive menu.

Thin by design: every branch resolves the operator's choices, then hands a
closure to `apply_change`. No business rules live here.
"""

from __future__ import annotations

import getpass
import textwrap
from datetime import datetime
from pathlib import Path

from . import ops
from .audit import access_matrix, export_access, orphans
from .config import DEFAULT_FOLDER_POLICY, FOLDER_POLICIES
from .console import (bold, confirm, confirm_typed, cyan, dim, err, green, ok, pick,
                      red, rule, say, step, table, track, warn, yellow)
from .db import now_ts
from .doctor import doctor
from .docker import Instance, is_running
from .errors import Fatal
from .history import last_undoable, revert, show_history
from .journal import undoable
from .maintenance import (disk_report, prune, prune_event_logs,
                          prune_insights, prune_snapshots, survey)
from .model import check_invariants, roles
from .queries import (credentials, folder_descendants, folder_path, folder_workflows,
                      folders, missing_credentials,
                      personal_project, projects, users,
                      workflow_credential_ids, workflows)
from .runner import apply_change
from .selectors import resolve
from .storage import Workspace, list_snapshots, restore

from .config import VERSION

MENU = [
    ("Projects", "menu_projects"),
    ("Users", "menu_users"),
    ("Workflows", "menu_workflows"),
    ("Credentials", "menu_credentials"),
    ("Folders", "menu_folders"),
    ("Bulk operations", "menu_bulk"),
    ("Audit & inventory", "menu_audit"),
    ("Snapshots & undo", "menu_history"),
    ("Prune & disk space", "menu_prune"),
    ("Doctor", "menu_doctor"),
]

FOLDER_POLICY_HELP = {
    "root": "root   — drop the folder, land at project root",
    "mirror": "mirror — recreate the folder path in the destination",
    "block": "block  — refuse if the workflow is in a folder",
}


# --- shell ---------------------------------------------------------------

def banner(inst: Instance) -> None:
    up = is_running(inst)
    dot = green("●") if up else red("●")
    say()
    say(f"  {cyan('▄▀')} {bold('warden')} {dim(VERSION)}"
        + dim(f"   n8n {inst.version} · {inst.db_kind} · {inst.container} ")
        + dot + (green(" up") if up else red(" down")))
    say(rule())


def interactive(inst: Instance) -> None:
    handlers = globals()
    while True:
        banner(inst)
        with Workspace(inst, write=False) as db:
            counts = [len(projects(db)), len(users(db)), len(workflows(db)),
                      len(credentials(db)), len(folders(db))]
        say()
        width = len(str(len(MENU)))          # keeps 10 aligned with 1
        for i, (label, _) in enumerate(MENU, 1):
            count = f"{counts[i - 1]}" if i <= len(counts) else ""
            say(f"   {cyan(str(i).rjust(width))}  {label.ljust(22)}"
                + (dim(f"({count})") if count else ""))
        say(f"   {dim('0'.rjust(width))}  {dim('Quit')}")
        say()

        try:
            choice = ask_choice()
        except Fatal:
            return
        if choice is None:
            return
        try:
            handlers[MENU[choice - 1][1]](inst)
        except Fatal as e:
            say()
            err(str(e))
        except Exception as e:                       # noqa: BLE001
            # An interactive admin session is worth more than a traceback.
            # Report the fault and stay in the menu rather than exiting.
            say()
            err(f"unexpected error: {type(e).__name__}: {e}")
            say(dim("      this is a bug — the session is still usable"))
        input(dim("\n  ↵ to continue"))


def ask_choice() -> int | None:
    from .console import ask
    raw = ask("choose")
    if raw in ("0", "q", ""):
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 1 <= value <= len(MENU) else None


# --- pickers -------------------------------------------------------------

def _pick_project(db, prompt="project", allow_none=False):
    return pick(projects(db),
                lambda p: f"{p['name'][:44].ljust(46)} {dim(p['type'])}  "
                          f"{p['workflows']}wf {p['credentials']}cr",
                prompt, allow_none=allow_none)


def _pick_user(db, prompt="user"):
    return pick(users(db),
                lambda u: f"{u['email'].ljust(34)} {dim(u['roleSlug'])}"
                          + (red("  disabled") if u["disabled"] else ""),
                prompt)


def _pick_folder(db, prompt="folder", project_id=None, allow_none=False):
    found = folders(db, project_id)
    if not found:
        return None
    return pick(found, lambda f: f"{f['name'].ljust(28)} {dim(f['project'])}",
                prompt, allow_none=allow_none)


# --- menus ---------------------------------------------------------------

def menu_projects(inst: Instance) -> None:
    from .console import ask
    with Workspace(inst, write=False) as db:
        say()
        say(table(projects(db),
                  ["id", "name", "type", "members", "workflows", "credentials"]))
    say()
    say("    1 create team project   2 rename   3 delete")
    say("    4 add/change member     5 remove member")
    action = ask("action")

    if action == "1":
        name = ask("project name")
        description = ask("description (optional)")
        if not name:
            raise Fatal("name required")
        apply_change(inst, f"create team project {name!r}",
                     lambda db, b: ops.create_project(db, b, name, description))

    elif action == "2":
        with Workspace(inst, write=False) as db:
            project = _pick_project(db)
        new_name = ask("new name", project["name"])
        apply_change(inst, f"rename project {project['name']!r}",
                     lambda db, b: ops.rename_project(db, b, project["id"], new_name))

    elif action == "3":
        with Workspace(inst, write=False) as db:
            project = _pick_project(db)
            destination = None
            if project["workflows"] or project["credentials"]:
                warn(f"owns {project['workflows']} workflow(s), "
                     f"{project['credentials']} credential(s)")
                say("  reassign them to:")
                destination = _pick_project(db, "destination")["id"]
        if not confirm_typed(project["name"], f"project {project['name']!r}"):
            return
        apply_change(inst, f"delete project {project['name']!r}",
                     lambda db, b: ops.delete_project(db, b, project["id"], destination))

    elif action == "4":
        with Workspace(inst, write=False) as db:
            project = _pick_project(db)
            user = _pick_user(db)
            assignable = [r for r in roles(db, "project") if r != "project:personalOwner"]
            role = pick(assignable, lambda r: r, "role")
        apply_change(inst, f"add {user['email']} to {project['name']!r} as {role}",
                     lambda db, b: ops.add_member(db, b, project["id"], user["id"], role))

    elif action == "5":
        with Workspace(inst, write=False) as db:
            project = _pick_project(db)
            members = [dict(r) for r in db.q(
                '''SELECT u.id, u.email, pr.role FROM project_relation pr
                   JOIN "user" u ON u.id=pr."userId" WHERE pr."projectId"=?''',
                project["id"])]
            member = pick(members, lambda m: f"{m['email'].ljust(34)} {dim(m['role'])}",
                          "member")
        apply_change(inst, f"remove {member['email']} from {project['name']!r}",
                     lambda db, b: ops.remove_member(db, b, project["id"], member["id"]))


def menu_users(inst: Instance) -> None:
    from .console import ask
    with Workspace(inst, write=False) as db:
        say()
        say(table(users(db),
                  ["email", "firstName", "lastName", "roleSlug", "disabled", "pending"],
                  ["email", "first", "last", "role", "disabled", "pending"]))
    say()
    say("    1 create   2 set global role   3 disable/enable")
    say("    4 clear 2FA   5 set password   6 clear password   7 delete")
    action = ask("action")

    if action == "1":
        email = ask("email")
        first = ask("first name")
        last = ask("last name")
        with Workspace(inst, write=False) as db:
            role = pick(roles(db, "global"), lambda r: r, "global role")
        apply_change(inst, f"create user {email}",
                     lambda db, b: ops.create_user(db, b, email, first, last, role))

    elif action == "2":
        with Workspace(inst, write=False) as db:
            user = _pick_user(db)
            role = pick(roles(db, "global"), lambda r: r, "new global role")
        apply_change(inst, f"set {user['email']} to {role}",
                     lambda db, b: ops.set_global_role(db, b, user["id"], role))

    elif action == "3":
        with Workspace(inst, write=False) as db:
            user = _pick_user(db)
        target = not user["disabled"]
        apply_change(inst, ("disable " if target else "enable ") + user["email"],
                     lambda db, b: ops.set_disabled(db, b, user["id"], target))

    elif action == "4":
        with Workspace(inst, write=False) as db:
            user = _pick_user(db)
        apply_change(inst, f"clear 2FA for {user['email']}",
                     lambda db, b: ops.clear_mfa(db, b, user["id"]))

    elif action == "5":
        if not ops.bcrypt_available():
            raise Fatal("bcrypt not installed — use 'clear password' "
                        "and n8n's invite flow")
        with Workspace(inst, write=False) as db:
            user = _pick_user(db)
        password = getpass.getpass("  new password: ")
        if password != getpass.getpass("  again: "):
            raise Fatal("passwords do not match")
        apply_change(inst, f"set password for {user['email']}",
                     lambda db, b: ops.set_password(db, b, user["id"], password))

    elif action == "6":
        with Workspace(inst, write=False) as db:
            user = _pick_user(db)
        apply_change(inst, f"clear password for {user['email']}",
                     lambda db, b: ops.set_password(db, b, user["id"], None))

    elif action == "7":
        with Workspace(inst, write=False) as db:
            user = _pick_user(db)
            project = personal_project(db, user["id"])
            destination = None
            if project:
                owned = db.one('SELECT COUNT(*) AS n FROM shared_workflow '
                               'WHERE "projectId"=?', project["id"])["n"]
                if owned:
                    warn(f"owns {owned} workflow share(s); reassign to:")
                    destination = _pick_project(db, "destination")["id"]
        if not confirm_typed(user["email"], f"user {user['email']}"):
            return
        apply_change(inst, f"delete user {user['email']}",
                     lambda db, b: ops.delete_user(db, b, user["id"], destination))


def menu_workflows(inst: Instance) -> None:
    from .console import ask
    with Workspace(inst, write=False) as db:
        say()
        say(table(workflows(db),
                  ["id", "name", "project", "folder", "active", "shares"]))
    say()
    say("    1 transfer to project   2 share with project   3 unshare")
    say("    4 move to folder        5 show access")
    action = ask("action")

    with Workspace(inst, write=False) as db:
        workflow = pick(workflows(db),
                        lambda w: f"{(w['name'] or '')[:40].ljust(42)} "
                                  f"{dim((w['project'] or 'ORPHAN')[:24])}", "workflow")

        if action == "5":
            _show_workflow_access(db, workflow)
            return

        if action == "1":
            destination = _pick_project(db, "destination project")
            policy = (pick(list(FOLDER_POLICIES), lambda p: FOLDER_POLICY_HELP[p],
                           "folder policy")
                      if workflow["parentFolderId"] else DEFAULT_FOLDER_POLICY)
            gaps = missing_credentials(db, workflow["id"], destination["id"])
        elif action in ("2", "3"):
            destination = _pick_project(db, "project")
            role = (pick([r for r in roles(db, "workflow") if r != "workflow:owner"],
                         lambda r: r, "role") if action == "2" else None)
        elif action == "4":
            folder = _pick_folder(db, "folder", workflow["projectId"], allow_none=True)
        else:
            return

    if action == "1":
        if gaps:
            warn(f"{len(gaps)} credential(s) the destination cannot see: "
                 + ", ".join(g["name"] for g in gaps))
            if not confirm("continue anyway?"):
                return
        apply_change(inst, f"transfer {workflow['name']!r} → {destination['name']!r}",
                     lambda db, b: ops.transfer_workflow(db, b, workflow["id"],
                                                         destination["id"], policy))
    elif action == "2":
        apply_change(inst,
                     f"share {workflow['name']!r} with {destination['name']!r} as {role}",
                     lambda db, b: ops.share_workflow(db, b, workflow["id"],
                                                      destination["id"], role))
    elif action == "3":
        apply_change(inst, f"unshare {workflow['name']!r} from {destination['name']!r}",
                     lambda db, b: ops.unshare_workflow(db, b, workflow["id"],
                                                        destination["id"]))
    elif action == "4":
        apply_change(inst, f"move {workflow['name']!r}",
                     lambda db, b: ops.move_workflow_to_folder(
                         db, b, workflow["id"], folder["id"] if folder else None))


def _show_workflow_access(db, workflow) -> None:
    rows = [dict(r) for r in db.q('''
        SELECT p.name AS project, p.type, s.role FROM shared_workflow s
        JOIN project p ON p.id=s."projectId" WHERE s."workflowId"=?''', workflow["id"])]
    say()
    say(table(rows, ["project", "type", "role"]))
    used = workflow_credential_ids(db, workflow["id"])
    if used:
        say()
        step(f"uses {len(used)} credential(s): " + ", ".join(sorted(used)))


def menu_credentials(inst: Instance) -> None:
    from .console import ask
    with Workspace(inst, write=False) as db:
        say()
        say(table(credentials(db), ["id", "name", "type", "project", "shares"]))
    say()
    say("    1 transfer   2 share   3 unshare   4 show access")
    action = ask("action")

    with Workspace(inst, write=False) as db:
        credential = pick(credentials(db),
                          lambda c: f"{c['name'][:34].ljust(36)} {dim(c['type'][:20])}",
                          "credential")
        if action == "4":
            rows = [dict(r) for r in db.q('''
                SELECT p.name AS project, p.type, s.role FROM shared_credentials s
                JOIN project p ON p.id=s."projectId" WHERE s."credentialsId"=?''',
                credential["id"])]
            say()
            say(table(rows, ["project", "type", "role"]))
            return
        destination = _pick_project(db, "project")
        role = (pick([r for r in roles(db, "credential") if r != "credential:owner"],
                     lambda r: r, "role") if action == "2" else None)

    if action == "1":
        apply_change(inst,
                     f"transfer credential {credential['name']!r} → {destination['name']!r}",
                     lambda db, b: ops.transfer_credential(db, b, credential["id"],
                                                           destination["id"]))
    elif action == "2":
        apply_change(inst,
                     f"share credential {credential['name']!r} with {destination['name']!r}",
                     lambda db, b: ops.share_credential(db, b, credential["id"],
                                                        destination["id"], role))
    elif action == "3":
        apply_change(inst, f"unshare credential {credential['name']!r}",
                     lambda db, b: ops.unshare_credential(db, b, credential["id"],
                                                          destination["id"]))


def menu_folders(inst: Instance) -> None:
    from .console import ask
    with Workspace(inst, write=False) as db:
        say()
        rows = []
        for folder in folders(db):
            depth = len(folder_path(db, folder["id"])) - 1
            rows.append({"id": folder["id"], "name": "  " * depth + folder["name"],
                         "workflows": len(folder_workflows(db, folder["id"])),
                         "project": folder["project"]})
        say(table(rows, ["id", "name", "workflows", "project"]))
    say()
    say("    1 create   2 rename   3 delete")
    say("    4 share contents with…   5 revoke shared contents   6 move to project")
    action = ask("action")

    if action in ("4", "5", "6"):
        _folder_access(inst, action)
        return

    if action == "1":
        with Workspace(inst, write=False) as db:
            project = _pick_project(db)
            parent = _pick_folder(db, "parent folder", project["id"], allow_none=True)
        name = ask("folder name")
        apply_change(inst, f"create folder {name!r}",
                     lambda db, b: ops.create_folder(db, b, name, project["id"],
                                                     parent["id"] if parent else None))

    elif action == "2":
        with Workspace(inst, write=False) as db:
            folder = _pick_folder(db)
        new_name = ask("new name", folder["name"])
        apply_change(inst, f"rename folder {folder['name']!r}",
                     lambda db, b: ops.rename_folder(db, b, folder["id"], new_name))

    elif action == "3":
        with Workspace(inst, write=False) as db:
            folder = _pick_folder(db)
        recursive = confirm("lift any contents to the parent folder?", True)
        apply_change(inst, f"delete folder {folder['name']!r}",
                     lambda db, b: ops.delete_folder(db, b, folder["id"], recursive))


def _folder_access(inst: Instance, action: str) -> None:
    """Share, revoke, or move a folder's contents.

    n8n cannot share a folder itself — folders belong to exactly one project —
    so sharing means sharing every workflow filed inside it.
    """
    with Workspace(inst, write=False) as db:
        folder = pick(folders(db),
                      lambda f: f"{f['name'][:24].ljust(26)} "
                                f"{len(folder_workflows(db, f['id']))}wf  "
                                f"{dim(f['project'][:34])}", "folder")
        recursive = True
        subfolders = len(folder_descendants(db, folder["id"])) - 1
        if subfolders:
            deep = len(folder_workflows(db, folder["id"], True))
            shallow = len(folder_workflows(db, folder["id"], False))
            if deep != shallow:
                step(f"{subfolders} subfolder(s): {deep} workflows including them, "
                     f"{shallow} without")
                recursive = confirm("include subfolders?", True)

        contents = folder_workflows(db, folder["id"], recursive)
        if not contents:
            raise Fatal("folder contains no workflows")
        say()
        say(table(contents, ["id", "name", "folder"]))
        step(f"{len(contents)} workflow(s)")

        say()
        target = _pick_project(db, "share with / move to")
        role, with_creds = None, False
        if action == "4":
            role = pick([r for r in roles(db, "workflow") if r != "workflow:owner"],
                        lambda r: r, "role")
            gaps = {c["id"] for w in contents
                    for c in missing_credentials(db, w["id"], target["id"])}
            if gaps:
                warn(f"{len(gaps)} credential(s) these workflows need are not "
                     "visible to that project — they could open but not run them")
                with_creds = confirm("share those credentials too?", True)

    if action == "4":
        apply_change(inst, f"share folder {folder['name']!r} → {target['name']!r}",
                     lambda db, b: ops.share_folder(db, b, folder["id"], target["id"],
                                                    role, recursive, with_creds))
    elif action == "5":
        apply_change(inst, f"unshare folder {folder['name']!r} from {target['name']!r}",
                     lambda db, b: ops.unshare_folder(db, b, folder["id"],
                                                      target["id"], recursive))
    elif action == "6":
        warn("this moves ownership of the folder and everything in it")
        if not confirm("proceed?", False):
            return
        apply_change(inst, f"move folder {folder['name']!r} → {target['name']!r}",
                     lambda db, b: ops.transfer_folder(db, b, folder["id"],
                                                       target["id"]))


def menu_bulk(inst: Instance) -> None:
    from .console import ask
    say()
    say(dim(textwrap.dedent("""\
          selectors
            wf:*              wf:active         wf:archived      wf:orphan
            wf:name~^Slack    wf:tag=prod       wf:project=Ops   wf:owner=a@b.com
            cred:*            cred:type=slackApi                 cred:name~aws
        """)))
    expression = ask("selector")

    with Workspace(inst, write=False) as db:
        kind, rows = resolve(db, expression)
        say()
        cols = (["id", "name", "project", "folder"] if kind == "workflow"
                else ["id", "name", "type", "project"])
        say(table(rows, cols))
        say()
        step(f"{len(rows)} {kind}(s) matched")
    if not rows:
        return

    say()
    say("    1 transfer all to project   2 share all with project   3 unshare all")
    action = ask("action")
    verb = {"1": "transfer", "2": "share", "3": "unshare"}.get(action)
    if not verb:
        return

    with Workspace(inst, write=False) as db:
        destination = _pick_project(db, "target project")
        role = None
        if action == "2":
            kind_roles = roles(db, "workflow" if kind == "workflow" else "credential")
            role = pick([r for r in kind_roles if not r.endswith(":owner")],
                        lambda r: r, "role")
        policy = DEFAULT_FOLDER_POLICY
        if action == "1" and kind == "workflow" and any(r["folder"] for r in rows):
            policy = pick(list(FOLDER_POLICIES), lambda p: FOLDER_POLICY_HELP[p],
                          "folder policy")

    ids = [r["id"] for r in rows]
    mutate = _bulk_mutation(kind, verb, ids, destination["id"], role, policy)
    label = f"bulk {verb} {len(ids)} {kind}(s) → {destination['name']!r}"

    apply_change(inst, label, mutate, dry_run=True)
    if confirm("apply?"):
        apply_change(inst, label, mutate)


def _bulk_mutation(kind, verb, ids, dest_id, role, policy):
    """Build the closure applied to every matched entity."""
    def mutate(db, batch):
        for entity_id in track(ids, f"{verb} {kind}s"):
            if kind == "workflow":
                if verb == "transfer":
                    ops.transfer_workflow(db, batch, entity_id, dest_id, policy)
                elif verb == "share":
                    ops.share_workflow(db, batch, entity_id, dest_id, role)
                else:
                    ops.unshare_workflow(db, batch, entity_id, dest_id)
            else:
                if verb == "transfer":
                    ops.transfer_credential(db, batch, entity_id, dest_id)
                elif verb == "share":
                    ops.share_credential(db, batch, entity_id, dest_id, role)
                else:
                    ops.unshare_credential(db, batch, entity_id, dest_id)
    return mutate


def menu_audit(inst: Instance) -> None:
    with Workspace(inst, write=False) as db:
        say()
        say(bold("  access matrix"))
        say(table(access_matrix(db),
                  ["email", "global", "workflows", "credentials", "projects"],
                  ["user", "global role", "wf", "cred", "project membership"]))

        say()
        say(bold("  loose ends"))
        for name, found in orphans(db).items():
            label = name.replace("_", " ")
            if found:
                warn(f"{label}: {len(found)}")
                for row in found[:8]:
                    say(f"        {dim(row.get('email') or row.get('name') or row['id'])}")
            else:
                ok(f"{label}: none")

        issues = check_invariants(db)
        say()
        if issues:
            err(f"{len(issues)} invariant issue(s)")
            for issue in issues:
                say(f"      {red('·')} {issue}")
        else:
            ok("all invariants hold")

        say()
        if confirm("export full access map to JSON?"):
            out = Path.cwd() / f"n8n-access-{datetime.now():%Y%m%d-%H%M%S}.json"
            export_access(db, out)
            ok(f"wrote {out}")


def menu_history(inst: Instance) -> None:
    from .console import ask
    records, snaps = show_history()
    say("    1 undo last batch   2 undo a specific batch   3 restore a snapshot")
    action = ask("action")

    if action in ("1", "2"):
        candidates = undoable(records)
        if not candidates:
            raise Fatal("nothing to undo")
        record = (candidates[-1] if action == "1"
                  else pick(list(reversed(candidates)),
                            lambda r: f"{r['id']}  {r['action'][:44]}", "batch"))
        revert(inst, record)

    elif action == "3":
        if not snaps:
            raise Fatal("no snapshots")
        snapshot_file = pick(snaps, lambda p: p.name, "snapshot")
        warn("this replaces the entire database")
        if confirm(f"restore {snapshot_file.name}?"):
            restore(inst, snapshot_file)


def menu_prune(inst: Instance) -> None:
    """Everything reclaimable, each with its own trade-off stated."""
    from .console import ask

    with Workspace(inst, write=False) as db:
        say()
        say(bold("  reclaimable space"))
        say(table(disk_report(inst, db), ["what", "detail", "size", "prune"],
                  ["what", "detail", "size", "option"]))

    say()
    say("    1 execution history      keep newest N runs per workflow")
    say("    2 workflow versions      keep newest N saved versions per workflow")
    say("    3 warden snapshots    keep newest N of our own backups")
    say("    4 insights (dashboard)   discard chart history")
    say("    5 n8n event logs         delete rotated log files")
    say("    6 " + dim("show plan only (dry run of 1 and 2)"))
    action = ask("action")

    if action == "1":
        keep = _ask_int("keep how many executions per workflow", 10)
        prune(inst, keep, None, vacuum=confirm("reclaim the space (vacuum)?", True))

    elif action == "2":
        warn("workflow_publish_history cascades from workflow_history — "
             "pruning versions also deletes their publish-audit records")
        with Workspace(inst, write=False) as db:
            keep = _ask_int("keep how many versions per workflow", 5)
            doomed = survey(db, None, keep)["versions"]
            cascade = _publish_audit_hits(db, doomed)
        if cascade:
            warn(f"{cascade} publish-audit record(s) would be deleted with them")
        prune(inst, None, keep, vacuum=confirm("reclaim the space (vacuum)?", True))

    elif action == "3":
        snaps = list_snapshots()
        total = sum(s.stat().st_size for s in snaps) / 2**20
        step(f"{len(snaps)} snapshot(s), {total:.0f} MB")
        keep = _ask_int("keep how many (newest first)", 5)
        removed, freed = prune_snapshots(keep)
        ok(f"removed {removed} snapshot(s), freed {freed / 2**20:.0f} MB")

    elif action == "4":
        raw = ask("keep insights newer than how many days (blank = discard all)")
        prune_insights(inst, int(raw) if raw.strip() else None)

    elif action == "5":
        removed, freed = prune_event_logs(inst)
        ok(f"removed {removed} rotated log(s), freed {freed / 2**20:.1f} MB")

    elif action == "6":
        exec_keep = _ask_int("executions to keep per workflow", 10)
        hist_keep = _ask_int("versions to keep per workflow", 5)
        prune(inst, exec_keep, hist_keep, dry_run=True)


def _ask_int(prompt: str, default: int) -> int:
    from .console import ask
    while True:
        raw = ask(prompt, str(default)).strip()
        if raw.isdigit():
            return int(raw)
        err(f"{raw!r} is not a whole number")


def _publish_audit_hits(db, versions: list[str]) -> int:
    """How many publish-audit rows would cascade away with these versions."""
    if not versions:
        return 0
    try:
        marks = ",".join("?" for _ in versions)
        return db.one(f'SELECT COUNT(*) AS n FROM workflow_publish_history '
                      f'WHERE "versionId" IN ({marks})', *versions)["n"]
    except Exception:
        return 0


def menu_doctor(inst: Instance) -> None:
    doctor(inst)
