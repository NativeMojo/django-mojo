"""Ordinary Admin enablement, published-owner compatibility and DB admission."""
import uuid
import time
import importlib
import threading
import inspect
from unittest import mock
from contextlib import contextmanager
from types import SimpleNamespace

from testit import helpers as th

@th.django_unit_test()
def test_real_postgres_admission_blocks_both_directions_and_lost_session(opts):
    from django.db import connections
    from mojo.apps.dnsman.services import acme_hub_upgrade as upgrade
    state = SimpleNamespace(mode="compatibility")
    shared_entered, release_shared = threading.Event(), threading.Event()
    exclusive_entered, release_exclusive = threading.Event(), threading.Event()
    later_entered = threading.Event()
    pids, errors = {}, []
    def run(label, exclusive, entered, release):
        try:
            db = connections["default"]
            with db.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                pids[label] = cursor.fetchone()[0]
            with upgrade.admission(exclusive=exclusive, state_reader=lambda: state):
                entered.set()
                assert release.wait(5), f"{label} admission was not released by fixture"
        except BaseException as error:
            errors.append(error)
        finally:
            connections["default"].close()
    def waiting(label):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with connections["default"].cursor() as cursor:
                cursor.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s AND locktype='advisory' AND NOT granted)", [pids.get(label, -1)])
                if cursor.fetchone()[0]:
                    return
            time.sleep(0.01)
        assert False, f"{label} must wait on a real PostgreSQL admission lock"
    threads = []
    try:
        first = threading.Thread(target=run, args=("shared", False, shared_entered, release_shared))
        threads.append(first)
        first.start()
        assert shared_entered.wait(5), "existing writer must hold shared admission"
        exclusive = threading.Thread(target=run, args=("exclusive", True, exclusive_entered, release_exclusive))
        threads.append(exclusive)
        exclusive.start()
        waiting("exclusive")
        assert not exclusive_entered.is_set(), "activation must not overtake admitted writer"
        release_shared.set()
        assert exclusive_entered.wait(5), "exclusive activation must enter after existing writer finishes"
        later = threading.Thread(target=run, args=("later", False, later_entered, release_shared))
        threads.append(later)
        later.start()
        waiting("later")
        assert not later_entered.is_set(), "new writer must not enter while activation is exclusive"
    finally:
        release_shared.set()
        release_exclusive.set()
        for thread in threads:
            thread.join(timeout=6)
    assert not errors and later_entered.is_set(), f"admission locks must complete cleanly: {errors}"
    with upgrade.admission(state_reader=lambda: state):
        upgrade.check_session()
        connections["default"].close()
        refuses(upgrade.check_session, "admission_unavailable")


