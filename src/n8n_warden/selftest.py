"""Self-verification against a synthetic database.

Builds a miniature n8n schema in a temp file, exercises every operation, and
asserts both the forward state and the reverse (undo) state. Touches nothing
real, so it is safe to run before pointing the tool at live data.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from .config import VERIFIED_MIGRATION, VERIFIED_MIGRATION_TS
from .console import _BadChoice, _parse_multi, _parse_single, bold, dim, err, ok, red, say
from .db import Db, now_ts
from .errors import Fatal
from .journal import UNDO_MARKER, Batch, is_undo_record, undo_batch, undoable
from .model import (Issue, check_invariants, compat_note, migration_state, roles,
                    schema_check)
from .maintenance import prune, survey
from .repair import FIXERS, REPORT_ONLY, apply_fixes, select
from .ops import (add_member, create_folder, create_project, create_user,
                  set_global_role, share_folder, share_workflow,
                  transfer_folder, transfer_workflow, unshare_folder,
                  unshare_workflow)
from .queries import (folder_descendants, folder_path, folder_workflows, folders,
                      missing_credentials, personal_project)
from .selectors import resolve

# A structurally faithful subset of n8n's schema — enough to exercise every
# constraint the tool relies on, including the FKs SQLite would otherwise skip.
DDL = """
CREATE TABLE role (slug varchar(128) PRIMARY KEY, "displayName" text, description text,
  "roleType" text, "systemRole" boolean NOT NULL DEFAULT (false),
  "createdAt" datetime, "updatedAt" datetime);
CREATE TABLE "user" (id varchar PRIMARY KEY, email varchar(255) UNIQUE,
  "firstName" varchar(32), "lastName" varchar(32), password varchar,
  "personalizationAnswers" text, "createdAt" datetime, "updatedAt" datetime,
  settings text, disabled boolean NOT NULL DEFAULT (0),
  "mfaEnabled" boolean NOT NULL DEFAULT (0), "mfaSecret" text,
  "mfaRecoveryCodes" text, "lastActiveAt" date,
  "roleSlug" varchar(128) NOT NULL DEFAULT ('global:member') REFERENCES role(slug));
CREATE TABLE project (id varchar(36) PRIMARY KEY, name varchar(255) NOT NULL,
  type varchar(36) NOT NULL, "createdAt" datetime, "updatedAt" datetime, icon text,
  description varchar(512),
  "creatorId" varchar REFERENCES "user"(id) ON DELETE SET NULL,
  "customTelemetryTags" text NOT NULL DEFAULT ('[]'));
CREATE TABLE project_relation (
  "projectId" varchar(36) NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  "userId" varchar NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
  role varchar NOT NULL REFERENCES role(slug),
  "createdAt" datetime, "updatedAt" datetime, PRIMARY KEY ("projectId","userId"));
CREATE TABLE folder (id varchar(36) PRIMARY KEY, name varchar(128) NOT NULL,
  "parentFolderId" varchar(36) REFERENCES folder(id) ON DELETE CASCADE,
  "projectId" varchar(36) NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  "createdAt" datetime, "updatedAt" datetime);
CREATE TABLE workflow_entity (id varchar(36) PRIMARY KEY, name varchar(128) NOT NULL,
  active boolean NOT NULL DEFAULT (0), nodes text, connections text, settings text,
  "staticData" text, "pinData" text, "versionId" varchar(36) NOT NULL DEFAULT (''),
  "triggerCount" integer DEFAULT (0), meta text,
  "parentFolderId" varchar(36) REFERENCES folder(id) ON DELETE CASCADE,
  "createdAt" datetime, "updatedAt" datetime,
  "isArchived" boolean NOT NULL DEFAULT (0),
  "versionCounter" integer NOT NULL DEFAULT (1), description text,
  "nodeGroups" text NOT NULL DEFAULT ('[]'),
  -- ON DELETE RESTRICT is the reason prune must never drop a pinned version.
  "activeVersionId" varchar(36)
    REFERENCES workflow_history("versionId") ON DELETE RESTRICT);
CREATE TABLE credentials_entity (id varchar(36) PRIMARY KEY, name varchar(128) NOT NULL,
  data text NOT NULL DEFAULT (''), type varchar(32) NOT NULL,
  "createdAt" datetime, "updatedAt" datetime,
  "isManaged" boolean NOT NULL DEFAULT (0));
