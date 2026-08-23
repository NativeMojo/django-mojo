"""Boundary contracts for the root-owned publish broker and its client.

The broker's whole job is to let an unprivileged application explain its own
content writes without ever letting it name someone else's paths, complete
someone else's operation, or make a publish depend on the annotation working.
"""

import json
import os
import tempfile

from testit import helpers as th


def _fixture(base):
    """Return (content root, tenant root, journal) over one temporary estate."""
    from mojo.deploy.mojosec_changes import ChangeJournal

    content = os.path.join(base, "content")
    tenant = os.path.join(content, "acme")
    os.makedirs(os.path.join(tenant, "current"))
    journal = ChangeJournal(
        journal_path=os.path.join(base, "journal.json"),
        lock_path=os.path.join(base, "journal.lock"),
        manifest_path=os.path.join(base, "expected.json"),
        allowed_roots=(content,))
    return content, tenant, journal


@th.django_unit_test()
def test_publish_refuses_paths_outside_the_declaring_tenant(opts):
    from mojo.deploy import publish_broker as broker

    with tempfile.TemporaryDirectory() as base:
        base = os.path.realpath(base)
        content, tenant, journal = _fixture(base)
        sibling = os.path.join(content, "other")
        os.makedirs(sibling)

        for root in ("/etc", "/opt/api/x", sibling.replace("other", "other/deep"),
                     content, os.path.join(tenant, "current"),
                     os.path.join(base, "elsewhere")):
            with th.assert_raises(broker.PublishError):
                broker.execute(
                    {"operation": "begin", "root": root, "subtrees": ["current"]},
                    journal=journal, content_roots=(content,))
        th.assert_eq(journal.status()["active_operations"], 0,
                     "a refused declaration must never open a journal window")

        for name in ("../other", "/absolute", "a/b", "", ".", "..", "x" * 65):
            with th.assert_raises(broker.PublishError):
                broker.execute(
                    {"operation": "begin", "root": tenant, "subtrees": [name]},
                    journal=journal, content_roots=(content,))
        th.assert_eq(journal.status()["active_operations"], 0,
                     "a subtree name may only ever address a direct child")

        begun = broker.execute(
            {"operation": "begin", "root": tenant, "subtrees": ["current"]},
            journal=journal, content_roots=(content,))
        record = json.load(open(os.path.join(base, "journal.json"), encoding="utf-8"))
        paths = record["operations"][begun["operation_id"]]["paths"]
        th.assert_true(all(path.startswith(tenant + "/") or path == tenant
                           for path in paths),
                       "every derived path must live inside the declaring tenant")
        th.assert_true(content not in paths,
                       "the shared content root itself is never in one tenant's scope")


@th.django_unit_test()
def test_publish_refuses_unshaped_tenants_and_unenrolled_nodes(opts):
    from mojo.deploy import publish_broker as broker

    with tempfile.TemporaryDirectory() as base:
        base = os.path.realpath(base)
        content, tenant, journal = _fixture(base)
        for name in ("_certs", "_private", ".hidden", "a b", "a/b"):
            os.makedirs(os.path.join(content, name.replace("/", "_")), exist_ok=True)
            with th.assert_raises(broker.PublishError):
                broker.execute(
                    {"operation": "begin", "root": os.path.join(content, name),
                     "subtrees": ["current"]},
                    journal=journal, content_roots=(content,))

        for roots in ((), None):
            with th.assert_raises(broker.PublishError):
                broker.execute(
                    {"operation": "begin", "root": tenant, "subtrees": ["current"]},
                    journal=journal, content_roots=roots,
                    enrollment_path=os.path.join(base, "absent.json"))

        enrollment = os.path.join(base, "enrollment.json")
        with open(enrollment, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "fim_content_roots": []}, handle)
        os.chmod(enrollment, 0o600)
        with th.assert_raises(broker.PublishError):
            broker.load_content_roots(enrollment)
        with open(enrollment, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "fim_content_roots": [content]}, handle)
        os.chmod(enrollment, 0o600)
        th.assert_eq(broker.load_content_roots(enrollment), (content,),
                     "an enrolled node must read exactly its declared content roots")
        os.chmod(enrollment, 0o644)
        with th.assert_raises(broker.PublishError):
            broker.load_content_roots(enrollment)