@th.django_unit_test()
def test_legacy_orm_and_compatibility_writer_share_allocation_lock(opts):
    from django.db import connections
    from django.db.migrations.executor import MigrationExecutor
    from mojo.apps.dnsman.models import AcmeHubDelegation, AcmeHubChallengeLease, AcmeHubParticipant
    from mojo.apps.dnsman.services import acme_hub as hub, acme_hub_legacy as legacy
    from mojo.helpers import dates
    old_apps = MigrationExecutor(connections["default"]).loader.project_state(
        [("dnsman", "0004_dnsrecordreservation")]).apps
    OldLease = old_apps.get_model("dnsman", "AcmeHubChallengeLease")
    ref = uuid.uuid4().hex
    allocation = AcmeHubDelegation.objects.create(owner_project_uuid=ref, client_ref=ref,
        domain_name="mixed.example", source_name="_acme-challenge.mixed.example",
        target_name=ref + ".hub.example", allocation_zone_name="hub.example", allocation_zone_id="TESTIT")
    initial_participants = AcmeHubParticipant.objects.count()
    try:
        for old_first in (True, False):
            AcmeHubChallengeLease.objects.filter(allocation=allocation).delete()
            entered, release = threading.Event(), threading.Event()
            pids, errors = {}, []
            def writer(old, first):
                try:
                    db = connections["default"]
                    with db.cursor() as cursor:
                        cursor.execute("SELECT pg_backend_pid()")
                        pids[old] = cursor.fetchone()[0]
                    # Published legacy uses the same1422/allocation advisory
                    # key. Its historical ORM deliberately has no participant.
                    with hub._allocation_lock(allocation.pk):
                        if old:
                            row = OldLease.objects.filter(allocation_id=allocation.pk, challenge_ref=ref).first()
                            if row is None:
                                OldLease.objects.create(allocation_id=allocation.pk, challenge_ref=ref,
                                    record_values=["preserved"], state="pending", expires_at=dates.add(seconds=60))
                        else:
                            legacy.persist(allocation, ref, ["preserved"])
                        if first:
                            entered.set()
                            assert release.wait(5), "first mixed writer must be released by fixture"
                except BaseException as error:
                    errors.append(error)
                finally:
                    connections["default"].close()
            first = threading.Thread(target=writer, args=(old_first, True))
            second = threading.Thread(target=writer, args=(not old_first, False))
            first.start()
            try:
                assert entered.wait(5), f"first mixed writer must acquire allocation lock: {errors}"
                second.start()
                deadline = time.monotonic() + 5
                blocked = False
                while time.monotonic() < deadline:
                    with connections["default"].cursor() as cursor:
                        cursor.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s AND locktype='advisory' AND NOT granted)", [pids.get(not old_first, -1)])
                        blocked = cursor.fetchone()[0]
                    if blocked:
                        break
                    time.sleep(0.01)
                assert blocked, "second old/new writer must block on the actual allocation advisory lock"
            finally:
                release.set()
                first.join(timeout=6)
                if second.ident is not None:
                    second.join(timeout=6)
            rows = list(AcmeHubChallengeLease.objects.filter(allocation=allocation))
            assert not errors and len(rows) == 1 and rows[0].participant_id is None, f"mixed writer race must retain one unbound legacy lease: {errors}"
            assert AcmeHubParticipant.objects.count() == initial_participants, "compatibility race must not lazily create owners/trust"
    finally:
        AcmeHubChallengeLease.objects.filter(allocation=allocation).delete()
        AcmeHubDelegation.objects.filter(pk=allocation.pk).delete()


@th.django_unit_test()
def test_default_compatibility_complete_legacy_flow_has_exact_wire_and_no_participants(opts):
    from mojo.apps.account.models import Group, ApiKey
    from mojo.apps.dnsman.models import AcmeHubDelegation, AcmeHubChallengeLease, AcmeHubParticipant
    from mojo.apps.dnsman.services import acme_hub as hub
    rest = importlib.import_module("mojo.apps.dnsman.rest.acme_hub")
    from mojo.helpers import dates
    fixtures = importlib.import_module("tests.test_dnsman.16_acme_hub")
    provider, probe = fixtures.FakeRoute53(), fixtures.FakeProbe()
    group = Group.objects.create(name="acme_hub_test_compat_" + uuid.uuid4().hex)
    key, unused_token = ApiKey.create_for_group(group, "acme_hub_test_compat", permissions={"dnsman_acme_federation": True})
    request = SimpleNamespace(api_key=key, user=None, group=group, DATA={})
    four = {"client_ref", "domain", "source", "target"}
    six = four | {"challenge_ref", "active_value_count"}
    try:
        # Only the external provider/probe and read-only zone boundary are
        # faked. The actual singleton reader, admission, mode dispatch, legacy
        # services and REST serializers run with NO state_reader override.
        with upgrade_schema(), installation(), mock.patch.object(hub, "route53", provider), mock.patch.object(hub, "probe", probe), mock.patch.object(hub, "_new_zone", lambda **kwargs: SimpleNamespace(name="hub.example.net", id="ZONE-HUB")):
            request.DATA = dict(client_ref="compat-owner", domain="compat.example")
            allocated = inspect.unwrap(rest.on_acme_delegation)(request)
            replay = inspect.unwrap(rest.on_acme_delegation)(request)
            assert set(allocated) == four and replay == allocated, "default owner allocation must retain exact four-field idempotent wire shape"
            allocation = AcmeHubDelegation.objects.get(client_ref="compat-owner")
            request.DATA = dict(client_ref="compat-owner", challenge_ref="compat-first", values=["compat-value"])
            published = inspect.unwrap(rest.on_acme_publish)(request)
            again = inspect.unwrap(rest.on_acme_publish)(request)
            assert set(published) == six and again == published and published["active_value_count"] == 1, "default publish/replay must retain exact six fields and additive idempotence"
            lease = AcmeHubChallengeLease.objects.get(allocation=allocation, challenge_ref="compat-first")
            assert lease.participant_id is None and lease.record_values == ["compat-value"], "default publication must persist legacy unbound intent"
            request.DATA = dict(client_ref="compat-owner", challenge_ref="compat-first")
            withdrawn = inspect.unwrap(rest.on_acme_withdraw)(request)
            repeated = inspect.unwrap(rest.on_acme_withdraw)(request)
            assert set(withdrawn) == six and repeated == withdrawn and withdrawn["active_value_count"] == 0, "default withdrawal must retain exact six-field idempotence"
            assert hub.reconcile(allocation) == [], "normal compatibility reconcile must retain empty desired union after withdraw"
            request.DATA = dict(client_ref="compat-owner", challenge_ref="compat-expire", values=["elapsed-value"])
            inspect.unwrap(rest.on_acme_publish)(request)
            AcmeHubChallengeLease.objects.filter(allocation=allocation, challenge_ref="compat-expire").update(expires_at=dates.add(seconds=-1))
            swept = hub.sweep()
            assert swept.expired == 1 and swept.errors == 0, "normal compatibility sweep must expire and reconcile elapsed old-shape intent"
            assert not AcmeHubParticipant.objects.exists(), "complete default legacy flow must create ZERO owner/trust participants"
            request.DATA = {}
            capabilities = inspect.unwrap(rest.on_acme_capabilities)(request)
            assert set(capabilities) == {"version", "shared_validation", "consumer_project_uuid", "can_consume", "can_manage_trust", "trust_scope"}, "disabled capability wire must retain its exact six-key shape"
            assert capabilities["shared_validation"] is False and capabilities["can_consume"] is False and capabilities["can_manage_trust"] is False, "default mode must leave every shared mutation capability off"
            refuses(lambda: hub.lookup_participants(request, "compat.example"), "acme_hub_unavailable")
            assert not AcmeHubParticipant.objects.exists(), "disabled shared read cannot lazily bind an owner"
    finally:
        ApiKey.objects.filter(pk=key.pk).delete()
        Group.objects.filter(pk=group.pk).delete()