CREATE TABLE shared_workflow (
  "workflowId" varchar(36) NOT NULL REFERENCES workflow_entity(id) ON DELETE CASCADE,
  "projectId" varchar(36) NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  role text NOT NULL, "createdAt" datetime, "updatedAt" datetime,
  PRIMARY KEY ("workflowId","projectId"));
CREATE TABLE shared_credentials (
  "credentialsId" varchar(36) NOT NULL REFERENCES credentials_entity(id) ON DELETE CASCADE,
  "projectId" varchar(36) NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  role text NOT NULL, "createdAt" datetime, "updatedAt" datetime,
  PRIMARY KEY ("credentialsId","projectId"));
CREATE TABLE tag_entity (id varchar(36) PRIMARY KEY, name varchar(24) NOT NULL);
CREATE TABLE workflows_tags ("workflowId" varchar(36) NOT NULL,
  "tagId" varchar(36) NOT NULL, PRIMARY KEY ("workflowId","tagId"));
CREATE TABLE execution_entity (id integer PRIMARY KEY,
  "workflowId" varchar(36) NOT NULL REFERENCES workflow_entity(id),
  finished boolean NOT NULL DEFAULT (1), mode varchar NOT NULL DEFAULT ('manual'),
  "startedAt" datetime, "stoppedAt" datetime);
CREATE TABLE execution_data (
  "executionId" integer PRIMARY KEY REFERENCES execution_entity(id),
  "workflowData" text, data text);
CREATE TABLE workflow_history ("versionId" varchar(36) PRIMARY KEY,
  "workflowId" varchar(36) NOT NULL REFERENCES workflow_entity(id),
  authors varchar(255), "createdAt" datetime, "updatedAt" datetime,
  nodes text, connections text);