@th.django_unit_test()
def test_publish_request_grammar_and_caller_are_exact(opts):
    from mojo.deploy import publish_broker as broker

    with th.assert_raises(broker.PublishError):
        broker.parse_request(b"")
    with th.assert_raises(broker.PublishError):
        broker.parse_request(b"[]")
    for payload in (
            b'{"operation":"run","root":"/opt/content/a","subtrees":["x"]}',
            b'{"operation":"begin","root":"/opt/content/a"}',
            b'{"operation":"begin","root":"/opt/content/a","subtrees":["x"],"argv":["sh"]}',
            b'{"operation":"end"}',
            b'{"operation":"begin","operation":"end"}'):
        with th.assert_raises(broker.PublishError):
            broker.parse_request(payload)
    parsed = broker.parse_request(
        b'{"operation":"begin","root":"/opt/content/a","subtrees":["x"],'
        b'"ttl_seconds":600}')
    th.assert_eq(parsed["ttl_seconds"], 600,
                 "an optional bounded TTL must be accepted")
    th.assert_eq(broker.main(["--help"]), 2,
                 "the broker must accept no arguments at all")
    th.assert_eq(broker.main(["anything"]), 2,
                 "argv is never a broker input surface")

    sudoers = broker.render_sudoers()
    th.assert_eq(sudoers,
                 'ec2-user ALL=(root) NOPASSWD: /usr/local/sbin/mojo-publish-broker ""\n',
                 "the grant must be the empty-argv broker and nothing else")
    for forbidden in ("ALL)", "*", "python", "rm ", "sh "):
        th.assert_true(forbidden not in sudoers,
                       f"the publish grant must not authorize {forbidden!r}")
    with th.assert_raises(broker.PublishError):
        broker.render_sudoers("ec2-user ALL=(root)")


@th.django_unit_test()
def test_publish_end_cannot_complete_another_producers_operation(opts):
    from mojo.deploy import publish_broker as broker
    from mojo.deploy.mojosec_changes import ChangeError

    with tempfile.TemporaryDirectory() as base:
        base = os.path.realpath(base)
        content, tenant, journal = _fixture(base)
        deploy_target = os.path.join(tenant, "current", "deployed.conf")
        with open(deploy_target, "w", encoding="utf-8") as handle:
            handle.write("root owned\n")
        journal.begin("post-deploy-77", "rendered-host-config", [deploy_target],
                      ttl_seconds=900)

        for operation in ("end", "abort"):
            with th.assert_raises(broker.PublishError):
                broker.execute(
                    {"operation": operation, "operation_id": "post-deploy-77"},
                    journal=journal, content_roots=(content,))
        th.assert_eq(journal.status()["active_operations"], 1,
                     "a publisher must not be able to touch a deploy's operation")

        # Even a correctly shaped publish id cannot complete a deploy: the
        # recorded kind is checked, not just the id's spelling.
        with th.assert_raises(ChangeError):
            journal.complete_derived(
                "post-deploy-77", expect_kind="content-publish")
        th.assert_eq(journal.status()["active_operations"], 1,
                     "a kind mismatch must leave the operation active and unannotated")
        th.assert_true(not os.path.exists(os.path.join(base, "expected.json")),
                       "a refused completion must annotate nothing at all")


