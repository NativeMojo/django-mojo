"""Actual primary-copy durability, original fences and controlled wire entry."""
import uuid

from testit import helpers as th


def journal_fixture(observer=None):
    from mojo.apps.dnsman.services import challenge_attempts as attempts
    token = uuid.uuid4().hex
    scope = {"provider": "route53", "zone": "Z-test-owned", "name": token + ".example", "type": "TXT"}
    owner = {"identity": {"kind": "test-owned-operation", "operation": token},
             "binding": {"domain": 1, "certificate": 1, "tenant_uuid": token}}
    return attempts.Journal(identity={"source_sha": "a" * 40, "manifest_sha256": "b" * 64}, observer=observer), scope, owner


@th.django_unit_test("journal append and receipt commit while uncommitted parent and owner fences remain held")
def test_independent_append_and_receipt_survive_outer_rollback(opts):
    from django.db import connection, transaction
    from django.test.utils import CaptureQueriesContext
    from mojo.apps.account.models import Group
    from mojo.apps.dnsman.models import Domain, Certificate
    from mojo.apps.dnsman.services import challenge_attempts as attempts, certs
    observed, open_copies = [], set()

    def observer(event, sql, copy):
        observed.append((event, sql))
        if event == "execute":
            open_copies.add(id(copy))
            assert copy.connection is not connection.connection, "Every ledger statement must use a different primary driver connection"
        else:
            open_copies.discard(id(copy))
            assert copy.connection is None, "The independent wrapper must actually close before returning"

    journal, scope, owner = journal_fixture(observer)
    group = Group.objects.create(name="challenge-journal-" + uuid.uuid4().hex)
    domain = Domain.objects.create(name=scope["name"], provider="route53", group=group, hosted_zone_id="Z-test-owned")
    sends = []

    class Rollback(Exception):
        pass

    try:
        try:
            with CaptureQueriesContext(connection) as queries:
                with transaction.atomic():
                    Domain.objects.select_for_update().get(pk=domain.pk)
                    # Uncommitted PK snapshots are intentional: a journal FK
                    # would self-wait on this transaction's parent insertion.
                    cert = Certificate.objects.create(domain=domain, common_name=domain.name)
                    owner["binding"].update(domain=domain.pk, certificate=cert.pk)
                    with certs._issue_lock(cert.pk) as owned:
                        assert owned, "The test caller must actually own its certificate advisory fence"
                        with attempts.scope_lock(scope) as anchor:
                            intent = journal.prepare("provider_mutation", scope, owner, "route53", {"values": ["test-private-txt"]}, anchor)
                            def wire():
                                assert not open_copies, "Every copied transaction/cursor must close before wire observation"
                                anchor.check()
                                sends.append("wire")
                                return "C-exact-test"
                            result = intent.send(wire)
                            intent.row = journal.receipt(intent.row, "accepted", change_id=result)
                            assert not open_copies, "Independent receipt CAS must close while original owner fences remain held"
                            anchor.check()
                            assert journal.project(owner=owner)[0]["provider_change_id"] == result, "Another committed connection must already see the exact receipt before outer commit"
                            raise Rollback()
        except Rollback:
            pass
        rows = journal.project(owner=owner)
        assert len(rows) == 1 and rows[0]["state"] == "accepted" and rows[0]["provider_change_id"] == "C-exact-test", "Caller rollback must preserve independently committed provider intent and receipt"
        assert sends == ["wire"] and not Certificate.objects.filter(pk=owner["binding"]["certificate"]).exists(), "Exactly one wire send must survive evidence-wise while the uncommitted parent rolls back"
        sql = [q["sql"].upper() for q in queries.captured_queries if "DNSMAN_CHALLENGE_ATTEMPT" in q["sql"].upper()]
        assert not any("FOR UPDATE" in q or q.lstrip().startswith(("INSERT", "UPDATE", "DELETE")) for q in sql), "The caller must never lock or mutate a journal row"
        assert any(event == "execute" and sql.startswith("INSERT") for event, sql in observed) and any(event == "execute" and sql.startswith("UPDATE") for event, sql in observed), "Actual independent INSERT and versioned receipt UPDATE must both execute"
    finally:
        domain.delete()
        group.delete()


