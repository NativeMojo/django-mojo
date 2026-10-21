"""Source-bound shared setup, immutable adoption, writer races and renewal.

Every remote collaborator is an injected fake. No live provider, DNS or CA is
contacted. The single real lock race uses separate PostgreSQL connections.
"""
TESTIT_TIER = "extended"

import importlib
import json
import threading
import uuid
from datetime import timedelta
from functools import partial

from objict import objict
from testit import helpers as th


class Hub:
    def __init__(self):
        self.consumer = str(uuid.uuid4())
        self.target = f"{uuid.uuid4().hex}.hub.example"
        self.participants = {}
        self.calls = []
        self.alias = "match"
        self.one_hop = True
        self.problem = None
        self.lose_join = False
        self.publish_problem = False

    def capabilities(self):
        from mojo.apps.dnsman.services import acme_hub_client
        self.calls.append(("capabilities",))
        if self.problem == "unconfigured":
            from mojo.apps.dnsman.services.delegation_setup import SetupError
            raise SetupError("hub_unconfigured")
        if self.problem:
            raise acme_hub_client.AcmeHubResponseError("untrusted secret remote text", http_status=self.problem, kind="http")
        return objict(version=2, shared_validation=True, consumer_project_uuid=self.consumer,
                      can_consume=True, can_manage_trust=False, trust_scope="apex_and_wildcard")

    def lookup(self, domain, allocation_ref=None):
        self.calls.append(("lookup", domain, allocation_ref))
        return objict(items=[p for p in self.participants.values() if p.domain == domain and (not allocation_ref or p.allocation_ref == allocation_ref)], next_cursor=None)

    def grant(self, row):
        from mojo.helpers import dates
        participant = objict(allocation_ref=self.target, participant_ref=str(uuid.uuid4()), client_ref=str(row.client_ref),
                            domain=row.domain_name, source=f"_acme-challenge.{row.domain_name}", target=self.target,
                            role="trusted", trust_state="active", scope=[row.domain_name, f"*.{row.domain_name}"],
                            can_publish=True, can_withdraw=True, revoked_at=None,
                            alias=dict(state=self.alias, observed_target=self.target, one_hop=self.one_hop, checked_at=dates.utcnow().isoformat()))
        self.participants[str(row.client_ref)] = participant
        return participant

    def status(self, ref):
        from mojo.apps.dnsman.services import acme_hub_client
        from mojo.helpers import dates
        self.calls.append(("status", ref))
        if ref not in self.participants:
            raise acme_hub_client.AcmeHubResponseError("acme_not_authorized", kind="acme_not_authorized", http_status=404)
        p = objict(self.participants[ref])
        p.alias = dict(state=self.alias, observed_target=self.target if self.alias != "lookup_failed" else None,
                       one_hop=self.one_hop if self.alias != "lookup_failed" else None, checked_at=dates.utcnow().isoformat())
        return p

    def join(self, domain, ref, allocation_ref):
        from mojo.apps.dnsman.models import AcmeDelegation
        from mojo.apps.dnsman.services import acme_hub_client
        stored = AcmeDelegation.objects.get(client_ref=ref)
        assert stored.connection_mode == "shared" and stored.hub_consumer_project_uuid == self.consumer, "immutable shared intent must be committed before ambiguous join"
        self.calls.append(("join", ref, allocation_ref))
        if self.lose_join:
            self.lose_join = False
            raise acme_hub_client.AcmeHubTransportError("do not expose configured credentials")
        return acme_hub_client.AcmeHubAllocation(client_ref=ref, domain=domain, source=f"_acme-challenge.{domain}", target=allocation_ref)

    def publish(self, ref, challenge_ref, record_values):
        from mojo.apps.dnsman.services import acme_hub_client
        self.calls.append(("publish", ref, challenge_ref, sorted(record_values)))
        if self.publish_problem:
            raise acme_hub_client.AcmeHubTransportError("ambiguous publish")
        p = self.participants[ref]
        return acme_hub_client.AcmeHubChallengeResult(client_ref=ref, challenge_ref=challenge_ref,
            domain=p.domain, source=p.source, target=p.target, active_value_count=len(record_values))

    def withdraw(self, ref, challenge_ref):
        from mojo.apps.dnsman.services import acme_hub_client
        self.calls.append(("withdraw", ref, challenge_ref))
        p = self.participants[ref]
        return acme_hub_client.AcmeHubChallengeResult(client_ref=ref, challenge_ref=challenge_ref,
            domain=p.domain, source=p.source, target=p.target, active_value_count=0)


def fixture(provider="godaddy", with_domain=True):
    from mojo.apps.account.models import Group
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import delegation_setup as setup
    name = f"shared-{uuid.uuid4().hex[:12]}.example"
    group = Group.objects.create(name=f"shared_setup_{uuid.uuid4().hex}", kind="organization")
    domain = Domain.objects.create(group=group, name=name, provider=provider, status="active", verified=True,
                                   hosted_zone_id="preserve-zone", metadata={"preserve": "identity"}) if with_domain else None
    target = setup.resolve_target(domain=domain) if domain else setup.resolve_target(group=group, name=name)
    return objict(group=group, domain=domain, target=target, hub=Hub())


def prepare(env):
    from mojo.apps.dnsman.models import AcmeDelegation
    from mojo.apps.dnsman.services import delegation_setup as setup
    result = setup.connect(env.target, env.hub.target, client=env.hub, custody=lambda cert: True)
    return AcmeDelegation.objects.get(pk=result["connection"]["id"])


def verified(env):
    from mojo.apps.dnsman.services import delegation_setup as setup
    row = prepare(env)
    env.hub.grant(row)
    setup.connect(env.target, env.hub.target, client=env.hub, custody=lambda cert: True)
    setup.check(row, client=env.hub, custody=lambda cert: True, probe=lambda source, target: objict(ok=True, error=None))
    row.refresh_from_db()
    return row


def caught(code, action):
    from mojo.apps.dnsman.services import delegation_setup as setup
    try:
        action()
    except setup.SetupError as error:
        assert error.error_code == code, f"expected {code}, got {error.error_code}"
        assert "secret remote" not in str(error), "upstream exception text must never leave the setup service"
        return error
    raise AssertionError(f"expected safe setup refusal {code}")


def cert_for(env, due=False, names=None):
    from mojo.apps.dnsman.models import Certificate
    from mojo.helpers import dates
    return Certificate.objects.create(domain=env.domain, common_name=env.target.name,
        sans=names or [env.target.name, f"*.{env.target.name}"], status="active",
        cert_pem="preserved-public-material", mojo_secrets="preserved-encrypted-key",
        not_after=dates.utcnow() + timedelta(days=60),
        renew_after=dates.utcnow() + timedelta(days=-1 if due else 30))


@th.django_unit_test("plan and failed preflight preserve all local state and do no writes")
def test_plan_is_readonly(opts):
    from mojo.apps.dnsman.models import AcmeAccount, AcmeDelegation, Certificate
    from mojo.apps.jobs.models import Job
    from mojo.apps.dnsman.services import delegation_setup as setup
    env = fixture()
    counts = lambda: [m.objects.count() for m in (AcmeAccount, AcmeDelegation, Certificate, Job)]
    before = counts()
    group_uuid = env.group.uuid
    result = setup.plan(env.target, client=env.hub)
    env.group.refresh_from_db()
    assert counts() == before and env.group.uuid == group_uuid, "plan must not create rows or lazily assign a tenant UUID"
    assert result["connection"] is None and result["connection_request"] is None, "unprepared plan must not invent a client ref"
    assert all(call[0] in ("capabilities", "lookup") for call in env.hub.calls), "readiness must use read-only own-consumer I/O"
    for status, expected in ((404, "hub_unsupported"), (403, "hub_unauthorized"), (503, "hub_unavailable")):
        env.hub.problem = status
        error = caught(expected, lambda: setup.connect(env.target, env.hub.target, client=env.hub))
        assert error.status == 503 and counts() == before, "failed preflight must leave the direct route unchanged"


