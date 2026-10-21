"""Irreversible activation and historical-schema contracts, isolated serially."""
import uuid
import time
import importlib
import threading
import tempfile
import json
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace

from testit import helpers as th


@th.django_unit_test()
def test_private_build_manifest_is_actual_bytes_and_loaded_identity_is_frozen(opts):
    from mojo.apps.dnsman import acme_hub_feature as feature
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "mojo"
        for name in feature.CRITICAL_FILES:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"reviewed critical file\n")
        source = root / "apps/dnsman/acme_hub_feature.py"
        source.write_text(Path(feature.__file__).read_text())
        def load():
            spec = importlib.util.spec_from_file_location("testit_acme_identity_" + uuid.uuid4().hex, source)
            result = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(result)
            return result
        unbound = load()
        assert unbound.loaded_identity() is None, "unbound source must never qualify as a published artifact"
        identity = {key: value for key, value in artifact().items() if key != "wheel_sha256"}
        identity["manifest_sha256"] = unbound.manifest()
        unbound.BUILD_FILE.write_text(json.dumps(identity))
        loaded = load()
        assert loaded.loaded_identity() == identity, "embedded build identity must match actual critical bytes"
        (root / "apps/edge/asyncjobs.py").write_bytes(b"different candidate bytes\n")
        assert loaded.installed_identity() is None, "file substitution must invalidate on-disk artifact qualification"
        assert loaded.loaded_identity() == identity, "new installed files cannot rewrite an already-loaded process identity"


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
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute('SET search_path TO public')
            cursor.execute('DROP SCHEMA "' + schema + '" CASCADE')


def artifact():
    return dict(epoch=1, source_sha="a" * 40, manifest_sha256="b" * 64,
                wheel_sha256="c" * 64, release="1.38.0")


@th.django_unit_test()
def test_actual_legacy_release_cannot_be_an_activation_floor(opts):
    from mojo.apps.dnsman import acme_hub_feature as feature
    for version in ("1.37.3", "1.37.2", "1.38.0.dev1", "1.38.0rc1", "1.38.0+local", "unknown"):
        try:
            feature.validate_artifact(dict(artifact(), release=version))
        except ValueError:
            pass
        else:
            assert False, f"new local epoch/source cannot make unqualified release {version} a production floor"


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
            app_sha="d" * 40, minimum_release="1.38.0", source_sha="a" * 40,
            wheel_sha256="c" * 64, manifest_sha256="b" * 64,
            roster_sha256="e" * 64, attestation_sha256="f" * 64, activated_at=dates.utcnow())
    try:
        yield AcmeHubInstallation.objects.get(pk=1)
    finally:
        AcmeHubInstallation.objects.all().delete()
        AcmeHubInstallation.objects.bulk_create([AcmeHubInstallation(**row) for row in saved])


def refuses(function, text):
    try:
        function()
    except Exception as error:
        assert text in str(error), f"expected {text}, got {type(error).__name__}: {error}"
    else:
        assert False, f"operation must refuse {text}"


def attestation(operation):
    now = time.time()
    holds = {name: True for name in (
        "old_processes_absent", "request_held", "engine_held", "scheduler_held", "cron_held",
        "supervisor_held", "launch_held", "no_root_launchers", "transaction_idle")}
    return {key: operation[key] for key in (
        "installation_uuid", "activation_uuid", "nonce", "deployment_uuid", "app_sha", "artifact") } | dict(
        roster_digest=operation["roster_digest"], collector_identity="test operator",
        classification="operator_attested", observed_at=now, expires_at=now + 60,
        unmanaged_writer_statement=True, launch_exclusion_statement=True, provider_outcome_statement=True,
        nodes=[dict(node_id=name, instance_id="i-test", boot_id=str(uuid.uuid4()),
                    helper_pid=1234, helper_start=123, helper_session=uuid.uuid4().hex,
                    nonce=operation["nonce"], activation_uuid=operation["activation_uuid"],
                    floor_generation=1, artifact=operation["artifact"], helper_confirmed_at=now,
                    installed_identity=operation["artifact"], updater_identity="a" * 64,
                    lifecycle_identity="b" * 64, processes=[],
                    **holds) for name in operation["roster"]])