@th.django_unit_test()
def test_publish_operations_can_never_starve_a_deploy(opts):
    from mojo.deploy import publish_broker as broker
    from mojo.deploy.mojosec_changes import (
        MAX_OPERATIONS, _KIND_OPERATION_BUDGETS, ChangeError,
    )

    budget = _KIND_OPERATION_BUDGETS["content-publish"]
    th.assert_true(budget < MAX_OPERATIONS,
                   "publishes must never be able to consume every journal slot")
    with tempfile.TemporaryDirectory() as base:
        base = os.path.realpath(base)
        content, _tenant, journal = _fixture(base)
        for index in range(budget):
            tenant = os.path.join(content, f"t{index}")
            os.makedirs(os.path.join(tenant, "current"))
            broker.execute(
                {"operation": "begin", "root": tenant, "subtrees": ["current"]},
                journal=journal, content_roots=(content,))
        th.assert_eq(journal.status()["active_operations"], budget,
                     "every publish inside the budget must be accepted")

        overflow = os.path.join(content, "overflow")
        os.makedirs(os.path.join(overflow, "current"))
        with th.assert_raises(ChangeError):
            broker.execute(
                {"operation": "begin", "root": overflow, "subtrees": ["current"]},
                journal=journal, content_roots=(content,))

        deploy_target = os.path.join(content, "t0", "current", "conf")
        with open(deploy_target, "w", encoding="utf-8") as handle:
            handle.write("x\n")
        journal.begin("post-deploy-1", "rendered-host-config", [deploy_target])
        th.assert_eq(journal.status()["active_operations"], budget + 1,
                     "a deploy must still be able to open its window at the publish cap")


@th.django_unit_test()
def test_manifest_overflow_frees_the_publish_slot(opts):
    from mojo.deploy.mojosec_changes import ChangeError, ChangeJournal

    with tempfile.TemporaryDirectory() as base:
        base = os.path.realpath(base)
        watched = os.path.join(base, "host.conf")
        # No entries fit, so this completion must overflow. The changes stay
        # unannotated — but the slot must not stay held until its TTL expires.
        journal = ChangeJournal(
            journal_path=os.path.join(base, "journal.json"),
            lock_path=os.path.join(base, "journal.lock"),
            manifest_path=os.path.join(base, "expected.json"),
            allowed_roots=(base,), max_manifest_entries=0)
        journal.begin("overflowing", "rendered-config", [watched])
        with open(watched, "w", encoding="utf-8") as handle:
            handle.write("changed\n")

        with th.assert_raises(ChangeError):
            journal.complete("overflowing")
        th.assert_eq(journal.status()["active_operations"], 0,
                     "an overflowing completion must still release its journal slot")
        manifest_path = os.path.join(base, "expected.json")
        entries = (json.load(open(manifest_path, encoding="utf-8"))["entries"]
                   if os.path.exists(manifest_path) else [])
        th.assert_eq(entries, [],
                     "an overflowing completion must annotate nothing")
        th.assert_true(
            not os.path.exists(manifest_path),
            "an overflowing completion must not rewrite the manifest at all")


@th.django_unit_test()
def test_publish_annotates_a_wide_subtree_and_a_delete(opts):
    from mojo.deploy import publish_broker as broker
    from mojo.deploy.mojosec_changes import _snapshot
    from mojo.mojosec.expected_changes import annotation, load_manifest

    with tempfile.TemporaryDirectory() as base:
        base = os.path.realpath(base)
        content, tenant, journal = _fixture(base)
        old = os.path.join(tenant, "rev-6")
        os.makedirs(old)
        with open(os.path.join(old, "retired.html"), "w", encoding="utf-8") as handle:
            handle.write("old release\n")

        begun = broker.execute(
            {"operation": "begin", "root": tenant,
             "subtrees": ["current", "rev-6", "rev-7"]},
            journal=journal, content_roots=(content,))

        new = os.path.join(tenant, "rev-7")
        os.makedirs(new)
        for index in range(4200):
            with open(os.path.join(new, f"asset-{index}.txt"), "w",
                      encoding="utf-8") as handle:
                handle.write(f"asset {index}\n")
        os.unlink(os.path.join(old, "retired.html"))
        os.rmdir(old)

        finished = broker.execute(
            {"operation": "end", "operation_id": begun["operation_id"]},
            journal=journal, content_roots=(content,))
        th.assert_true(finished["complete"],
                       "a subtree of this size must enumerate completely")
        th.assert_true(finished["annotated_paths"] > 4200,
                       "a wide publish must annotate past the narrow 4096 bound")

        entries = load_manifest(os.path.join(base, "expected.json"),
                                require_root=False)
        created = os.path.join(new, "asset-9.txt")
        th.assert_true(
            annotation(entries, created, "created", None, _snapshot(created),
                       correlation_seconds=900,
                       directory_metadata_roots=(content,)) is not None,
            "a file created during the window must be explainable")
        deleted = os.path.join(old, "retired.html")
        th.assert_true(
            any(item["path"] == deleted and item["change"] == "deleted"
                for item in entries),
            "a path removed inside the window must be annotated as deleted")
        th.assert_true(
            any(item["path"] == old and item["change"] == "deleted"
                for item in entries),
            "the retired revision directory itself must be annotated as deleted")