@th.django_unit_test("prepare, public descriptor, grant, replay and check adopt existing rows unchanged")
def test_adoption_preserves_domain_certificate_and_vhost(opts):
    from mojo.apps.edge.models import Vhost
    from mojo.apps.dnsman.services import delegation_setup as setup
    env = fixture(provider="route53")
    cert = cert_for(env)
    host = Vhost.objects.create(domain=env.domain, certificate=cert)
    domain_before = dict(env.domain.__dict__)
    cert_before = dict(cert.__dict__)
    row = prepare(env)
    descriptor = setup.plan(env.target, client=env.hub, custody=lambda cert: True)["connection_request"]
    assert set(descriptor) == {"version", "consumer_project_uuid", "client_ref", "domain", "allocation_ref"}, "public request must have exactly the five frozen fields"
    assert descriptor["consumer_project_uuid"] != row.tenant_uuid, "local tenant and configured hub consumer identity are separate namespaces"
    replay = setup.connect(env.target, env.hub.target, client=env.hub, custody=lambda cert: True)
    assert replay["connection"]["client_ref"] == str(row.client_ref), "missing-grant retries must retain the committed reference"
    env.hub.grant(row)
    connected = setup.connect(env.target, env.hub.target, client=env.hub, custody=lambda cert: True)
    assert connected["connection"]["state"] == "pending" and connected["next_action"]["code"] == "check", "join does not persist local proof"
    result = setup.check(row, client=env.hub, custody=lambda cert: True, probe=lambda *args: objict(ok=True))
    env.domain.refresh_from_db()
    cert.refresh_from_db()
    host.refresh_from_db()
    for field in ("provider", "group_id", "credential_id", "hosted_zone_id", "metadata", "modified"):
        assert getattr(env.domain, field) == domain_before[field], f"adoption must preserve Domain.{field}"
    for field in ("id", "cert_pem", "mojo_secrets", "renew_after", "modified"):
        assert getattr(cert, field) == cert_before[field], f"adoption must preserve Certificate.{field}"
    assert host.certificate_id == cert.pk and result["connection"]["state"] == "verified", "vhost references must survive verification"
    assert not any(c[0] == "allocate" for c in env.hub.calls), "shared setup can never allocate a replacement target"


@th.django_unit_test("readiness observations never demote persisted verified connections")
def test_read_failure_is_not_persisted(opts):
    from mojo.apps.dnsman.services import delegation_setup as setup
    env = fixture()
    row = verified(env)
    before = row.modified
    env.hub.alias = "mismatch"
    result = setup.plan(env.target, client=env.hub)
    row.refresh_from_db()
    assert row.state == "verified" and row.modified == before, "GET plan must not persist alias failure or touch timestamps"
    assert result["alias"]["state"] == "mismatch" and result["capabilities"]["can_request"] is False, "readiness must report the observed prerequisite failure"
    caught("alias_mismatch", lambda: setup.check(row, client=env.hub))
    row.refresh_from_db()
    assert row.state == "broken", "explicit Check must persist broken proof while preserving verified_at"


@th.django_unit_test("house domains are ineligible before any hub or row operation")
def test_house_is_readonly_ineligible(opts):
    from mojo.apps.dnsman.services import delegation_setup as setup
    env = fixture()
    env.domain.group = None
    env.domain.save(update_fields=["group"])
    target = setup.resolve_target(domain=env.domain)
    result = setup.plan(target, client=env.hub)
    assert result["domain"]["group"] is None and result["next_action"]["code"] == "unsupported_domain_scope", "house readiness must explicitly explain group-managed rollout scope"
    assert not any(result["capabilities"].values()) and result["connection_request"] is None, "house readiness has no mutation capabilities or descriptor"
    caught("unsupported_domain_scope", lambda: setup.connect(target, env.hub.target, client=env.hub))
    assert not env.hub.calls, "ineligible house target must be resolved before remote I/O"


@th.django_unit_test("foreign, inactive and key-override authority refuse before remote reads")
def test_authority_is_exact_and_underlying(opts):
    from mojo.apps.account.models import ApiKey
    from mojo.apps.dnsman.services import delegation_setup as setup
    from tests.test_dnsman._helpers import make_group_member, make_user
    env = fixture()
    user, _, _, own = make_group_member(["manage_dns"])
    try:
        setup.resolve_target(domain=env.domain, actor=user, write=True)
    except Exception as error:
        assert getattr(error, "status", None) in (403, 404), "foreign target must refuse without disclosure"
    else:
        raise AssertionError("a foreign domain must never resolve")
    admin, _, _ = make_user(is_superuser=True)
    key, _token = ApiKey.create_for_group(own, "shared-test-key", permissions={"manage_dns": True})
    request = objict(user=admin, api_key=key)
    assert setup.can_access(request, env.group, True) is False, "key-backed superuser override must retain underlying key group confinement"
    env.group.is_active = False
    env.group.save(update_fields=["is_active"])
    try:
        setup.plan(env.target, client=env.hub)
    except Exception as error:
        assert getattr(error, "status", None) in (403, 404), "inactive groups must refuse before remote reads"
    else:
        raise AssertionError("inactive target must never plan")
    assert not env.hub.calls, "authority rejection must precede all hub activity"


@th.django_unit_test("lost join reply replays the same immutable request and exact profile")
def test_ambiguous_join_replays(opts):
    from mojo.apps.dnsman.services import delegation_setup as setup
    env = fixture()
    row = prepare(env)
    env.hub.grant(row)
    env.hub.lose_join = True
    caught("hub_unavailable", lambda: setup.connect(env.target, env.hub.target, client=env.hub))
    result = setup.connect(env.target, env.hub.target, client=env.hub)
    assert result["connection"]["client_ref"] == str(row.client_ref), "lost join reply must not rotate reference"
    assert [call[1] for call in env.hub.calls if call[0] == "join"] == [str(row.client_ref)] * 2, "join must replay one exact persisted reference"
    wrong = f"other.{env.hub.target}"
    caught("allocation_conflict", lambda: setup.connect(env.target, wrong, client=env.hub))


@th.django_unit_test("consumer changes and revoked grants cannot authorize a join or CA call")
def test_identity_and_trust_fail_closed(opts):
    from mojo.apps.dnsman.services import delegation_setup as setup, certs
    env = fixture()
    row = verified(env)
    cert = cert_for(env, due=True)
    original = env.hub.consumer
    env.hub.consumer = str(uuid.uuid4())
    caught("hub_identity_changed", lambda: setup.request(row, client=env.hub))
    env.hub.consumer = original
    env.hub.participants[str(row.client_ref)].trust_state = "revoked"
    env.hub.participants[str(row.client_ref)].can_publish = False
    caught("trust_revoked", lambda: setup.request(row, client=env.hub))
    calls = []
    result = certs.issue(cert, acme=lambda: calls.append("CA"), hub_client=env.hub)
    cert.refresh_from_db()
    assert not result.ok and not calls and cert.cert_pem == "preserved-public-material", "revoked connection must block before CA while keeping valid serving material"


@th.django_unit_test("revoked restart reuses immutable target in a fresh isolated generation")
def test_restart_tombstones_and_immutability(opts):
    from mojo.apps.dnsman.models import AcmeDelegation
    from mojo.apps.dnsman.services import delegation_setup as setup, delegation
    env = fixture()
    row = verified(env)
    cert = cert_for(env)
    old_target = row.target_name
    env.hub.participants[str(row.client_ref)].trust_state = "revoked"
    env.hub.participants[str(row.client_ref)].can_publish = False
    result = setup.connect(env.target, env.hub.target, restart=True, client=env.hub, custody=lambda cert: True)
    new = AcmeDelegation.objects.get(pk=result["connection"]["id"])
    row.refresh_from_db()
    assert row.state == "retired" and new.client_ref != row.client_ref, "restart retires rather than revives an immutable ref"
    assert result["trust"]["state"] == "missing" and result["connection_request"]["allocation_ref"] == old_target, "fresh generation requires a new exact controller grant"
    env.hub.grant(new)
    setup.connect(env.target, env.hub.target, client=env.hub, custody=lambda cert: True)
    new.refresh_from_db()
    assert new.target_name == row.target_name, "historical generations must be able to retain the same permanent target"
    assert delegation.routing_for_domain(env.domain).pk == new.pk, "new pending guard must beat retired verified history"
    new.hub_consumer_project_uuid = str(uuid.uuid4())
    try:
        new.save()
    except Exception:
        pass
    else:
        raise AssertionError("prepared consumer binding must be immutable")
    cert.refresh_from_db()
    assert cert.mojo_secrets == "preserved-encrypted-key", "restart must preserve existing encrypted serving key"