@th.django_unit_test()
def test_empty_enabled_state_never_falls_back(opts):
    from mojo.apps.dnsman.models import AcmeHubInstallation
    from mojo.apps.dnsman.services import acme_hub_upgrade as upgrade
    with installation("enabled"):
        assert upgrade.read_state().mode == "enabled", "empty hub still has explicit enabled authority"
        AcmeHubInstallation.objects.all().delete()
        refuses(upgrade.read_state, "state_unavailable")
        assert not AcmeHubInstallation.objects.exists(), "read must never reseed missing authority"


@th.django_unit_test()
def test_generic_settings_are_not_activation_authority(opts):
    from mojo.apps.account.models import Setting
    from mojo.apps.dnsman.services import acme_hub_upgrade as upgrade
    with installation():
        try:
            Setting.set("TESTIT_ACME_HUB_SHARED_ENABLED", True)
            assert upgrade.read_state().mode == "compatibility", "generic settings must not enable sharing"
            refuses(lambda: upgrade.read_state().save(), "dedicated operator writer")
            refuses(lambda: upgrade.read_state().delete(), "cannot be deleted")
        finally:
            Setting.objects.filter(key="TESTIT_ACME_HUB_SHARED_ENABLED").delete()


@th.django_unit_test()
def test_default_operator_and_unknown_evidence_refuse(opts):
    from mojo.apps.dnsman.services import acme_hub_upgrade as upgrade
    with installation():
        operation = upgrade.plan(artifact(), ["node-engine"], str(uuid.uuid4()), "d" * 40)
        refuses(lambda: upgrade.activate(operation, operator_uid=1000), "evidence_unavailable")
        refuses(lambda: upgrade.activate(operation, operator_uid=0), "evidence_unavailable")
        assert upgrade.read_state().mode == "compatibility", "unavailable evidence must not activate or bind"


@th.django_unit_test()
def test_strict_operator_attestation_and_retry_identity(opts):
    from mojo.apps.dnsman.services import acme_hub_upgrade as upgrade
    with installation():
        operation = upgrade.plan(artifact(), ["node-engine"], str(uuid.uuid4()), "d" * 40)
        receipt = attestation(operation)
        assert len(upgrade.validate_attestation(receipt, operation)) == 64, "honestly classified complete operator assertion is digestible"
        for key, bad in (("nonce", "wrong"), ("classification", "machine_verified"),
                         ("provider_outcome_statement", False), ("launch_exclusion_statement", False)):
            refuses(lambda: upgrade.validate_attestation(dict(receipt, **{key: bad}), operation), "unavailable" if key not in ("nonce",) else "conflict")
        floor = dict(installation_uuid=operation["installation_uuid"], artifact=operation["artifact"],
                     roster_digest=operation["roster_digest"], activation_uuid=operation["activation_uuid"])
        retry = upgrade.plan(artifact(), operation["roster"], operation["deployment_uuid"], operation["app_sha"], previous_floor=floor)
        assert retry["activation_uuid"] == operation["activation_uuid"] and retry["nonce"] != operation["nonce"], "partial-stamp retry must retain immutable activation and renew only session nonce"
        refuses(lambda: upgrade.plan(artifact(), ["other"], operation["deployment_uuid"], operation["app_sha"], previous_floor=floor), "activation_conflict")