@contextmanager
def upgrade_schema():
    from django.db import connection
    from mojo.apps.dnsman.models import AcmeHubInstallation, AcmeHubDelegation, AcmeHubParticipant, AcmeHubChallengeLease
    from mojo.apps.edge.models import PlatformDeployment
    schema = "testit_acme_activation_" + uuid.uuid4().hex
    with connection.cursor() as cursor:
        cursor.execute('CREATE SCHEMA "' + schema + '"')
        cursor.execute('SET search_path TO "' + schema + '", public')
    try:
        with connection.schema_editor() as editor:
            for model in (AcmeHubInstallation, AcmeHubDelegation, AcmeHubParticipant, AcmeHubChallengeLease, PlatformDeployment):
                editor.create_model(model)
        yield schema
    finally:
        with connection.cursor() as cursor:
            cursor.execute('SET search_path TO public')
            cursor.execute('DROP SCHEMA "' + schema + '" CASCADE')


@contextmanager
def installation(mode="compatibility"):
    from mojo.apps.dnsman.models import AcmeHubInstallation
    from mojo.helpers import dates
    saved = list(AcmeHubInstallation.objects.values())
    AcmeHubInstallation.objects.all().delete()
    AcmeHubInstallation.objects.bulk_create([AcmeHubInstallation(id=1)])
    if mode == "enabled":
        AcmeHubInstallation.objects.filter(pk=1).update(
            mode="enabled", activation_uuid=uuid.uuid4(), deployment_uuid=uuid.uuid4(),
            app_sha="d" * 40, minimum_release="1.40.0", activated_at=dates.utcnow())
    try:
        yield AcmeHubInstallation.objects.get(pk=1)
    finally:
        AcmeHubInstallation.objects.all().delete()
        AcmeHubInstallation.objects.bulk_create([AcmeHubInstallation(**row) for row in saved])


def refuses(function, text):
    try:
        function()
    except Exception as error:
        assert getattr(error,"error_code",None) == text or text in str(error), f"expected {text}, got {type(error).__name__}: {error}"
    else:
        assert False, f"operation must refuse {text}"