@th.django_unit_test("canonical guard batching is one query and tombstones remain composable")
def test_canonical_routes(opts):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext
    from mojo.apps.dnsman.models import AcmeDelegation, Domain
    from mojo.apps.dnsman.services import delegation, delegation_setup as setup
    env = fixture()
    row = prepare(env)
    plain = fixture()
    with CaptureQueriesContext(connection) as queries:
        mapping = delegation.routing_for_domains([env.domain.pk, plain.domain.pk])
    assert len(queries) == 1 and mapping[env.domain.pk].pk == row.pk and plain.domain.pk not in mapping, "batch routing must execute one query with shared pending guards"
    assert Domain.objects.filter(pk__in=delegation.routed_domain_ids(), pk=env.domain.pk).exists(), "guard ids must compose as a subquery"
    AcmeDelegation.objects.filter(pk=row.pk).update(state="retired")
    assert delegation.routing_for_domain(env.domain) is None, "unverified retired pending request alone is inert"
    from mojo.helpers import dates
    AcmeDelegation.objects.filter(pk=row.pk).update(verified_at=dates.utcnow())
    assert delegation.routing_for_domain(env.domain).pk == row.pk, "ever-verified retired history must retain a deny guard"


@th.django_unit_test("pending shared intent refuses direct CA issuance and direct cleanup")
def test_pending_guard_blocks_direct_writer(opts):
    from mojo.apps.dnsman.services import certs
    env = fixture()
    prepare(env)
    cert = cert_for(env, due=True)
    called = []
    result = certs.issue(cert, acme=lambda: called.append("CA"), hub_client=env.hub)
    cert.refresh_from_db()
    assert not result.ok and not called and cert.status == "active", "pending guard must refuse before CA/provider paths and retain valid material"
    assert cert.attempts == 1 and cert.renew_after is not None, "failed renewal must retain established retry/backoff state"


@th.django_unit_test("current, due and queued wildcard requests reuse the existing exact lineage")
def test_request_idempotent_exact_profile(opts):
    from mojo.apps.dnsman.models import Certificate
    from mojo.apps.dnsman.services import delegation_setup as setup
    env = fixture()
    row = verified(env)
    cert = cert_for(env)
    published = []
    publish = lambda func, cert: published.append((func, cert.pk)) or "queued"
    current = setup.request(row, client=env.hub, custody=lambda cert: True, publish=publish)
    assert current["operation"]["outcome"] == "already_current" and not published, "current exact-profile row must not be reset or queued"
    from mojo.helpers import dates
    Certificate.objects.filter(pk=cert.pk).update(renew_after=dates.utcnow() - timedelta(days=1))
    due = setup.request(row, client=env.hub, custody=lambda cert: True, publish=publish)
    assert due["certificate"]["id"] == cert.pk and published[-1][1] == cert.pk and "renew_certificate" in published[-1][0], "due certificate must renew the same id"
    other = fixture()
    other_row = verified(other)
    cert_for(other, names=[other.target.name])
    caught("profile_conflict", lambda: setup.request(other_row, client=other.hub))


def _backend_pid():
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


def _wait_for_blocker(pid, expected=None):
    import time
    from django.db import connection
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_blocking_pids(%s)", [pid])
            blockers = cursor.fetchone()[0]
        if blockers and (expected is None or expected in blockers):
            return blockers
        time.sleep(0.01)
    raise AssertionError(f"backend {pid} must become a real PostgreSQL lock waiter")


@th.django_unit_test("Connect tries advisory locks before certificate row locks held by a real issuer")
def test_worker_handover_lock_order(opts):
    from django.db import close_old_connections, connection, transaction
    from mojo.apps.dnsman.models import Certificate, Domain
    from mojo.apps.dnsman.services import delegation_setup as setup, certs
    env = fixture()
    cert = cert_for(env)
    held, finished = threading.Event(), threading.Event()
    state, problems = {}, []

    def worker():
        close_old_connections()
        try:
            state["pid"] = _backend_pid()
            with certs._issue_lock(cert.pk) as acquired:
                assert acquired, "test worker must own the advisory issue lock"
                with transaction.atomic():
                    # A real issuer may have acquired Certificate before its
                    # joined Domain lock. It now waits on Connect's Domain.
                    Certificate.objects.select_for_update().get(pk=cert.pk)
                    held.set()
                    Domain.objects.select_for_update().get(pk=env.domain.pk)
        except Exception as error:
            problems.append(str(error))
        finally:
            close_old_connections()
            finished.set()

    with transaction.atomic():
        Domain.objects.select_for_update().get(pk=env.domain.pk)
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '2s'")
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            assert held.wait(5), "worker must own advisory and Certificate row locks"
            assert _wait_for_blocker(state["pid"], _backend_pid()), "issuer must really wait on the coordinator's Domain lock"
            # Reversing _prepare's advisory/Certificate order now blocks on
            # the worker and fails the test with a PostgreSQL lock timeout.
            caught("certificate_busy", lambda: setup.connect(env.target, env.hub.target, client=env.hub))
        finally:
            pass
    thread.join(6)
    assert finished.is_set() and not problems, f"handover must release the real issuer without deadlock: {problems}"
    assert not env.domain.acme_delegations.exists(), "busy refusal must not commit a new route guard"


@th.django_unit_test("cleanup uncertainty blocks preparation without abandoning the prior route")
def test_cleanup_blocks_prepare(opts):
    from mojo.apps.dnsman.services import delegation_setup as setup
    env = fixture()
    cert_for(env)
    def cleanup(domain, certificates, old):
        raise setup.SetupError("cleanup_pending")
    caught("cleanup_pending", lambda: setup.connect(env.target, env.hub.target, client=env.hub, cleanup=cleanup))
    assert not env.domain.acme_delegations.exists(), "cleanup failure must roll back the new pending route"


@th.django_unit_test("forged shape and alias chains never join or reach issuance")
def test_exact_alias_and_binding(opts):
    from mojo.apps.dnsman.services import delegation_setup as setup
    env = fixture()
    row = prepare(env)
    p = env.hub.grant(row)
    p.scope = [row.domain_name]
    caught("allocation_conflict", lambda: setup.connect(env.target, env.hub.target, client=env.hub))
    p.scope = [row.domain_name, f"*.{row.domain_name}"]
    for alias, hop, expected in (("mismatch", False, "alias_mismatch"), ("match", False, "alias_mismatch"), ("lookup_failed", None, "alias_lookup_failed")):
        env.hub.alias, env.hub.one_hop = alias, hop
        caught(expected, lambda: setup.connect(env.target, env.hub.target, client=env.hub))
    assert not any(call[0] == "join" for call in env.hub.calls), "forged scope and chained/missing aliases must refuse before join"


_RENEW = {}

def _renew_handler(job):
    from mojo.apps.dnsman import asyncjobs
    return asyncjobs.renew_certificate(job, issuer=_RENEW[job.payload["certificate"]])