"""

SEED_ROLES = [
    ("global:owner", "global"), ("global:admin", "global"), ("global:member", "global"),
    ("project:personalOwner", "project"), ("project:admin", "project"),
    ("project:editor", "project"), ("project:viewer", "project"),
    ("workflow:owner", "workflow"), ("workflow:editor", "workflow"),
    ("credential:owner", "credential"), ("credential:user", "credential"),
]

ALICE, BOB = "p_u_alice", "p_u_bob"


# --- assertions ----------------------------------------------------------

def eq(actual, expected):
    if actual != expected:
        raise AssertionError(f"expected {expected!r}, got {actual!r}")


def truthy(value):
    if not value:
        raise AssertionError("expected true")


def raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return
    except Exception as e:
        raise AssertionError(f"raised {type(e).__name__}, expected {exc.__name__}")
    raise AssertionError("did not raise")


class Checks:
    """Collects pass/fail without aborting the run on the first failure."""

    def __init__(self):
        self.failures: list[str] = []

    def __call__(self, name: str, fn):
        try:
            fn()
            ok(name)
        except AssertionError as e:
            err(f"{name}: {e}")
            self.failures.append(name)
        except Exception as e:                      # noqa: BLE001 - report, don't crash
            err(f"{name}: {type(e).__name__}: {e}")
            self.failures.append(name)


# --- fixture -------------------------------------------------------------

def build_fixture(path: Path) -> Db:
    db = Db(path)
    db.conn.executescript(DDL)
    ts = now_ts()

    for slug, kind in SEED_ROLES:
        db.exec('INSERT INTO role (slug, "roleType", "systemRole", "createdAt", '
                '"updatedAt") VALUES (?,?,1,?,?)', slug, kind, ts, ts)

    def add_user(user_id, email, role):
        db.exec('INSERT INTO "user" (id,email,"createdAt","updatedAt","roleSlug") '
                "VALUES (?,?,?,?,?)", user_id, email, ts, ts, role)
        project_id = "p_" + user_id
        db.exec('INSERT INTO project (id,name,type,"createdAt","updatedAt",'
                '"customTelemetryTags") VALUES (?,?,\'personal\',?,?,\'[]\')',
                project_id, f"{email} <{email}>", ts, ts)
        db.exec('INSERT INTO project_relation ("projectId","userId",role,"createdAt",'
                '"updatedAt") VALUES (?,?,\'project:personalOwner\',?,?)',
                project_id, user_id, ts, ts)

    add_user("u_alice", "alice@example.com", "global:owner")
    add_user("u_bob", "bob@example.com", "global:member")

    db.exec('INSERT INTO folder (id,name,"projectId","createdAt","updatedAt") '
            "VALUES ('f_reports','Reports',?,?,?)", ALICE, ts, ts)
    db.exec('INSERT INTO folder (id,name,"parentFolderId","projectId","createdAt",'
            '"updatedAt") VALUES (\'f_q1\',\'Q1\',\'f_reports\',?,?,?)', ALICE, ts, ts)

    db.exec('INSERT INTO credentials_entity (id,name,type,"createdAt","updatedAt") '
            "VALUES ('c_slack','Slack account','slackApi',?,?)", ts, ts)
    db.exec('INSERT INTO shared_credentials ("credentialsId","projectId",role,'
            '"createdAt","updatedAt") VALUES (\'c_slack\',?,\'credential:owner\',?,?)',
            ALICE, ts, ts)

    nodes = json.dumps([{"name": "Slack", "credentials": {
        "slackApi": {"id": "c_slack", "name": "Slack account"}}}])
    db.exec('INSERT INTO workflow_entity (id,name,active,nodes,"parentFolderId",'
            '"createdAt","updatedAt") '
            "VALUES ('w_report','Weekly report',1,?,'f_q1',?,?)", nodes, ts, ts)
    db.exec('INSERT INTO shared_workflow ("workflowId","projectId",role,"createdAt",'
            '"updatedAt") VALUES (\'w_report\',?,\'workflow:owner\',?,?)', ALICE, ts, ts)

    db.exec('INSERT INTO workflow_entity (id,name,active,"createdAt","updatedAt") '
            "VALUES ('w_plain','Plain flow',0,?,?)", ts, ts)
    db.exec('INSERT INTO shared_workflow ("workflowId","projectId",role,"createdAt",'
            '"updatedAt") VALUES (\'w_plain\',?,\'workflow:owner\',?,?)', ALICE, ts, ts)

    db.exec("INSERT INTO tag_entity (id,name) VALUES ('t_prod','prod')")
    db.exec('INSERT INTO workflows_tags ("workflowId","tagId") '
            "VALUES ('w_plain','t_prod')")
    db.conn.commit()
    return db


# --- helpers -------------------------------------------------------------

def _owner_project(db, workflow_id: str) -> str | None:
    row = db.one('SELECT "projectId" AS p FROM shared_workflow WHERE "workflowId"=? '
                 "AND role='workflow:owner'", workflow_id)
    return row["p"] if row else None


def _folder_of(db, workflow_id: str) -> str | None:
    return db.one('SELECT "parentFolderId" AS f FROM workflow_entity WHERE id=?',
                  workflow_id)["f"]


def _share_count(db, workflow_id: str) -> int:
    return db.one('SELECT COUNT(*) AS n FROM shared_workflow WHERE "workflowId"=?',
                  workflow_id)["n"]


# --- suites --------------------------------------------------------------

def _suite_basics(db, check) -> None:
    check("fixture starts clean", lambda: eq(check_invariants(db), []))
    check("roles are read from the role table",
          lambda: eq(set(roles(db, "workflow")), {"workflow:owner", "workflow:editor"}))

    def bad_role():
        db.exec('INSERT INTO project_relation ("projectId","userId",role,"createdAt",'
                '"updatedAt") VALUES (?,?,?,?,?)',
                ALICE, "u_bob", "no:such:role", now_ts(), now_ts())

    check("foreign keys are enforced", lambda: raises(bad_role))
    db.conn.rollback()


def _suite_transfer(db, check) -> None:
    batch = Batch(db, "transfer")
    transfer_workflow(db, batch, "w_report", BOB, "root")

    check("transfer leaves exactly one owner", lambda: eq(
        db.one("SELECT COUNT(*) AS n FROM shared_workflow WHERE "
               "\"workflowId\"='w_report' AND role='workflow:owner'")["n"], 1))
    check("transfer moves ownership to the destination",
          lambda: eq(_owner_project(db, "w_report"), BOB))
    check("transfer clears the cross-project folder",
          lambda: eq(_folder_of(db, "w_report"), None))
    check("transfer introduces no invariant breach",
          lambda: eq(check_invariants(db), []))
    check("credential gap is detected", lambda: eq(
        [c["id"] for c in missing_credentials(db, "w_report", BOB)], ["c_slack"]))

    def undo_restores():
        undo_batch(db, {"entries": batch.entries})
        eq(_owner_project(db, "w_report"), ALICE)
        eq(_folder_of(db, "w_report"), "f_q1")
        eq(check_invariants(db), [])

    check("undo restores the previous owner", undo_restores)


def _suite_folder_policies(db, check) -> None:
    batch = Batch(db, "mirror")
    transfer_workflow(db, batch, "w_report", BOB, "mirror")

    check("mirror recreates the folder path", lambda: eq(
        [f["name"] for f in folder_path(db, _folder_of(db, "w_report"))],
        ["Reports", "Q1"]))
    check("mirrored folders belong to the destination",
          lambda: eq({f["projectId"] for f in folders(db, BOB)}, {BOB}))
    check("mirror leaves invariants intact", lambda: eq(check_invariants(db), []))
    undo_batch(db, {"entries": batch.entries})

    check("block policy refuses a foldered workflow", lambda: raises(
        lambda: transfer_workflow(db, Batch(db, "x"), "w_report", BOB, "block"), Fatal))
    db.conn.rollback()


def _suite_sharing(db, check) -> None:
    batch = Batch(db, "share")
    share_workflow(db, batch, "w_plain", BOB, "workflow:editor")

    check("share adds a second, non-owner row",
          lambda: eq(_share_count(db, "w_plain"), 2))
    check("bad role slug is rejected", lambda: raises(
        lambda: share_workflow(db, Batch(db, "x"), "w_plain", BOB, "workflow:Owner"),
        Fatal))
    check("sharing as owner is rejected", lambda: raises(
        lambda: share_workflow(db, Batch(db, "x"), "w_plain", BOB, "workflow:owner"),
        Fatal))
    check("unsharing the owner is rejected", lambda: raises(
        lambda: unshare_workflow(db, Batch(db, "x"), "w_plain", ALICE), Fatal))

    undo_batch(db, {"entries": batch.entries})
    check("undo removes the share", lambda: eq(_share_count(db, "w_plain"), 1))


def _suite_invariant_detection(db, check) -> None:
    def second_owner():
        db.exec('INSERT INTO shared_workflow ("workflowId","projectId",role,'
                '"createdAt","updatedAt") '
                "VALUES ('w_plain',?,'workflow:owner',?,?)", BOB, now_ts(), now_ts())
        truthy(any(i.kind == "dup-owner" for i in check_invariants(db)))

    check("a second owner row is detected", second_owner)
    db.conn.rollback()

    def cross_project_folder():
        db.exec("UPDATE workflow_entity SET \"parentFolderId\"='f_q1' "
                "WHERE id='w_plain'")
        db.exec('UPDATE shared_workflow SET "projectId"=? '
                "WHERE \"workflowId\"='w_plain'", BOB)
        truthy(any(i.kind == "cross-project-folder" for i in check_invariants(db)))

    check("a cross-project folder is detected", cross_project_folder)
    db.conn.rollback()


def _suite_users(db, check) -> None:
    batch = Batch(db, "create user")
    create_user(db, batch, "carol@example.com", "Carol", "X", "global:member")

    check("new user gets a personal project", lambda: truthy(
        personal_project(db, db.one('SELECT id FROM "user" WHERE email=?',
                                    "carol@example.com")["id"])))
    check("new user keeps invariants", lambda: eq(check_invariants(db), []))
    check("duplicate email is rejected", lambda: raises(
        lambda: create_user(db, Batch(db, "x"), "carol@example.com"), Fatal))
    check("demoting the only owner is rejected", lambda: raises(
        lambda: set_global_role(db, Batch(db, "x"), "u_alice", "global:member"), Fatal))

    undo_batch(db, {"entries": batch.entries})
    check("undo removes user, project and relation", lambda: eq(
        db.one('SELECT COUNT(*) AS n FROM "user" WHERE email=?',
               "carol@example.com")["n"], 0))


def _suite_selectors(db, check) -> None:
    check("folder under a foreign project is rejected", lambda: raises(
        lambda: create_folder(db, Batch(db, "x"), "Nope", BOB, "f_reports"), Fatal))
    check("selector wf:tag=prod resolves", lambda: eq(
        [r["id"] for r in resolve(db, "wf:tag=prod")[1]], ["w_plain"]))
    check("selector wf:name~ resolves", lambda: eq(
        [r["id"] for r in resolve(db, "wf:name~^Weekly")[1]], ["w_report"]))
    check("selector cred:type= resolves", lambda: eq(
        [r["id"] for r in resolve(db, "cred:type=slackApi")[1]], ["c_slack"]))
    check("bad selector is rejected",
          lambda: raises(lambda: resolve(db, "nope:*"), Fatal))


def _suite_staleness(db, check) -> None:
    """The hole that made undo dangerous: reverting a row someone else moved."""
    batch = Batch(db, "share for staleness test")
    share_workflow(db, batch, "w_plain", BOB, "workflow:editor")
    # Simulate the same row being changed through the n8n UI afterwards.
    db.exec("UPDATE shared_workflow SET role='workflow:owner' "
            "WHERE \"workflowId\"='w_plain' AND \"projectId\"=?", BOB)

    def skips_changed_row():
        reverted, skipped = undo_batch(db, {"entries": batch.entries})
        eq(reverted, 0)
        eq(len(skipped), 1)
        truthy("changed after the batch" in skipped[0])

    check("undo skips a row that changed after the batch", skips_changed_row)
    check("undo did not clobber the later change", lambda: eq(
        db.one('SELECT role FROM shared_workflow WHERE "workflowId"=\'w_plain\' '
               'AND "projectId"=?', BOB)["role"], "workflow:owner"))
    db.conn.rollback()

    clean = Batch(db, "share then undo cleanly")
    share_workflow(db, clean, "w_plain", BOB, "workflow:editor")

    def reverts_untouched():
        reverted, skipped = undo_batch(db, {"entries": clean.entries})
        eq(reverted, 1)
        eq(skipped, [])

    check("undo still reverts an untouched row", reverts_untouched)

    def reports_deleted_row():
        batch2 = Batch(db, "share then delete")
        share_workflow(db, batch2, "w_plain", BOB, "workflow:editor")
        db.exec('DELETE FROM shared_workflow WHERE "workflowId"=\'w_plain\' '
                'AND "projectId"=?', BOB)
        reverted, skipped = undo_batch(db, {"entries": batch2.entries})
        eq(reverted, 0)
        truthy("deleted after the batch" in skipped[0])

    check("undo reports a row deleted after the batch", reports_deleted_row)
    db.conn.rollback()


def _suite_compatibility(db, check) -> None:
    db.conn.executescript("CREATE TABLE migrations (id integer PRIMARY KEY, "
                          "timestamp bigint, name varchar);")
    db.exec("INSERT INTO migrations (id,timestamp,name) VALUES (1,1,?)",
            VERIFIED_MIGRATION)

    def reads_state():
        name, ts, _ = migration_state(db)
        eq(name, VERIFIED_MIGRATION)
        eq(ts, VERIFIED_MIGRATION_TS)

    check("migration state is read from n8n's own table", reads_state)
    check("verified migration passes the compat gate", lambda: eq(compat_note(db), None))

    def newer_trips_gate():
        db.exec("INSERT INTO migrations (id,timestamp,name) VALUES (2,2,?)",
                f"SomeFutureThing{VERIFIED_MIGRATION_TS + 1000}")
        truthy("newer than the tool was verified" in (compat_note(db) or ""))

    check("a newer migration trips the compat gate", newer_trips_gate)
    db.exec("DELETE FROM migrations WHERE id=2")


def _suite_repair(db, check) -> None:
    """Each fixer must clear its own issue without tripping a different one."""

    def fix_dup_owner():
        db.exec('INSERT INTO shared_workflow ("workflowId","projectId",role,'
                '"createdAt","updatedAt") '
                "VALUES ('w_plain',?,'workflow:owner',?,?)", BOB, now_ts(), now_ts())
        issues = check_invariants(db)
        truthy(any(i.kind == "dup-owner" for i in issues))
        batch = Batch(db, "fix")
        eq(apply_fixes(db, batch, select(issues, "dup-owner")), 1)
        eq(check_invariants(db), [])
        # Demoted rather than deleted: the surplus project keeps access.
        eq(db.one('SELECT role FROM shared_workflow WHERE "workflowId"=\'w_plain\' '
                  'AND "projectId"=?', BOB)["role"], "workflow:editor")

    check("dup-owner repair keeps one owner and demotes the rest", fix_dup_owner)
    db.conn.rollback()

    def fix_cross_project_folder():
        db.exec("UPDATE workflow_entity SET \"parentFolderId\"='f_q1' "
                "WHERE id='w_plain'")
        db.exec('UPDATE shared_workflow SET "projectId"=? '
                "WHERE \"workflowId\"='w_plain'", BOB)
        issues = check_invariants(db)
        batch = Batch(db, "fix")
        apply_fixes(db, batch, select(issues, "cross-project-folder"))
        eq(_folder_of(db, "w_plain"), None)
        eq(check_invariants(db), [])

    check("cross-project-folder repair clears the dangling reference",
          fix_cross_project_folder)
    db.conn.rollback()

    def fix_orphan_share():
        db.exec("DELETE FROM shared_workflow WHERE \"workflowId\"='w_plain'")
        issues = check_invariants(db)
        truthy(any(i.kind == "orphan-share" for i in issues))
        batch = Batch(db, "fix")
        apply_fixes(db, batch, select(issues, "orphan-share"))
        eq(_owner_project(db, "w_plain"), ALICE)   # instance owner's project
        eq(check_invariants(db), [])

    check("orphan-share repair adopts into the instance owner's project",
          fix_orphan_share)
    db.conn.rollback()

    check("'all' selects only fixable issues", lambda: eq(
        select([Issue("dup-owner", "x"), Issue("unknown-role", "y")], "all"),
        [Issue("dup-owner", "x")]))
    check("report-only kinds are refused explicitly", lambda: raises(
        lambda: select([Issue("unknown-role", "y")], "unknown-role"), Fatal))
    check("unknown issue kind is rejected", lambda: raises(
        lambda: select([], "no-such-kind"), Fatal))
    check("every fixer is reachable by name",
          lambda: eq(set(FIXERS) & set(REPORT_ONLY), set()))


def _suite_journal_selection(db, check) -> None:
    """Regression: repeated `undo` used to consume its own journal records
    instead of stepping back through real batches."""
    write_a = {"id": "1", "undone": False, "kind": "write",
               "entries": [{"table": "shared_workflow"}]}
    write_b = {"id": "2", "undone": False, "kind": "write",
               "entries": [{"table": "project"}]}
    undo_of_b = {"id": "3", "kind": "undo",
                 "entries": [{"table": UNDO_MARKER}]}
    legacy_undo = {"id": "4", "entries": [{"table": UNDO_MARKER}]}   # pre-fix record

    check("undo records are recognised",
          lambda: truthy(is_undo_record(undo_of_b)))
    check("pre-fix undo records are recognised by their marker",
          lambda: truthy(is_undo_record(legacy_undo)))
    check("ordinary writes are not mistaken for undos",
          lambda: truthy(not is_undo_record(write_a)))
    check("undo skips its own records and picks the last real batch",
          lambda: eq([r["id"] for r in
                      undoable([write_a, write_b, undo_of_b, legacy_undo])],
                     ["1", "2"]))
    check("already-undone batches are excluded", lambda: eq(
        [r["id"] for r in undoable([{**write_a, "undone": True}, write_b])], ["2"]))


def _suite_prune(db, check) -> None:
    """Pruning is the most destructive operation here, and the one whose
    foreign keys are easiest to get wrong."""
    ts = now_ts()
    for i in range(1, 26):                       # 25 executions on w_plain
        db.exec('INSERT INTO execution_entity (id,"workflowId","startedAt") '
                "VALUES (?,'w_plain',?)", i, f"2026-08-{i:02d} 10:00:00")
        db.exec('INSERT INTO execution_data ("executionId",data) VALUES (?,?)',
                i, "x" * 100)
    for i in range(1, 9):                        # 8 saved versions on w_plain
        db.exec('INSERT INTO workflow_history ("versionId","workflowId","createdAt") '
                "VALUES (?,'w_plain',?)", f"v{i:02d}", f"2026-08-{i:02d} 10:00:00")

    check("survey counts executions beyond the limit", lambda: eq(
        len(survey(db, 10, None)["executions"]), 15))
    check("survey counts versions beyond the limit", lambda: eq(
        len(survey(db, None, 5)["versions"]), 3))

    def keeps_newest():
        doomed = set(survey(db, 10, None)["executions"])
        eq(doomed & set(range(16, 26)), set())    # newest 10 survive
        eq(doomed, set(range(1, 16)))

    check("prune targets the oldest and keeps the newest", keeps_newest)

    def respects_pinned_version():
        # v01 is the oldest and would be pruned, but a workflow points at it.
        db.exec("UPDATE workflow_entity SET \"activeVersionId\"='v01' "
                "WHERE id='w_plain'")
        plan = survey(db, None, 5)
        truthy("v01" not in plan["versions"])
        eq(plan["pinned_kept"], 1)

    check("a pinned active version is never pruned", respects_pinned_version)

    check("survey with nothing selected returns nothing", lambda: eq(
        survey(db, None, None), {"executions": [], "versions": [], "pinned_kept": 0}))
    check("prune refuses when no target is given", lambda: raises(
        lambda: prune(None, None, None), Fatal))
    check("prune rejects a negative keep count", lambda: raises(
        lambda: prune(None, -1, None), Fatal))

    db.conn.rollback()


def _suite_picker(db, check) -> None:
    """Regression: a mistyped menu answer used to raise ValueError and end
    the whole session."""
    items = ["credential:user", "credential:owner", "workflow:editor"]
    ident = lambda x: x                                          # noqa: E731

    check("an index selects by position",
          lambda: eq(_parse_single("2", items, ident, False), "credential:owner"))
    check("exact text selects the option",
          lambda: eq(_parse_single("workflow:editor", items, ident, False),
                     "workflow:editor"))
    check("a unique substring selects the option",
          lambda: eq(_parse_single("editor", items, ident, False), "workflow:editor"))
    check("an ambiguous substring is rejected, not guessed", lambda: raises(
        lambda: _parse_single("credential", items, ident, False), _BadChoice))
    check("unmatched text is rejected rather than crashing", lambda: raises(
        lambda: _parse_single("member", items, ident, False), _BadChoice))
    check("an out-of-range index is rejected", lambda: raises(
        lambda: _parse_single("99", items, ident, False), _BadChoice))
    check("zero selects none when permitted",
          lambda: eq(_parse_single("0", items, ident, True), None))

    check("'*' selects everything", lambda: eq(_parse_multi("*", items), items))
    check("a comma list selects those items", lambda: eq(
        _parse_multi("1,3", items), ["credential:user", "workflow:editor"]))
    check("a range selects the span", lambda: eq(
        _parse_multi("1-2", items), ["credential:user", "credential:owner"]))
    check("garbage in a multi-select is rejected", lambda: raises(
        lambda: _parse_multi("one,two", items), _BadChoice))


def _suite_folder_access(db, check) -> None:
    """Sharing a folder means sharing its workflows — n8n cannot share the
    folder itself."""
    batch = Batch(db, "seed")
    nested = create_folder(db, batch, "Nested", ALICE, "f_reports")
    db.exec('UPDATE workflow_entity SET "parentFolderId"=? WHERE id=\'w_plain\'', nested)

    check("descendants include the whole subtree", lambda: eq(
        set(folder_descendants(db, "f_reports")), {"f_reports", "f_q1", nested}))
    check("recursive listing reaches nested workflows", lambda: eq(
        {w["id"] for w in folder_workflows(db, "f_reports", True)},
        {"w_report", "w_plain"}))
    check("direct-only listing does not", lambda: eq(
        folder_workflows(db, "f_reports", False), []))

    share = Batch(db, "share folder")
    share_folder(db, share, "f_reports", BOB, "workflow:editor", True, False)

    check("sharing a folder shares every workflow in it", lambda: eq(
        db.one('SELECT COUNT(*) AS n FROM shared_workflow WHERE "projectId"=? '
               "AND role='workflow:editor'", BOB)["n"], 2))
    check("ownership is unchanged by sharing",
          lambda: eq(_owner_project(db, "w_report"), ALICE))
    check("sharing a folder keeps invariants", lambda: eq(check_invariants(db), []))

    check("sharing with the owning project is refused", lambda: raises(
        lambda: share_folder(db, Batch(db, "x"), "f_reports", ALICE), Fatal))

    unshare = Batch(db, "unshare folder")
    unshare_folder(db, unshare, "f_reports", BOB, True)
    check("unsharing a folder revokes those shares", lambda: eq(
        db.one('SELECT COUNT(*) AS n FROM shared_workflow WHERE "projectId"=?',
               BOB)["n"], 0))

    move = Batch(db, "transfer folder")
    transfer_folder(db, move, "f_reports", BOB)
    check("transferring a folder moves the subtree", lambda: eq(
        {f["projectId"] for f in folders(db, BOB)}, {BOB}))
    check("transferred workflows follow their folder",
          lambda: eq(_owner_project(db, "w_report"), BOB))
    check("the moved root folder loses a parent left behind", lambda: eq(
        db.one('SELECT "parentFolderId" AS p FROM folder WHERE id=?',
               "f_reports")["p"], None))
    check("transferring a folder leaves no dangling reference",
          lambda: eq(check_invariants(db), []))

    check("'keep' policy refuses when the folder did not move", lambda: raises(
        lambda: transfer_workflow(db, Batch(db, "x"), "w_report", ALICE, "keep"),
        Fatal))

    db.conn.rollback()


def _suite_presentation(db, check) -> None:
    """Colour and animation are terminal affordances only.

    The self-test itself runs with stdout redirected, so if these emit escape
    codes here they would corrupt every pipe, --json consumer and CI log.
    """
    import re as _re
    from . import console

    ansi = _re.compile(r"\x1b\[")

    check("colour is off when stdout is not a terminal", lambda: truthy(
        not ansi.search(console.bold("x") + console.red("y") + console.cyan("z"))))
    check("rules emit no escape codes off-terminal",
          lambda: truthy(not ansi.search(console.rule("section"))))
    check("tables emit no escape codes off-terminal", lambda: truthy(
        not ansi.search(console.table([{"a": "1"}], ["a"]))))

    def spinner_is_silent():
        with console.Spinner("working", "done"):
            pass
        # No thread should survive, and nothing should have been animated.
        truthy(console._ANIMATE is False)

    check("spinner does not animate off-terminal", spinner_is_silent)
    check("progress falls back to a plain line off-terminal",
          lambda: eq(list(console.track([1, 2, 3], "items")), [1, 2, 3]))
    check("progress passes items through unchanged even when empty",
          lambda: eq(list(console.track([], "items")), []))

    def set_color_is_reversible():
        console.set_color(False)
        truthy(not ansi.search(console.green("x")))
        console.set_color(True)      # still off: stdout is not a terminal here

    check("--no-color cannot be overridden into a non-terminal",
          set_color_is_reversible)


SUITES = (
    _suite_basics, _suite_transfer, _suite_folder_policies, _suite_sharing,
    _suite_invariant_detection, _suite_users, _suite_selectors,
    _suite_staleness, _suite_compatibility, _suite_repair,
    _suite_journal_selection, _suite_prune, _suite_picker, _suite_folder_access,
    _suite_presentation,
)


def selftest() -> int:
    say()
    say(bold("  warden selftest"))
    say(dim("  synthetic database — touches nothing real"))
    say()

    tmp = Path(tempfile.mkdtemp(prefix="n8nadm-test-"))
    check = Checks()
    db = build_fixture(tmp / "t.sqlite")

    for suite in SUITES:
        suite(db, check)

    check("schema fingerprint matches", lambda: eq(schema_check(db), []))
    check("final state is clean", lambda: eq(check_invariants(db), []))

    db.conn.close()
    shutil.rmtree(tmp, ignore_errors=True)

    say()
    if check.failures:
        err(f"{len(check.failures)} check(s) failed: {', '.join(check.failures)}")
        return 1
    ok("all checks passed")
    say()
    return 0