@contextmanager
def admin_candidate(status="converged", version="1.40.0"):
    import mojo
    from mojo.apps.account.models import User
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.edge.services import deploy
    from mojo.apps.dnsman.services import acme_hub_upgrade as upgrade
    from mojo.helpers import dates
    actor = User.objects.create(username="testit-hub-enable-"+uuid.uuid4().hex,
                                is_active=True, is_superuser=True)
    row = PlatformDeployment.objects.create(sha="a"*40, framework_version=version,
        status=status, request_key=uuid.uuid4().hex, frozen_roster=["node-a", "node-b"])
    row.node_evidence = [dict(runner=node, state="proven",
        stale_after=dates.add(seconds=600).isoformat(), proof=dict(platform_sha=row.sha,
        platform_deployment=str(row.pk), django_mojo_version=version)) for node in row.frozen_roster]
    row.save(update_fields=["node_evidence"])
    try:
        with mock.patch.object(mojo, "__version__", version), mock.patch.object(deploy, "get_target", return_value=None), mock.patch.object(upgrade, "_configured", return_value=(True,None)):
            yield actor,row
    finally:
        row.delete()
        actor.delete()


@th.django_unit_test()
def test_forward_schema_keeps_old_installation_readers(opts):
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    old_apps=MigrationExecutor(connection).loader.project_state([("dnsman","0007_acmehubinstallation")]).apps
    OldInstallation=old_apps.get_model("dnsman","AcmeHubInstallation")
    with upgrade_schema(),installation() as current:
        previous=OldInstallation.objects.get(pk=1)
        assert previous.installation_uuid==current.installation_uuid and previous.feature_epoch==1, "old1.39 ORM must still select every persisted installation column during rolling upgrade"


@th.django_unit_test()
def test_missing_installation_fails_closed_and_read_does_not_recreate(opts):
    from mojo.apps.dnsman.models import AcmeHubInstallation
    from mojo.apps.dnsman.services import acme_hub_upgrade as upgrade
    with upgrade_schema():
        refuses(upgrade.read_state,"state_unavailable")
        assert not AcmeHubInstallation.objects.exists(), "a missing installation must stay missing after a read"


@th.django_unit_test()
def test_admin_enable_checks_complete_current_deployment_and_version(opts):
    from mojo.apps.dnsman.services import acme_hub_upgrade as upgrade
    with upgrade_schema(), installation(), admin_candidate() as (actor,row):
        ready = upgrade.status(actor)
        assert ready == dict(version=1,enabled=False,configured=True,deployment_uuid=str(row.pk),can_enable=True,reason=None), "converged matching ordinary deployment must be ready without new auth/platform"
        for field,value,reason in (("status","partial","deployment_not_converged"),
                ("node_evidence",row.node_evidence[:1],"deployment_proof_unavailable"),
                ("framework_version","1.39.0","framework_upgrade_required")):
            before=getattr(row,field)
            setattr(row,field,value);row.save(update_fields=[field])
            assert upgrade.status(actor)["reason"] == reason, f"{field} must refuse {reason}"
            refuses(lambda:upgrade.enable(actor,str(row.pk)),reason)
            setattr(row,field,before);row.save(update_fields=[field])
        for key,value in (("platform_deployment",str(uuid.uuid4())),("platform_sha","b"*40),("django_mojo_version","1.39.0")):
            saved=row.node_evidence[0]["proof"][key]
            row.node_evidence[0]["proof"][key]=value;row.save(update_fields=["node_evidence"])
            assert upgrade.status(actor)["reason"] == "deployment_proof_unavailable", f"node {key} must match deployment candidate"
            row.node_evidence[0]["proof"][key]=saved;row.save(update_fields=["node_evidence"])
        saved=row.node_evidence[0]["stale_after"]
        row.node_evidence[0]["stale_after"]="2000-01-01T00:00:00Z";row.save(update_fields=["node_evidence"])
        assert upgrade.status(actor)["reason"] == "deployment_proof_unavailable", "expired node proofs require ordinary deployment verification"
        row.node_evidence[0]["stale_after"]=saved;row.save(update_fields=["node_evidence"])
        refuses(lambda:upgrade.enable(actor,str(uuid.uuid4())),"deployment_changed")
        result=upgrade.enable(actor,str(row.pk))
        assert result["enabled"] and not result["can_enable"] and result["reason"] is None, "ordinary Admin action must enable once"
        assert upgrade.enable(actor,str(row.pk)) == result, "same selected deployment retry must be idempotent"