@th.django_unit_test("scheduled renewal dispatches actual handler, preserves lineage and hands off installation")
def test_scheduled_renewal_and_cleanup(opts):
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job
    from mojo.apps.dnsman import cronjobs, asyncjobs
    from mojo.apps.dnsman.services import certs
    from mojo.helpers import dates
    env = fixture()
    row = verified(env)
    cert = cert_for(env, due=True)
    helpers = importlib.import_module("test_dnsman.6_certs_service")
    chain, _leaf = helpers._make_chain([env.target.name, f"*.{env.target.name}"])
    ca = helpers.FakeAcmeClient([env.target.name, env.target.name], chain=chain)
    syncs = []
    def store_key(certificate, pem):
        certificate._get_kms = lambda: helpers.FakeKMS()
        # Existing encrypted material uses this test-owned envelope on renewal.
        certificate.mojo_secrets = None
        certificate.set_private_key_pem(pem)
    issuer = partial(certs.issue, acme=lambda: (None, ca), hub_client=env.hub,
                     resolve_cname=lambda *args: objict(ok=True), store_key=store_key,
                     wait_for_txt=lambda name, values, **kwargs: (True, values),
                     sync=lambda current: syncs.append(current.pk))
    _RENEW[cert.pk] = issuer
    own_ids = []
    def publish(func, certificate, **kwargs):
        # A real durable Job and actual async renewal handler, scoped to one cert.
        if certificate.pk != cert.pk:
            return "unrelated-certificate-not-queued"
        job_id = jobs.publish(f"{__name__}._renew_handler", payload={"certificate": certificate.pk}, channel="default",
                             idempotency_key=f"shared-setup-test-{uuid.uuid4().hex}")
        own_ids.append(job_id)
        return job_id
    try:
        cronjobs.renew_certificates(renew=lambda: certs.renew_due(publish=publish))
        assert own_ids and Job.objects.filter(pk=own_ids[0], payload__certificate=cert.pk).exists(), "cron must queue a durable own-certificate renewal job"
        th.run_pending_jobs(func=f"{__name__}._renew_handler", payload={"certificate": cert.pk})
        cert.refresh_from_db()
        row.refresh_from_db()
        assert cert.status == "active" and syncs == [cert.pk] and cert.cert_pem.startswith("-----BEGIN CERTIFICATE-----"), "actual scheduled handler must replace material in the same lineage and notify its installer owner"
        published = [c for c in env.hub.calls if c[0] == "publish"]
        withdrawn = [c for c in env.hub.calls if c[0] == "withdraw"]
        assert len(published[-1][3]) == 2 and withdrawn[-1][2] == published[-1][2], "apex/wildcard digests must publish simultaneously and cleanup the same lease"
        assert row.cleanup_challenge_ref is None, "successful renewal must clear durable cleanup intent"
        # The real installation handoff schedules edge convergence; a recorder
        # is supplied at its existing queue boundary in the second half below.
        result = asyncjobs.certificate_updated(objict(payload={"certificate": cert.pk}), publish=lambda **kwargs: syncs.append(kwargs))
        assert "completed" in result and syncs[-1]["func"].endswith(".converge"), "certificate update must hand off to the existing edge convergence installer"
    finally:
        _RENEW.pop(cert.pk, None)
        Job.objects.filter(pk__in=own_ids).delete()


@th.django_unit_test("ambiguous publish cleans only its lease and preserves valid material on failure")
def test_failed_renewal_keeps_serving_material(opts):
    from mojo.apps.dnsman.services import certs
    env = fixture()
    row = verified(env)
    cert = cert_for(env, due=True)
    helpers = importlib.import_module("test_dnsman.6_certs_service")
    ca = helpers.FakeAcmeClient([env.target.name, env.target.name])
    env.hub.publish_problem = True
    result = certs.issue(cert, acme=lambda: (None, ca), hub_client=env.hub,
        resolve_cname=lambda *args: objict(ok=True), wait_for_txt=lambda *args, **kwargs: (True, []))
    cert.refresh_from_db()
    row.refresh_from_db()
    pubs = [c for c in env.hub.calls if c[0] == "publish"]
    clears = [c for c in env.hub.calls if c[0] == "withdraw"]
    assert not result.ok and cert.status == "active" and cert.mojo_secrets == "preserved-encrypted-key" and cert.cert_pem == "preserved-public-material", "ambiguous failure must preserve old valid material"
    assert pubs and clears and pubs[-1][1:3] == clears[-1][1:3] and row.cleanup_challenge_ref is None, "ambiguous publish cleanup must use its own original immutable client/challenge refs"


@th.django_unit_test("Readiness v1 representative fixture serialization has no credentials or private material")
def test_readiness_fixture_serialization(opts):
    from mojo.apps.dnsman.services import delegation_setup as setup
    env = fixture()
    examples = {}
    for status, label in (("unconfigured", "unconfigured"), (404, "unsupported"), (503, "unavailable")):
        env.hub.problem = status
        examples[label] = setup.plan(env.target, client=env.hub)
    env.hub.problem = None
    row = prepare(env)
    examples["awaiting_trust"] = setup.plan(env.target, client=env.hub)
    env.hub.grant(row)
    examples["connected_unverified"] = setup.connect(env.target, env.hub.target, client=env.hub)
    examples["verified"] = setup.check(row, client=env.hub, probe=lambda *args: objict(ok=True))
    cert = cert_for(env)
    examples["already_current"] = setup.request(row, client=env.hub, custody=lambda cert: True)
    cert.last_error = "private remote exception must not be serialized"
    cert.attempts = 2
    cert.save(update_fields=["last_error", "attempts"])
    examples["renewal_failed"] = setup.plan(env.target, client=env.hub, custody=lambda cert: True)
    env.hub.participants[str(row.client_ref)].trust_state = "revoked"
    env.hub.participants[str(row.client_ref)].can_publish = False
    examples["revoked"] = setup.plan(env.target, client=env.hub, custody=lambda cert: True)
    house = fixture()
    house.domain.group = None
    house.domain.save(update_fields=["group"])
    examples["house_group_none"] = setup.plan(setup.resolve_target(domain=house.domain), client=house.hub)
    expected = {"version", "checked_at", "domain", "profile", "hub", "connection", "trust", "alias", "certificate", "renewal", "capabilities", "next_action", "connection_request", "operation"}
    for label, dto in examples.items():
        assert set(dto) == expected and dto["version"] == 1, f"{label} must preserve exact Readiness v1 top-level shape"
    encoded = json.dumps(dict(provenance="fixture serialization through production readiness service; not live receipts", responses=examples), sort_keys=True, indent=2)
    for forbidden in ("private_key_pem", "cert_pem", "mojo_secrets", "API_KEY", "private remote exception"):
        assert forbidden not in encoded, f"representative DTOs must not expose {forbidden}"
    with open("/tmp/maestro-8939-representative-responses.json", "w") as handle:
        handle.write(encoded + "\n")


@th.django_unit_test("REST returns exact readiness and normal safe prerequisite envelopes without writes")
def test_rest_readiness_and_prerequisite_envelope(opts):
    from mojo.apps.dnsman.models import AcmeAccount, AcmeDelegation, Certificate
    from tests.test_dnsman._helpers import make_group_member, login
    env = fixture()
    user, email, password, _group = make_group_member(["manage_dns"], group=env.group)
    login(opts, email, password)
    before = [model.objects.count() for model in (AcmeAccount, AcmeDelegation, Certificate)]
    result = opts.client.get(f"/api/dnsman/delegation/plan?domain={env.domain.pk}")
    assert result.status_code == 200 and result.response.data.version == 1, "authorized REST plan must expose the shared versioned serializer"
    assert result.response.data.hub.state == "unconfigured", "a test deployment without static hub access must report unconfigured"
    connect = opts.client.post("/api/dnsman/delegation/connect", dict(domain=env.domain.pk, allocation_ref=env.hub.target))
    assert connect.status_code == 503 and connect.response.error_code == "hub_unconfigured" and connect.response.data.version == 1, "explicit action prerequisite failure must use normal false envelope with Readiness"
    forged = opts.client.post("/api/dnsman/delegation/request", dict(delegation=1, force=True, api_key="fake-do-not-echo"))
    assert forged.status_code == 400 and "fake-do-not-echo" not in str(forged.response), "forged mutation inputs must fail shape validation without credential reflection"
    giant_id = opts.client.get("/api/dnsman/delegation/plan?domain=" + "9" * 5000)
    giant_size = opts.client.get("/api/dnsman/delegation/group-choice?size=" + "9" * 5000)
    assert giant_id.status_code == giant_size.status_code == 400, "oversized integer shapes must be bounded 400 errors before conversion or remote I/O"
    assert [model.objects.count() for model in (AcmeAccount, AcmeDelegation, Certificate)] == before, "read/configuration refusal must leave all local account/connection/certificate counts unchanged"
    user.delete()