@th.django_unit_test("controlled exact prewire abort cannot settle a different crashed prepared attempt")
def test_controlled_abort_is_exact_and_prepared_crash_stays_unknown(opts):
    from mojo.apps.dnsman.services import challenge_attempts as attempts
    journal, scope, owner = journal_fixture()
    sends = []
    with attempts.scope_lock(scope) as anchor:
        crashed = journal.prepare("provider_mutation", scope, owner, "route53", {"values": ["older"]}, anchor)
        # Losing this live Intent object leaves its prepared row possibly sent.
        crashed_id = crashed.row["attempt_id"]
        del crashed
        refused = journal.prepare("provider_mutation", scope, owner, "route53", {"values": ["newer"]}, anchor)
        def refuse():
            raise attempts.JournalError("stale_owner", 409)
        with th.assert_raises(attempts.JournalError):
            refused.send(lambda: sends.append("forbidden"), guard=refuse)
    rows = journal.project(owner=owner)
    assert [r["state"] for r in rows] == ["prepared", "aborted_before_send"], "Controlled no-send evidence must apply only to its own exact prepared attempt"
    assert rows[0]["attempt_id"] == crashed_id and not attempts.all_final(rows), "A crash-prepared older attempt remains unknown regardless of a later controlled abort"
    assert not sends, "A refused final guard must perform zero wire sends"


@th.django_unit_test("one intent permits one wire entry and never rewrites contradictory receipts")
def test_single_wire_entry_and_receipt_cas(opts):
    from mojo.apps.dnsman.services import challenge_attempts as attempts
    journal, scope, owner = journal_fixture()
    calls = []
    with attempts.scope_lock(scope) as anchor:
        intent = journal.prepare("provider_mutation", scope, owner, "route53", {"values": ["test"]}, anchor)
        intent.send(lambda: calls.append("wire"))
        with th.assert_raises(attempts.JournalError):
            intent.send(lambda: calls.append("replay"))
        accepted = journal.receipt(intent.row, "accepted", change_id="C-original")
        for state, kwargs in (("final", {"change_id": "C-other"}), ("rejected_no_change", {})):
            with th.assert_raises(attempts.JournalError):
                journal.receipt(accepted, state, **kwargs)
        final = journal.receipt(accepted, "final", change_id="C-original", receipt="c" * 64)
        assert final["version"] == 3 and attempts.all_final([final]), "Only the stored exact provider ID may reach final through versioned independent CAS"
    assert calls == ["wire"], "An intent must never hide an application mutation retry"


@th.django_unit_test("terminal barriers are independent and generic model writes are denied")
def test_barrier_survives_rollback_and_model_is_internal(opts):
    from django.db import transaction
    from mojo import errors as me
    from mojo.apps.dnsman.models import DnsChallengeAttempt
    from mojo.apps.dnsman.services import challenge_attempts as attempts
    journal, scope, owner = journal_fixture()
    try:
        with transaction.atomic():
            with attempts.scope_lock(scope) as anchor:
                barrier = journal.append("retirement_barrier", scope, owner, "route53", {"retired": True}, anchor.identity)
                raise RuntimeError("test-owned rollback")
    except RuntimeError:
        pass
    rows = journal.project(owner=owner, kinds=["retirement_barrier"])
    assert len(rows) == 1 and rows[0]["attempt_id"] == barrier["attempt_id"], "A caller rollback must never erase a permanent retirement barrier"
    with th.assert_raises(me.PermissionDeniedException):
        DnsChallengeAttempt().save()
    assert DnsChallengeAttempt.RestMeta.GRAPHS == {} and DnsChallengeAttempt.RestMeta.DENY_AI, "No generic REST graph or AI surface may expose journal values"