@th.django_unit_test()
def test_enable_reloads_proof_after_existing_deployment_row_lock(opts):
    from django.db import connections,transaction
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.dnsman.services import acme_hub_upgrade as upgrade
    with upgrade_schema() as schema,installation(),admin_candidate() as (actor,row):
        held,release,finished=threading.Event(),threading.Event(),threading.Event()
        outcome,pids=[],{}
        def connection():
            db=connections["default"]
            with db.cursor() as cursor:
                cursor.execute('SET search_path TO "'+schema+'", public')
            return db
        def refresh_proof():
            connection()
            try:
                with transaction.atomic():
                    current=PlatformDeployment.objects.select_for_update().get(pk=row.pk)
                    held.set()
                    assert release.wait(5), "test must release ordinary deployment proof writer"
                    current.status="partial";current.save(update_fields=["status"])
            finally:
                connections["default"].close()
        def enable():
            db=connection()
            with db.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                pids["enable"]=cursor.fetchone()[0]
            try:
                upgrade.enable(actor,str(row.pk));outcome.append("enabled")
            except Exception as error:
                outcome.append(str(error))
            finally:
                connections["default"].close();finished.set()
        writer=threading.Thread(target=refresh_proof);worker=threading.Thread(target=enable)
        writer.start()
        try:
            assert held.wait(5), "ordinary proof writer must hold real deployment row lock"
            worker.start()
            waiting=False;deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE pid=%s AND NOT granted)", [pids.get("enable",-1)])
                    waiting=cursor.fetchone()[0]
                if waiting:break
                time.sleep(0.01)
            assert waiting and not finished.is_set(), "enable must wait for existing proof row writer before validating"
        finally:
            release.set();writer.join(6)
            if worker.ident is not None:worker.join(6)
        assert outcome==["deployment_not_converged"], f"proof change must be re-read under lock, never ignored: {outcome}"
        assert upgrade.read_state().mode=="compatibility", "racing incomplete proof must not enable sharing"


@th.django_unit_test()
def test_admin_enable_refuses_active_old_version_and_missing_hub(opts):
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.dnsman.services import acme_hub_upgrade as upgrade
    with upgrade_schema(), installation(), admin_candidate() as (actor,row):
        active=PlatformDeployment.objects.create(sha="b"*40,status="requested",request_key=uuid.uuid4().hex)
        assert upgrade.status(actor)["reason"] == "deployment_in_progress", "active ordinary deployment must block enablement"
        active.delete()
        with mock.patch.object(upgrade,"_configured",return_value=(False,"hub_unconfigured")):
            assert upgrade.status(actor)["can_enable"] is False, "unconfigured hub must not enable"
            refuses(lambda:upgrade.enable(actor,str(row.pk)),"hub_unconfigured")
    for legacy in ("1.39.0", "1.39.1"):
        with upgrade_schema(), installation(), admin_candidate(version=legacy) as (actor,row):
            assert upgrade.status(actor)["reason"] == "framework_upgrade_required", f"published pre-cleanup {legacy} cannot qualify same-number new bytes"