@th.django_unit_test("retired verified references never revive when stale Check fails")
def test_stale_check_preserves_retired_tombstone(opts):
    from mojo.apps.dnsman.services import delegation_setup as setup
    env = fixture()
    old = verified(env)
    env.hub.participants[str(old.client_ref)].trust_state = "revoked"
    env.hub.participants[str(old.client_ref)].can_publish = False
    successor = setup.connect(env.target, env.hub.target, restart=True, client=env.hub)
    caught("stale_connection", lambda: setup.check(old, client=env.hub))
    old.refresh_from_db()
    assert old.state == "retired" and successor["connection"]["state"] == "pending", "a stale Check must not turn a permanent tombstone into a live row"


@th.django_unit_test("REST group choices and plans retain viewer, foreign and override-key confinement")
def test_rest_authority_and_legacy_contracts(opts):
    from mojo.apps.account.models import ApiKey, GroupMember
    from mojo.apps.dnsman.models import AcmeDelegation
    from tests.test_dnsman._helpers import make_group_member, make_user, login
    env, foreign = fixture(), fixture()
    user, email, password, _ = make_group_member(["manage_dns"], group=env.group)
    login(opts, email, password)
    choice = opts.client.get(f"/api/dnsman/delegation/group-choice?id={env.group.pk}")
    hidden = opts.client.get(f"/api/dnsman/delegation/group-choice?id={foreign.group.pk}")
    assert choice.status_code == 200 and choice.response.data[0].id == env.group.pk and hidden.response.data == [], "chooser must return minimal own active groups and no foreign ids"
    assert set(choice.response.data[0]) == {"id", "name"}, "chooser must not expose credential or unrelated group fields"
    denied = opts.client.get(f"/api/dnsman/delegation/plan?domain={foreign.domain.pk}")
    unknown = opts.client.get("/api/dnsman/delegation/plan?domain=999999999")
    assert denied.status_code == unknown.status_code == 404, "foreign and inaccessible ids must share a non-oracular refusal"
    legacy = AcmeDelegation.objects.create(domain=env.domain, group=env.group,
        tenant_uuid=env.group.get_uuid(), tenant_name=env.group.name, domain_name=env.domain.name)
    old = opts.client.get(f"/api/dnsman/delegation/{legacy.pk}")
    assert set(old.response.data) == {"id", "created", "modified", "domain", "domain_name", "source", "target", "state", "verified_at", "last_error_code"}, "legacy delegation detail shape must remain unchanged"
    assert legacy.connection_mode == "allocated", "additive migration/model default must retain legacy allocated mode"
    viewer, vemail, vpassword, _ = make_group_member(["view_dns"], group=env.group)
    login(opts, vemail, vpassword)
    dto = opts.client.get(f"/api/dnsman/delegation/plan?domain={env.domain.pk}")
    assert dto.status_code == 200 and not any(dto.response.data.capabilities.values()) and dto.response.data.connection_request is None, "view-only readiness must expose no descriptor or mutation capability"
    refused = opts.client.post("/api/dnsman/delegation/connect", dict(domain=env.domain.pk, allocation_ref=env.hub.target))
    assert refused.status_code == 403, "a DNS viewer must never prepare or preflight a mutation"
    admin, _, _ = make_user(perms=["view_dns", "manage_dns"])
    GroupMember.objects.get_or_create(group=env.group, user=admin)
    key, token = ApiKey.create_for_group(env.group, "shared-http-override", permissions={"manage_dns": True}, user=admin, override_user=True)
    opts.client.logout()
    opts.client.session.headers["Authorization"] = f"apikey {token}"
    try:
        own = opts.client.get(f"/api/dnsman/delegation/plan?domain={env.domain.pk}")
        other = opts.client.get(f"/api/dnsman/delegation/plan?domain={foreign.domain.pk}")
        groups = opts.client.get(f"/api/dnsman/delegation/group-choice?id={foreign.group.pk}")
        assert own.status_code == 200 and other.status_code in (403, 404) and groups.response.data == [], "HTTP override User must remain confined to the underlying key's group"
    finally:
        opts.client.session.headers.pop("Authorization", None)
        key.delete()
        viewer.delete()
        user.delete()
        admin.delete()


@th.django_unit_test("Check finishing after restart returns stale without reviving the retired generation")
def test_check_restart_barrier(opts):
    from django.db import close_old_connections
    from mojo.apps.dnsman.models import AcmeDelegation
    from mojo.apps.dnsman.services import delegation_setup as setup
    env = fixture()
    old = verified(env)
    proof_started, resume = threading.Event(), threading.Event()
    outcomes = []
    def probe(*args):
        proof_started.set()
        assert resume.wait(5), "coordinator must release the pre-restart alias proof"
        return objict(ok=True)
    def checker():
        close_old_connections()
        try:
            setup.check(old.pk, client=env.hub, probe=probe)
            outcomes.append("unexpected_verification")
        except setup.SetupError as error:
            outcomes.append((error.error_code, error.status))
        except Exception as error:
            outcomes.append(type(error).__name__)
        finally:
            close_old_connections()
    thread = threading.Thread(target=checker)
    thread.start()
    try:
        assert proof_started.wait(5), "Check must pause after preflight and before persisted proof"
        env.hub.participants[str(old.client_ref)].trust_state = "revoked"
        env.hub.participants[str(old.client_ref)].can_publish = False
        result = setup.connect(env.target, env.hub.target, restart=True, client=env.hub)
    finally:
        resume.set()
        thread.join(6)
    old.refresh_from_db()
    successor = AcmeDelegation.objects.get(pk=result["connection"]["id"])
    assert outcomes == [("stale_connection", 409)], f"stale Check must return a bounded contract error, never a 500: {outcomes}"
    assert old.state == "retired" and successor.state == "pending" and old.client_ref != successor.client_ref, "old immutable authority must never revive beside its successor"


@th.django_unit_test("normal Connect replay and Check complete while contending on real lifecycle locks")
def test_connect_check_lock_order(opts):
    from django.db import close_old_connections, connection, transaction
    from mojo.apps.account.models import Group
    from mojo.apps.dnsman.services import delegation_setup as setup
    env = fixture()
    row = verified(env)
    states, errors, pids = {}, [], {}
    started = {key: threading.Event() for key in ("connect", "check")}
    def runner(kind):
        close_old_connections()
        try:
            pids[kind] = _backend_pid()
            started[kind].set()
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '3s'")
                if kind == "connect":
                    states[kind] = setup.connect(env.target, env.hub.target, client=env.hub)
                else:
                    states[kind] = setup.check(row.pk, client=env.hub, probe=lambda *args: objict(ok=True))
        except Exception as error:
            errors.append((kind, type(error).__name__, str(error)))
        finally:
            close_old_connections()
    connect_thread = threading.Thread(target=runner, args=("connect",))
    check_thread = threading.Thread(target=runner, args=("check",))
    with transaction.atomic():
        Group.objects.select_for_update().get(pk=env.group.pk)
        connect_thread.start()
        assert started["connect"].wait(5), "Connect backend must start"
        assert _wait_for_blocker(pids["connect"], _backend_pid()), "Connect must hold Domain while genuinely waiting for Group"
        check_thread.start()
        assert started["check"].wait(5), "Check backend must start"
        assert _wait_for_blocker(pids["check"]), "Check must enter real lifecycle contention before Group release"
    connect_thread.join(6)
    check_thread.join(6)
    row.refresh_from_db()
    assert not connect_thread.is_alive() and not check_thread.is_alive() and not errors, f"Domain/Group/Delegation ordering must complete both paths without deadlock: {errors}"
    assert set(states) == {"connect", "check"} and row.state == "verified", "normal replay and Check must retain the same verified generation"