@th.django_unit_test()
def test_publish_window_never_raises_into_the_publish_path(opts):
    from mojo.deploy.publish_client import publish_window

    seen = []

    def broken(request, timeout):
        seen.append(request["operation"])
        raise RuntimeError("broker is not installed on this node")

    published = []
    with publish_window("/opt/content/acme", ["current"], runner=broken) as found:
        published.append("write happened")
    th.assert_eq(published, ["write happened"],
                 "a publish must run even when its explanation cannot be recorded")
    th.assert_eq(found, None, "a failed begin must yield no operation id")
    th.assert_eq(seen, ["begin"],
                 "without an operation there is nothing to end")

    replies = []

    def working(request, timeout):
        replies.append(request["operation"])
        return json.dumps({"ok": True,
                           "operation_id": "content-publish-" + "a" * 32}).encode()

    with publish_window("/opt/content/acme", ["current"], runner=working):
        pass
    th.assert_eq(replies, ["begin", "end"],
                 "a successful window must open and close exactly once")

    replies.clear()
    with th.assert_raises(ValueError):
        with publish_window("/opt/content/acme", ["current"], runner=working):
            raise ValueError("the publish itself failed")
    th.assert_eq(replies, ["begin", "abort"],
                 "a failed publish must abort its window and re-raise unchanged")

    for reply in (b"not json", b'{"ok":false}', b'"ok"'):
        replies.clear()
        with publish_window("/opt/content/acme", ["current"],
                            runner=lambda request, timeout, r=reply: r) as found:
            pass
        th.assert_eq(found, None,
                     "an unreadable or refused reply must never become an operation id")


@th.django_unit_test()
def test_producer_and_sensor_agree_on_relaxed_directory_evidence(opts):
    from mojo.deploy.mojosec_changes import _relaxed_directory_digest
    from mojo.mojosec.expected_changes import evidence_digest, relaxed_directory_digest

    entry = {"kind": "directory", "mode": 0o755, "uid": 0, "gid": 0,
             "size": 4096, "mtime_ns": 1, "ctime_ns": 2, "device": 3, "inode": 4}
    th.assert_eq(_relaxed_directory_digest(entry), relaxed_directory_digest(entry),
                 "the producer and the sensor must compute one identical digest")
    moved = dict(entry, mtime_ns=999, ctime_ns=999, size=8192, inode=77)
    th.assert_eq(relaxed_directory_digest(moved), relaxed_directory_digest(entry),
                 "a publish moves mtime, size and inode; those must not decide the match")
    for changed in ({"mode": 0o777}, {"uid": 1000}, {"gid": 1000}):
        th.assert_true(
            relaxed_directory_digest(dict(entry, **changed)) !=
            relaxed_directory_digest(entry),
            f"ownership and mode must still decide the match: {changed}")
    th.assert_true(
        relaxed_directory_digest(entry)[1] != evidence_digest(entry)[1],
        "a relaxed digest must never collide with a full one")
    for kind in ("file", "symlink", "other"):
        th.assert_eq(relaxed_directory_digest(dict(entry, kind=kind)), (None, None),
                     f"relaxation must never apply to a {kind} entry")