@th.django_unit_test()
def test_atomic_enable_preserves_owner_history_and_rolls_back_conflict(opts):
    from mojo.apps.account.models import Group
    from mojo.apps.dnsman.models import AcmeHubDelegation,AcmeHubParticipant,AcmeHubChallengeLease
    from mojo.apps.dnsman.services import acme_hub_upgrade as upgrade
    from mojo.helpers import dates
    group=Group.objects.create(name="testit-hub-enable-"+uuid.uuid4().hex)
    try:
        with upgrade_schema(), installation(), admin_candidate() as (actor,row):
            allocation=AcmeHubDelegation.objects.create(group=group,owner_project_uuid=group.get_uuid(),client_ref="owner",
                domain_name="owner.example",source_name="_acme-challenge.owner.example",target_name="owner.hub.example",allocation_zone_name="hub.example",allocation_zone_id="TESTIT")
            lease=AcmeHubChallengeLease.objects.create(allocation=allocation,challenge_ref="preserved",record_values=["preserved"],expires_at=dates.add(seconds=600))
            original=(lease.created,lease.modified,lease.record_values,lease.expires_at)
            conflict=AcmeHubParticipant.objects.create(allocation=allocation,group=group,consumer_project_uuid=group.get_uuid(),client_ref="wrong-owner",role="owner")
            refuses(lambda:upgrade.enable(actor,str(row.pk)),"owner_conflict")
            assert upgrade.read_state().mode == "compatibility" and AcmeHubParticipant.objects.count()==1, "late conflict must roll back all owner binding and enablement"
            AcmeHubParticipant.objects.filter(pk=conflict.pk).delete()
            installation_id=upgrade.read_state().installation_uuid
            upgrade.enable(actor,str(row.pk));lease.refresh_from_db()
            assert lease.participant_id and (lease.created,lease.modified,lease.record_values,lease.expires_at)==original, "enable must bind historical owner without rewriting lease facts"
            assert upgrade.read_state().installation_uuid==installation_id, "enable must retain installation identity"
    finally:
        group.delete()


@th.django_unit_test()
def test_published_138_owner_namespace_continues_before_enable(opts):
    from mojo.apps.account.models import Group
    from mojo.apps.dnsman.models import AcmeHubDelegation,AcmeHubParticipant,AcmeHubChallengeLease
    from mojo.apps.dnsman.services import acme_hub as hub
    from mojo.helpers import dates
    fixtures=importlib.import_module("tests.test_dnsman.16_acme_hub")
    group=Group.objects.create(name="testit-hub-published-"+uuid.uuid4().hex)
    try:
        with upgrade_schema(), installation():
            allocation=AcmeHubDelegation.objects.create(group=group,owner_project_uuid=group.get_uuid(),client_ref="published-owner",domain_name="published.example",source_name="_acme-challenge.published.example",target_name="published.hub.example.net",allocation_zone_name="hub.example.net",allocation_zone_id="ZONE-HUB")
            owner=AcmeHubParticipant.objects.create(allocation=allocation,group=group,consumer_project_uuid=group.get_uuid(),client_ref="published-owner",role="owner")
            lease=AcmeHubChallengeLease.objects.create(allocation=allocation,participant=owner,challenge_ref="published",record_values=["old"],expires_at=dates.add(seconds=600))
            provider,probe=fixtures.FakeRoute53(),fixtures.FakeProbe()
            with mock.patch.object(hub,"route53",provider),mock.patch.object(hub,"probe",probe):
                assert hub.allocate(group,"published-owner","published.example").pk==allocation.pk, "published1.38 participant must not block normal owner allocation"
                published=hub.publish(group,"published-owner","new",["new"])
                assert published.active_value_count==2, "owner publication must preserve prior published participant lease"
                assert hub.withdraw(group,"published-owner","new").active_value_count==1, "owner withdrawal must stay in existing participant namespace"
                lease.refresh_from_db()
                assert lease.participant_id==owner.pk and lease.record_values==["old"], "preexisting published owner lease custody remains unchanged"
    finally:
        group.delete()