@th.django_unit_test("first legacy initiate cannot invert Domain and Group locks against shared Connect")
def test_legacy_initiate_shared_connect_lock_order(opts):
    from django.db import close_old_connections, connection, transaction
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import delegation, delegation_setup as setup
    env = fixture()
    env.group.get_uuid()
    states, errors, pids = {}, [], {}
    started = {key: threading.Event() for key in ("connect", "legacy")}
    def runner(kind):
        close_old_connections()
        try:
            pids[kind] = _backend_pid()
            started[kind].set()
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL lock_timeout = '3s'")
                if kind == "connect":
                    states[kind] = setup.connect(env.target, env.hub.target, client=env.hub)
                else:
                    try:
                        delegation.initiate(env.group, None, domain=env.domain, available=True)
                    except Exception as error:
                        if getattr(error, "status", None) == 400 and "shared connection setup" in str(error):
                            states[kind] = "shared_route_refused"
                        else:
                            raise
        except Exception as error:
            errors.append((kind, type(error).__name__, str(error)))
        finally:
            close_old_connections()
    connect_thread = threading.Thread(target=runner, args=("connect",))
    legacy_thread = threading.Thread(target=runner, args=("legacy",))
    with transaction.atomic():
        Domain.objects.select_for_update().get(pk=env.domain.pk)
        connect_thread.start()
        assert started["connect"].wait(5), "shared Connect backend must start"
        assert _wait_for_blocker(pids["connect"], _backend_pid()), "Connect must queue first for the held Domain"
        legacy_thread.start()
        assert started["legacy"].wait(5), "legacy initiate backend must start"
        assert _wait_for_blocker(pids["legacy"]), "legacy initiate must contend before the held Domain is released"
    connect_thread.join(6)
    legacy_thread.join(6)
    assert not errors and not connect_thread.is_alive() and not legacy_thread.is_alive(), f"legacy/shared collision must not deadlock or reach provider allocation: {errors}"
    assert states.get("legacy") == "shared_route_refused" and states["connect"]["connection"]["state"] == "pending", "first prepared shared generation must win and retain its pending deny guard"


@th.django_unit_test("wildcard Request does not wait on an active issuer's Certificate row while holding Domain")
def test_request_worker_lock_order(opts):
    from django.db import close_old_connections, connection, transaction
    from mojo.apps.dnsman.models import Certificate, Domain
    from mojo.apps.dnsman.services import delegation_setup as setup, certs
    env = fixture()
    row = verified(env)
    cert = cert_for(env, due=True)
    held, state, errors = threading.Event(), {}, []
    def worker():
        close_old_connections()
        try:
            state["pid"] = _backend_pid()
            with certs._issue_lock(cert.pk) as acquired:
                assert acquired, "test issuer must own advisory lock"
                with transaction.atomic():
                    Certificate.objects.select_for_update().get(pk=cert.pk)
                    held.set()
                    Domain.objects.select_for_update().get(pk=env.domain.pk)
        except Exception as error:
            errors.append(str(error))
        finally:
            close_old_connections()
    with transaction.atomic():
        Domain.objects.select_for_update().get(pk=env.domain.pk)
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '2s'")
        thread = threading.Thread(target=worker)
        thread.start()
        assert held.wait(5), "worker must own its Certificate row"
        assert _wait_for_blocker(state["pid"], _backend_pid()), "worker must genuinely wait on the held Domain"
        published = []
        result = setup.request(row, client=env.hub, custody=lambda cert: True,
                               publish=lambda *args: published.append(args))
        assert result["operation"]["outcome"] == "already_queued" and not published, "Request must observe active advisory ownership before locking Certificate rows or queuing duplicates"
    thread.join(6)
    assert not thread.is_alive() and not errors, f"Request must release the active issuer without deadlock: {errors}"


BUSY_CHANNEL = "testit_dnsman_cert_jobs"


@th.django_unit_test("pending and issuing Request outcomes follow runnable jobs or an observed issue owner")
def test_request_pending_job_matrix(opts):
    from django.db import close_old_connections
    from mojo.apps.jobs.models import Job
    from mojo.apps.dnsman.services import certs, delegation_setup as setup
    from mojo.helpers import dates
    env = fixture()
    row = verified(env)
    cert = cert_for(env, due=True)
    job_id = certs.publish_job(certs.ISSUE_JOB, cert, channel=BUSY_CHANNEL)
    held, release = threading.Event(), threading.Event()
    errors = []
    def owner():
        close_old_connections()
        try:
            with certs._issue_lock(cert.pk) as acquired:
                assert acquired, "test issuer must actually acquire its advisory guard"
                held.set()
                assert release.wait(8), "Request must finish before the actual owner is released"
        except Exception as error:
            errors.append(str(error))
        finally:
            close_old_connections()
    thread = None
    try:
        for certificate_state in ("pending", "issuing"):
            type(cert).objects.filter(pk=cert.pk).update(status=certificate_state)
            for state in ("pending", "running", "canceled", "expired", "failed", "completed"):
                Job.objects.filter(pk=job_id).update(status=state, last_error="ordinary_ca_failure" if state == "failed" else "",
                    expires_at=dates.utcnow() + timedelta(days=-1 if state == "expired" else 1))
                result = setup.request(row, client=env.hub, custody=lambda certificate: True)
                runnable = state in ("pending", "running")
                expected = "already_queued" if runnable else "job_held"
                assert result["operation"] == dict(action="request", outcome=expected, changed=False), f"{certificate_state} certificate with {state} job must report only runnable work as queued"
                renewal_state = {"pending": "queued", "running": "issuing", "failed": "failed"}.get(state, "blocked")
                assert result["renewal"]["state"] == renewal_state and result["renewal"]["job"] == dict(id=job_id, state=state), "Request operation and readiness must describe the same real job"
                assert result["next_action"]["code"] == ("wait_for_job" if runnable else "inspect_failure"), "held terminal work needs inspection rather than waiting on a job that cannot run"
                assert result["capabilities"]["can_request"] is runnable, "Readiness must disable a pending/issuing Request that cannot dispatch work"
                assert Job.objects.filter(payload__certificate=cert.pk).count() == 1 and Job.objects.get(pk=job_id).status == state, "Request must not publish replacements or revive terminal work in this matrix"
        Job.objects.filter(pk=job_id).delete()
        for certificate_state in ("pending", "issuing"):
            type(cert).objects.filter(pk=cert.pk).update(status=certificate_state)
            result = setup.request(row, client=env.hub, custody=lambda certificate: True)
            assert result["operation"] == dict(action="request", outcome="job_held", changed=False) and result["renewal"]["job"] is None, "a pending/issuing row without any Job must not claim queued work"
            assert result["renewal"]["state"] == "blocked" and result["next_action"]["code"] == "inspect_failure", "a missing worker handoff must be visible as a blocker"
        thread = threading.Thread(target=owner)
        thread.start()
        assert held.wait(5), "independent issuer must hold the real advisory guard"
        result = setup.request(row, client=env.hub, custody=lambda certificate: True)
        assert result["operation"] == dict(action="request", outcome="already_queued", changed=False), "observed active issue ownership takes precedence over a missing Job"
        assert result["renewal"]["state"] == "issuing" and result["next_action"]["code"] == "wait_for_job", "Request must retain its actual live-owner observation in readiness"
        cert.refresh_from_db()
        assert cert.cert_pem == "preserved-public-material" and cert.mojo_secrets == "preserved-encrypted-key" and cert.attempts == 0, "the full matrix must preserve serving material and CA backoff"
    finally:
        release.set()
        if thread:
            thread.join(6)
        Job.objects.filter(pk=job_id).delete()
    assert not errors and not thread.is_alive(), f"actual issue owner must release cleanly: {errors}"