@th.django_unit_test("process death after actual HTTP receive preserves prepared evidence and rolls back caller rows")
def test_killed_wire_owner_keeps_independent_evidence(opts):
    import os
    import subprocess
    import sys
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from mojo.apps.account.models import Group
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import challenge_attempts as attempts
    token = uuid.uuid4().hex
    group = Group.objects.create(name="killed-challenge-" + token)
    domain = Domain.objects.create(group=group, name=token + ".example", provider="route53", status="pending")
    received, release = threading.Event(), threading.Event()
    bodies = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            bodies.append(self.rfile.read(int(self.headers["Content-Length"])))
            received.set()
            release.wait(10)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"accepted")
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    scope = {"provider": "route53", "name": token + ".example", "zone": "Z-test-owned"}
    owner = {"identity": {"kind": "killed-test", "operation": token}, "binding": {"domain": domain.pk}}
    code = """
import django, urllib.request
from django.db import transaction
django.setup()
from mojo.apps.dnsman.models import Domain
from mojo.apps.dnsman.services import challenge_attempts as a
scope, owner, pk, url = CONFIG
with transaction.atomic():
    d = Domain.objects.select_for_update().get(pk=pk)
    d.status = 'active'
    d.save(update_fields=['status'])
    with a.scope_lock(scope) as anchor:
        journal = a.Journal(identity={'source_sha':'a'*40,'manifest_sha256':'b'*64})
        intent = journal.prepare('provider_mutation',scope,owner,'route53',{'owned':'fictional'},anchor)
        intent.send(lambda: urllib.request.urlopen(urllib.request.Request(url,b'fictional-test-owned',method='POST'),timeout=10).read())
""".replace("CONFIG", repr((scope, owner, domain.pk, "http://127.0.0.1:%s/" % server.server_port)))
    env = dict(os.environ, DJANGO_SETTINGS_MODULE=os.environ.get("DJANGO_SETTINGS_MODULE", "settings"),
               PYTHONPATH=os.pathsep.join(str(path) for path in sys.path))
    process = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert received.wait(10), "The real local HTTP server must receive the one mutation before process death"
        journal = attempts.Journal(identity={"source_sha": "a" * 40, "manifest_sha256": "b" * 64})
        rows = journal.project(owner=owner)
        assert len(rows) == 1 and rows[0]["state"] == "prepared", "A separate connection must observe committed possibly-sent intent while the killed owner still holds its outer transaction"
        process.kill()
        process.wait(timeout=5)
        domain.refresh_from_db()
        assert domain.status == "pending", "Killing the wire owner must actually roll back the caller's Domain change"
        rows = journal.project(owner=owner)
        assert rows[0]["state"] == "prepared" and not attempts.all_final(rows), "Process death must not manufacture controlled no-send or finality evidence"
        assert bodies == [b"fictional-test-owned"], "One prepared intent must correspond to exactly one actual received HTTP request"
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        domain.delete()
        group.delete()


@th.django_unit_test("lost original PostgreSQL session refuses wire and commits exact controlled disposition")
def test_lost_backend_before_wire_is_not_reconnected(opts):
    from django.db import connection
    from mojo.apps.dnsman.services import challenge_attempts as attempts
    journal, scope, owner = journal_fixture()
    calls = []
    with attempts.scope_lock(scope) as anchor:
        intent = journal.prepare("provider_mutation", scope, owner, "route53", {"test": "owned"}, anchor)
        other = connection.copy(alias="test_owned_terminate")
        try:
            other.ensure_connection()
            with other.connection.cursor() as cursor:
                cursor.execute("SELECT pg_terminate_backend(%s)", [anchor.identity["pid"]])
                assert cursor.fetchone()[0], "A different PostgreSQL connection must actually terminate the original fence owner"
        finally:
            other.close()
        try:
            with th.assert_raises(attempts.JournalError):
                intent.send(lambda: calls.append("forbidden"))
        finally:
            connection.close()
    assert not calls, "A newly connected wrapper cannot inherit the lost original session's authority"
    rows = journal.project(owner=owner)
    assert rows[0]["state"] == "aborted_before_send", "This live invocation knows it never entered wire and must independently dispose only its exact prepared intent"