@th.django_unit_test()
def test_published_138_trusted_connection_renews_without_new_grants(opts):
    from mojo.apps.account.models import Group,ApiKey
    from mojo.apps.dnsman.models import AcmeHubDelegation,AcmeHubParticipant
    from mojo.apps.dnsman.services import acme_hub as hub,delegation_setup as setup
    from mojo.helpers import dates
    from objict import objict
    fixtures=importlib.import_module("tests.test_dnsman.16_acme_hub")
    owner=Group.objects.create(name="testit-published-owner-"+uuid.uuid4().hex)
    consumer=Group.objects.create(name="testit-published-trusted-"+uuid.uuid4().hex)
    key,unused=ApiKey.create_for_group(consumer,"testit-published-trusted",permissions={"dnsman_acme_federation":True})
    request=SimpleNamespace(api_key=key,user=key,group=consumer)
    try:
        with upgrade_schema(),installation():
            allocation=AcmeHubDelegation.objects.create(group=owner,owner_project_uuid=owner.get_uuid(),client_ref="owner",domain_name="trusted.example",source_name="_acme-challenge.trusted.example",target_name="trusted.hub.example.net",allocation_zone_name="hub.example.net",allocation_zone_id="ZONE-HUB")
            AcmeHubParticipant.objects.create(allocation=allocation,group=owner,consumer_project_uuid=owner.get_uuid(),client_ref="owner",role="owner")
            trusted=AcmeHubParticipant.objects.create(allocation=allocation,group=consumer,consumer_project_uuid=consumer.get_uuid(),client_ref="existing-trusted",role="trusted")
            row=SimpleNamespace(client_ref="existing-trusted",domain_name=allocation.domain_name,
                hub_allocation_ref=allocation.target_name,hub_consumer_project_uuid=consumer.get_uuid(),
                hub_participant_ref=str(trusted.participant_ref),target_name=allocation.target_name,source_name=allocation.source_name)
            provider,probe=fixtures.FakeRoute53(),fixtures.AliasProbe(allocation)
            client=SimpleNamespace(capabilities=lambda:objict(version=2,shared_validation=False,can_consume=False,consumer_project_uuid=consumer.get_uuid()),status=lambda ref:objict(hub.participant_status(request,ref,dns_probe=probe)))
            with mock.patch.object(hub,"route53",provider),mock.patch.object(hub,"probe",probe):
                assert setup._shared_status(row,client)["can_publish"] is True, "existing pinned trusted status must remain renewable during compatible rollout"
                assert hub.publish(consumer,"existing-trusted","renewal",["new"],request=request).active_value_count==1, "existing trusted participant must publish before new grants enable"
                assert hub.withdraw(consumer,"existing-trusted","renewal",request=request).active_value_count==0, "existing trusted participant must withdraw its own lease"
                refuses(lambda:hub.join(request,allocation.domain_name,"existing-trusted",allocation.target_name),"acme_hub_unavailable")
                trusted.revoked_at=dates.utcnow();trusted.save(update_fields=["revoked_at","modified"])
                refuses(lambda:setup._shared_status(row,client),"trust_revoked")
                refuses(lambda:hub.publish(consumer,"existing-trusted","late",["late"],request=request),"acme_trust_revoked")
                assert hub.withdraw(consumer,"existing-trusted","renewal",request=request).active_value_count==0, "revocation still allows authorized cleanup, never renewed authority"
                row.hub_participant_ref=None
                refuses(lambda:setup._shared_status(row,client),"hub_unsupported")
    finally:
        key.delete();consumer.delete();owner.delete()


@th.django_unit_test()
def test_existing_admin_auth_is_reused_and_keys_never_enable(opts):
    from mojo.apps.account.rest import admin_shared_validation_hub as rest
    from mojo.apps.account.models import User,ApiKey,Group
    from mojo.apps.dnsman.services import acme_hub_upgrade as upgrade
    from mojo import errors as me
    from testit.client import RestClient
    mutate=rest.on_shared_validation_hub_enable
    assert mutate._mojo_requires_fresh_auth and mutate._mojo_fresh_auth_seconds==600, "enable must use existing fresh-auth decorator"
    assert mutate._mojo_denies_key_backed_session, "enable must reject API-key-backed sessions"
    actor=User.objects.create(username="testit-hub-auth-"+uuid.uuid4().hex,is_active=True,permissions={"admin":True})
    try:
        with th.assert_raises(me.PermissionDeniedException):
            upgrade.status(actor)
        User.objects.filter(pk=actor.pk).update(is_superuser=True,is_active=False)
        with th.assert_raises(me.PermissionDeniedException):
            upgrade.status(actor)
    finally:
        actor.delete()
    client=RestClient(opts.client.host)
    endpoint="/api/account/admin/fleet/shared-validation/hub"
    assert client.get(endpoint).status_code in (401,403), "anonymous browser must not read activation status"
    assert client.post(endpoint,{"deployment_uuid":str(uuid.uuid4())}).status_code in (401,403), "anonymous browser must not activate"
    group=Group.objects.create(name="testit-hub-auth-"+uuid.uuid4().hex)
    key,token=ApiKey.create_for_group(group,"testit-hub-auth",permissions={"admin":True,"dnsman_acme_federation":True})
    try:
        for method in ("get","post"):
            response=getattr(client,method)(endpoint,headers={"Authorization":"apikey "+token})
            assert response.status_code==403, f"{method} must deny machine credentials before activation logic"
    finally:
        key.delete();group.delete()