@th.django_unit_test("Request reports preserved terminal jobs without claiming queued work")
def test_request_preserved_terminal_job_outcomes(opts):
    from mojo.apps.jobs.models import Job
    from mojo.apps.dnsman.services import certs, delegation_setup as setup
    from mojo.helpers import dates
    env = fixture()
    row = verified(env)
    cert = cert_for(env, due=True)
    before = dict(cert.__dict__)
    job_id = certs.publish_job(certs.RENEW_JOB, cert, channel=BUSY_CHANNEL)
    try:
        for state, error in (("expired", ""), ("failed", "ordinary_ca_failure"),
                             ("canceled", certs.ISSUE_BUSY_ERROR), ("completed", "")):
            Job.objects.filter(pk=job_id).update(status=state, last_error=error,
                                                expires_at=dates.utcnow() - timedelta(seconds=1))
            count = Job.objects.filter(payload__certificate=cert.pk).count()
            result = setup.request(row, client=env.hub, custody=lambda certificate: True,
                publish=lambda func, certificate: certs.publish_job(func, certificate, channel=BUSY_CHANNEL))
            job = Job.objects.get(pk=job_id)
            cert.refresh_from_db()
            assert result["operation"] == dict(action="request", outcome="job_held", changed=False), f"preserved {state} job must not claim it queued work"
            assert result["renewal"]["job"] == dict(id=job_id, state=state), "Readiness must retain the actual terminal job identity and state"
            assert job.status == state and job.last_error == error and Job.objects.filter(payload__certificate=cert.pk).count() == count, "Request must preserve cancellation, unrelated failure and expiration without publishing a replacement"
            for field in ("id", "cert_pem", "mojo_secrets", "renew_after", "attempts", "modified"):
                assert getattr(cert, field) == before[field], f"held work must preserve Certificate.{field}"
    finally:
        Job.objects.filter(pk=job_id).delete()


def _issuer_fixture(env, syncs):
    from mojo.apps.dnsman.services import certs
    helpers = importlib.import_module("test_dnsman.6_certs_service")
    chain, _leaf = helpers._make_chain([env.target.name, f"*.{env.target.name}"])
    ca = helpers.FakeAcmeClient([env.target.name, env.target.name], chain=chain)
    def store_key(certificate, pem):
        certificate._get_kms = lambda: helpers.FakeKMS()
        certificate.mojo_secrets = None
        certificate.set_private_key_pem(pem)
    return partial(certs.issue, acme=lambda: (None, ca), hub_client=env.hub,
                   resolve_cname=lambda *args: objict(ok=True), store_key=store_key,
                   wait_for_txt=lambda name, values, **kwargs: (True, values),
                   sync=lambda certificate: syncs.append(certificate.pk)), ca


def _execute_job_thread(job_id, errors):
    from django.db import close_old_connections
    from mojo.apps.jobs.job_engine import JobEngine
    close_old_connections()
    try:
        engine = JobEngine(channels=[BUSY_CHANNEL], runner_id=f"shared-{uuid.uuid4().hex}-engine", max_workers=1)
        engine.execute_job(BUSY_CHANNEL, job_id)
    except Exception as error:
        errors.append((type(error).__name__, str(error)))
    finally:
        close_old_connections()


@th.django_unit_test("Request releases temporary issue ownership before an on_commit worker can claim its job")
def test_request_on_commit_handoff(opts):
    from django.db import transaction
    from mojo.apps.jobs.models import Job
    from mojo.apps.dnsman.services import certs, delegation_setup as setup
    env = fixture()
    row = verified(env)
    cert = cert_for(env, due=True)
    syncs, errors, job_ids = [], [], []
    issuer, ca = _issuer_fixture(env, syncs)
    _RENEW[cert.pk] = issuer
    def publish(func, certificate):
        job_id = certs.publish_job(f"{__name__}._renew_handler", certificate, channel=BUSY_CHANNEL)
        job_ids.append(job_id)
        def worker_at_commit():
            thread = threading.Thread(target=_execute_job_thread, args=(job_id, errors))
            thread.start()
            thread.join(6)
            assert not thread.is_alive(), "commit-exposed worker must complete while on_commit is still running"
        transaction.on_commit(worker_at_commit)
        return job_id
    try:
        result = setup.request(row, client=env.hub, custody=lambda certificate: True, publish=publish)
        cert.refresh_from_db()
        job = Job.objects.get(pk=job_ids[0])
        assert not errors and ca.orders and syncs == [cert.pk] and job.status == "completed", f"on_commit must expose work after temporary advisory release, never swallow renewal as skipped: {errors}, job={job.status}, orders={ca.orders}"
        assert cert.cert_pem.startswith("-----BEGIN CERTIFICATE-----") and result["certificate"]["id"] == cert.pk, "same-lineage material must actually renew before reporting successful worker completion"
    finally:
        _RENEW.pop(cert.pk, None)
        Job.objects.filter(pk__in=job_ids).delete()


@th.django_unit_test("a job already queued during Connect retries busy ownership and recovers after exhaustion")
def test_connect_queued_job_busy_recovery(opts):
    from mojo.apps.jobs.models import Job
    from mojo.apps.dnsman.services import certs, delegation_setup as setup
    env = fixture()
    cert = cert_for(env, due=True)
    syncs, errors = [], []
    issuer, ca = _issuer_fixture(env, syncs)
    _RENEW[cert.pk] = issuer
    path = f"{__name__}._renew_handler"
    root_id = certs.publish_job(path, cert, channel=BUSY_CHANNEL)
    assert Job.objects.get(pk=root_id).max_retries == 0, "fixture must begin with the deployment's existing zero-retry job policy"
    def cleanup(domain, certificates, old):
        # Every execution is a real engine dispatch while Connect owns its
        # temporary advisory guard. Force the bounded budget to exhaustion.
        for unused in range(certs.ISSUE_BUSY_RETRIES):
            thread = threading.Thread(target=_execute_job_thread, args=(root_id, errors))
            thread.start()
            thread.join(5)
            assert not thread.is_alive(), "busy worker must return without waiting on Connect's Domain"
    ids = [root_id]
    try:
        prepared = setup.connect(env.target, env.hub.target, client=env.hub, cleanup=cleanup, custody=lambda current: True)
        root = Job.objects.get(pk=root_id)
        assert not errors and root.status == "failed" and root.last_error == certs.ISSUE_BUSY_ERROR and root.max_retries == certs.ISSUE_BUSY_RETRIES, "busy must durably receive only its bounded retry budget, never report successful skipped issuance"
        cert.refresh_from_db()
        assert cert.attempts == 0 and cert.renew_after is not None and not ca.orders, "busy collisions must not consume CA attempts, alter material or reset certificate backoff"
        from mojo.apps.dnsman.models import AcmeDelegation
        row = AcmeDelegation.objects.get(pk=prepared["connection"]["id"])
        env.hub.grant(row)
        setup.connect(env.target, env.hub.target, client=env.hub, custody=lambda current: True)
        setup.check(row, client=env.hub, custody=lambda current: True, probe=lambda *args: objict(ok=True))
        recovered = certs.renew_due(publish=lambda func, current, **kwargs: certs.publish_job(path, current, channel=BUSY_CHANNEL) if current.pk == cert.pk else "unrelated-not-queued")
        root.refresh_from_db()
        leaf_id = root.metadata["retried_as"]
        ids.append(leaf_id)
        leaf = Job.objects.get(pk=leaf_id)
        assert leaf_id != root_id and leaf.status == "pending" and leaf.payload == {"certificate": cert.pk}, "existing cron and Jobs retry replacement must recover an exhausted busy idempotency key"
        thread = threading.Thread(target=_execute_job_thread, args=(leaf_id, errors))
        thread.start()
        thread.join(6)
        leaf.refresh_from_db()
        cert.refresh_from_db()
        assert not errors and leaf.status == "completed" and ca.orders and syncs == [cert.pk] and cert.status == "active", "recovered leaf must actually renew the same certificate after temporary ownership ends"
    finally:
        _RENEW.pop(cert.pk, None)
        Job.objects.filter(pk__in=ids).delete()