@th.django_unit_test()
def test_atomic_activation_and_immutable_readback(opts):
    from mojo.apps.dnsman.models import AcmeHubDelegation, AcmeHubChallengeLease, AcmeHubParticipant
    from mojo.apps.edge.models import PlatformDeployment
    from mojo.apps.dnsman.services import acme_hub_upgrade as upgrade
    from mojo.helpers import dates
    with upgrade_schema(), installation():
        record = PlatformDeployment.objects.create(
            sha="d" * 40, framework_version="1.38.0", status="converged", request_key=uuid.uuid4().hex,
            frozen_roster=["node-engine"], actor="acme_upgrade_test")
        ref = uuid.uuid4().hex
        allocation = AcmeHubDelegation.objects.create(owner_project_uuid=ref, client_ref=ref,
            domain_name="upgrade.example", source_name="_acme-challenge.upgrade.example", target_name=ref + ".hub.example",
            allocation_zone_name="hub.example", allocation_zone_id="TESTIT")
        lease = AcmeHubChallengeLease.objects.create(allocation=allocation, challenge_ref=ref,
            record_values=["preserve"], state="expired", expires_at=dates.add(seconds=-100))
        before = (lease.created, lease.modified, lease.record_values, lease.state, lease.expires_at)
        operation = upgrade.plan(artifact(), ["node-engine"], str(record.pk), record.sha)
        try:
            result = upgrade.activate(operation, attestation, lambda digest: digest, operator_uid=0,
                                      identity_reader=lambda: {k: v for k, v in artifact().items() if k != "wheel_sha256"})
            lease.refresh_from_db()
            assert result["mode"] == "enabled" and lease.participant_id, "binding and one-way enable must commit together"
            assert before == (lease.created, lease.modified, lease.record_values, lease.state, lease.expires_at), "activation must preserve historical intent and timestamps"
            owner = lease.participant_id
            result2 = upgrade.activate(operation, operator_uid=0, attestation=attestation)
            assert result2["activation_uuid"] == result["activation_uuid"] and AcmeHubParticipant.objects.get(pk=owner).role == "owner", "same-identity repeat must preserve owner refs"
        finally:
            AcmeHubChallengeLease.objects.filter(allocation=allocation).delete()
            AcmeHubParticipant.objects.filter(allocation=allocation).delete()
            AcmeHubDelegation.objects.filter(pk=allocation.pk).delete()
            record.delete()


@th.django_unit_test()
def test_false_capabilities_stop_setup_before_lookup(opts):
    from mojo.apps.dnsman.services import acme_hub_client as client, delegation_setup as setup
    result = dict(version=2, shared_validation=False, consumer_project_uuid=str(uuid.uuid4()),
                  can_consume=False, can_manage_trust=False, trust_scope="apex_and_wildcard")
    assert client.capabilities(transport=lambda *args: result).shared_validation is False, "false is a supported v2 capability"
    class Disabled:
        def capabilities(self):
            return result
        def lookup(self, *args, **kwargs):
            assert False, "disabled sharing must short-circuit before lookup/prepare"
    try:
        setup._preflight(SimpleNamespace(name="example.com"), Disabled())
    except setup.SetupError as error:
        assert error.error_code == "hub_unconfigured", "false capability must retain existing configure_hub action"
    else:
        assert False, "disabled sharing must refuse before preparing intent"


@th.django_unit_test()
def test_legacy_lease_insert_after_upgrade(opts):
    from mojo.apps.dnsman.models import AcmeHubDelegation, AcmeHubChallengeLease
    from mojo.helpers import dates

    ref = uuid.uuid4().hex
    allocation = AcmeHubDelegation.objects.create(
        owner_project_uuid=ref, client_ref=ref, domain_name="legacy.example",
        source_name="_acme-challenge.legacy.example", target_name=ref + ".hub.example",
        allocation_zone_name="hub.example", allocation_zone_id="TESTIT")
    try:
        lease = AcmeHubChallengeLease.objects.create(
            allocation=allocation, challenge_ref=ref, record_values=["legacy-value"],
            expires_at=dates.add(seconds=60))
        assert lease.participant_id is None, "published legacy writers must insert leases without participant after upgrade"
    finally:
        AcmeHubChallengeLease.objects.filter(allocation=allocation).delete()
        AcmeHubDelegation.objects.filter(pk=allocation.pk).delete()


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