@th.django_unit_test()
def test_hub_admin_http_uses_existing_login_origin_and_private_envelope(opts):
    from tests.test_dnsman._helpers import make_user,login
    endpoint="/api/account/admin/fleet/shared-validation/hub"
    actor,email,password=make_user(is_superuser=True)
    try:
        login(opts,email,password)
        with installation():
            response=opts.client.get(endpoint)
            assert response.status_code==200 and response.response.data.version==1, "existing superuser login must read actual hub route"
            assert response.response.data.enabled is False, "GET must not enable sharing"
            assert "no-store" in {key.lower():value for key,value in opts.client.last_response.headers.items()}.get("cache-control",""), f"hub status must reuse private response headers: {opts.client.last_response.headers}"
            changed=opts.client.post(endpoint,{"deployment_uuid":str(uuid.uuid4())},headers={"Origin":opts.client.host.rstrip("/")})
            assert changed.status_code in (409,503), "fresh existing same-origin login must reach ordinary selected-deployment validation"
            assert opts.client.post(endpoint,{"deployment_uuid":str(uuid.uuid4())},headers={"Origin":"https://attacker.example"}).status_code==403, "existing origin policy must reject cross-origin activation"
            malformed=opts.client.post(endpoint,{"deployment_uuid":str(uuid.uuid4()),"token":"forbidden"},headers={"Origin":opts.client.host.rstrip("/")})
            assert malformed.status_code==400, "enable endpoint accepts only selected deployment UUID"
    finally:
        opts.client.logout();actor.delete()


def _http_identity(opts):
    from testit.client import RestClient
    from mojo.apps.account.models import ApiKey
    from tests.test_dnsman._helpers import make_user, login
    module = importlib.import_module("tests.test_dnsman.16_acme_hub")
    allocation = module._allocation(opts)
    key, token = ApiKey.create_for_group(opts.acme_group, "acme_hub_test_http_owner", permissions={"dnsman_acme_federation": True})
    client = RestClient(opts.client.host)
    headers = {"Authorization": "apikey " + token}
    response = client.post("/api/dnsman/acme/consumer/lookup", {"domain": allocation.domain_name}, headers=headers)
    assert response.status_code == 200 and len(response.response.data["items"]) == 1, f"own protected identity must resolve its binding over HTTP: {response}"
    response = client.post("/api/dnsman/acme/trust/list", {"allocation_ref": allocation.target_name}, headers=headers)
    assert response.status_code == 200, f"owner must manage its exact allocation over HTTP: {response}"
    response = client.post("/api/dnsman/acme/consumer/lookup", {"domain": allocation.domain_name, "hub_url": "https://evil.example"}, headers=headers)
    assert response.status_code == 400 and response.response.error == "acme_invalid_request", f"new HTTP shapes must reject arbitrary fields: {response}"
    user, email, password = make_user(perms=["manage_dns", "security", "manage_sites", "manage_groups"])
    try:
        login(opts, email, password)
        response = opts.client.post("/api/dnsman/acme/trust/list", {"allocation_ref": allocation.target_name})
        assert response.status_code == 403 and response.response.error == "acme_not_authorized", f"ordinary global managers must not gain issuance controller authority: {response}"
        response = opts.client.post("/api/dnsman/acme/consumer/lookup", {"domain": allocation.domain_name})
        assert response.status_code == 403, f"human sessions cannot consume as an implicit project: {response}"
    finally:
        opts.client.logout()
        user.delete()


@th.django_unit_test()
def test_shared_http_identity_serial(opts):
    from mojo.apps.account.models import Group, ApiKey
    from mojo.apps.dnsman.models import AcmeHubDelegation, AcmeHubParticipant, AcmeHubChallengeLease
    group = Group.objects.create(name="acme_hub_test_serial_" + uuid.uuid4().hex)
    try:
        with installation("enabled"):
            _http_identity(SimpleNamespace(acme_group=group, client=opts.client))
    finally:
        allocations = AcmeHubDelegation.objects.filter(group=group)
        AcmeHubChallengeLease.objects.filter(allocation__in=allocations).delete()
        AcmeHubParticipant.objects.filter(allocation__in=allocations).delete()
        allocations.delete()
        ApiKey.objects.filter(group=group).delete()
        Group.objects.filter(pk=group.pk).delete()