@th.django_unit_test("concurrent terminal-busy recovery creates one leaf and respects terminal operator decisions")
def test_busy_recovery_serializes_and_preserves_holds(opts):
    from concurrent.futures import ThreadPoolExecutor
    from django.db import close_old_connections
    from mojo.apps.jobs.models import Job
    from mojo.apps.dnsman.services import certs
    env = fixture()
    cert = cert_for(env, due=True)
    path = f"{__name__}._renew_handler"
    root_id = certs.publish_job(path, cert, channel=BUSY_CHANNEL)
    Job.objects.filter(pk=root_id).update(status="failed", last_error=certs.ISSUE_BUSY_ERROR, max_retries=certs.ISSUE_BUSY_RETRIES)
    def recover():
        close_old_connections()
        try:
            return certs.publish_job(path, cert, channel=BUSY_CHANNEL)
        finally:
            close_old_connections()
    ids = [root_id]
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda unused: recover(), range(2)))
        assert results[0] == results[1] and results[0] != root_id, "concurrent retry requests must serialize one actual replacement leaf"
        ids.append(results[0])
        leaf = Job.objects.get(pk=results[0])
        assert leaf.metadata.get("retried_from") == root_id, "Jobs must preserve its existing retry linkage"
        for state, error in (("canceled", certs.ISSUE_BUSY_ERROR), ("completed", certs.ISSUE_BUSY_ERROR), ("failed", "ordinary_ca_failure")):
            Job.objects.filter(pk=leaf.pk).update(status=state, last_error=error)
            before = Job.objects.filter(payload__certificate=cert.pk).count()
            assert certs.publish_job(path, cert, channel=BUSY_CHANNEL) == leaf.pk, f"{state} leaf must remain the authoritative stop condition"
            assert Job.objects.filter(payload__certificate=cert.pk).count() == before, "recovery must not bypass cancellation, successful completion or CA backoff"
        from mojo.helpers import dates
        Job.objects.filter(pk=leaf.pk).update(status="expired", last_error=certs.ISSUE_BUSY_ERROR,
                                              expires_at=dates.utcnow() - timedelta(seconds=1))
        fresh_id = certs.publish_job(path, cert, channel=BUSY_CHANNEL)
        ids.append(fresh_id)
        fresh = Job.objects.get(pk=fresh_id)
        assert fresh_id != leaf.pk and fresh.status == "pending" and fresh.expires_at > dates.utcnow(), "an expired busy retry must get the existing Jobs replacement's fresh lifetime"
    finally:
        Job.objects.filter(pk__in=ids).delete()


@th.django_unit_test("existing renewal cron recovers only initial pending certificates with terminal busy jobs")
def test_pending_busy_cron_recovery(opts):
    from mojo.apps.jobs.models import Job
    from mojo.apps.dnsman.services import certs
    env = fixture()
    cert = cert_for(env)
    cert.status = "pending"
    cert.save(update_fields=["status"])
    root_id = certs.publish_job(certs.ISSUE_JOB, cert, channel=BUSY_CHANNEL)
    Job.objects.filter(pk=root_id).update(status="failed", last_error=certs.ISSUE_BUSY_ERROR, max_retries=certs.ISSUE_BUSY_RETRIES)
    ids = [root_id]
    try:
        result = certs.renew_due(publish=lambda func, current, **kwargs: certs.publish_job(func, current, channel=BUSY_CHANNEL) if current.pk == cert.pk else "unrelated-not-queued")
        root = Job.objects.get(pk=root_id)
        leaf_id = root.metadata["retried_as"]
        ids.append(leaf_id)
        assert cert.pk in result.certificates and Job.objects.get(pk=leaf_id).status == "pending", "pending initial issuance must not remain stranded after bounded busy exhaustion"
        Job.objects.filter(pk=leaf_id).update(status="failed", last_error="ordinary_ca_failure")
        before = Job.objects.filter(payload__certificate=cert.pk).count()
        certs.renew_due(publish=lambda func, current, **kwargs: certs.publish_job(func, current, channel=BUSY_CHANNEL) if current.pk == cert.pk else "unrelated-not-queued")
        assert Job.objects.filter(payload__certificate=cert.pk).count() == before, "initial CA failure must not become a generic cron retry through a busy ancestor"
    finally:
        Job.objects.filter(pk__in=ids).delete()


@th.django_unit_test("a prior busy retry budget cannot turn CA failure into immediate job retries")
def test_busy_budget_preserves_ca_backoff(opts):
    from mojo.apps.jobs.models import Job, JobEvent
    from mojo.apps.dnsman.services import certs
    from mojo.helpers import dates
    env = fixture()
    verified(env)
    cert = cert_for(env, due=True)
    calls, errors = [], []
    def reject_ca():
        calls.append("CA")
        raise RuntimeError("stub CA refused issuance")
    _RENEW[cert.pk] = partial(certs.issue, hub_client=env.hub, acme=reject_ca,
                              resolve_cname=lambda *args: objict(ok=True))
    root_id = certs.publish_job(f"{__name__}._renew_handler", cert, channel=BUSY_CHANNEL)
    Job.objects.filter(pk=root_id).update(max_retries=certs.ISSUE_BUSY_RETRIES)
    try:
        thread = threading.Thread(target=_execute_job_thread, args=(root_id, errors))
        thread.start()
        thread.join(6)
        job = Job.objects.get(pk=root_id)
        cert.refresh_from_db()
        assert not thread.is_alive() and not errors and job.status == "failed" and job.attempt == 1, "ordinary CA failure must be terminal at the Jobs boundary even with a prior busy budget"
        assert calls == ["CA"] and not JobEvent.objects.filter(job=job, event="retry").exists(), "a CA rejection must not be retried immediately by Jobs"
        assert cert.attempts == 1 and cert.renew_after > dates.utcnow() and cert.status == "active" and cert.mojo_secrets == "preserved-encrypted-key", "certificate backoff and valid serving custody must remain the owners after CA failure"
    finally:
        _RENEW.pop(cert.pk, None)
        Job.objects.filter(pk=root_id).delete()


@th.django_unit_test("busy preparation requires an explicit retry that still refuses stale identity")
def test_profile_publication_preparation_race(opts):
    from django.db import close_old_connections
    from mojo.apps.dnsman.models import AcmeDelegation
    from mojo.apps.dnsman.services import connection_config, delegation_setup as setup
    env = fixture()
    attempted, finished = threading.Event(), threading.Event()
    current = {"consumer": env.hub.consumer}
    outcomes = []

    def current_identity(consumer):
        if consumer != current["consumer"]:
            raise connection_config.ConfigurationError("hub_identity_migration_required")

    def prepare_paused():
        close_old_connections()
        attempted.set()
        try:
            setup._prepare(env.target, env.hub.target, env.hub.consumer, None, False, identity_check=current_identity)
            outcomes.append("created")
        except setup.SetupError as error:
            outcomes.append(error.error_code)
        finally:
            close_old_connections()
            finished.set()
    try:
        with connection_config.identity_lock():
            thread = threading.Thread(target=prepare_paused)
            thread.start()
            assert attempted.wait(3), "Connection preparation must reach the held publication lock"
            assert finished.wait(3), "Preparation must promptly refuse while publication holds identity admission"
            assert outcomes == ["configuration_busy"], "Contention must remain distinct from a changed authenticated identity"
            assert not AcmeDelegation.objects.filter(group=env.group).exists(), "Busy admission must persist no old-identity intent"
            current["consumer"] = uuid.uuid4().hex
        thread.join(5)
        caught("hub_identity_changed", lambda: setup._prepare(
            env.target, env.hub.target, env.hub.consumer, None, False, identity_check=current_identity))
        assert not AcmeDelegation.objects.filter(group=env.group).exists(), "The profile transition race must leave no stale shared connection row"
    finally:
        if env.domain:
            env.domain.delete()
        env.group.delete()
