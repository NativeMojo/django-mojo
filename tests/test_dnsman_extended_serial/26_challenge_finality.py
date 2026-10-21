"""Producer integration and conservative all-attempt outcomes."""
import uuid
from unittest import mock

from testit import helpers as th


@th.django_unit_test("inline provider evidence is committed before wire and survives mirror rollback")
def test_inline_send_survives_caller_rollback(opts):
    from django.db import transaction
    from mojo.apps.account.models import Group
    from mojo.apps.dnsman.models import Domain, Certificate, DnsRecordReservation, DnsChallengeAttempt
    from mojo.apps.dnsman.services import dns, record_reservations
    token = uuid.uuid4().hex
    group = Group.objects.create(name="challenge-producer-" + token)
    domain = Domain.objects.create(group=group, name=token + ".example", provider="route53",
                                   status="active", verified=True, hosted_zone_id="Z-test-owned")
    cert = Certificate.objects.create(domain=domain, common_name=domain.name)
    reservation = record_reservations.reserve(cert, "_acme-challenge." + domain.name, ["test_owned_value"])
    seen = []

    class Adapter:
        name = "route53"
        def upsert_record(self, *args, **kwargs):
            rows = list(DnsChallengeAttempt.objects.filter(kind="provider_mutation",
                owner__binding__reservation=reservation.pk).values("state"))
            seen.append(rows)
            return "C-test-owned"

    class Rollback(Exception):
        pass

    try:
        with mock.patch.object(dns, "get_adapter", return_value=Adapter()):
            try:
                with transaction.atomic():
                    result = dns.upsert_record(domain, "TXT", reservation.record_name,
                                               reservation.record_values, reservation=reservation)
                    assert result.change_id == "C-test-owned", "The actual inline producer must preserve provider acceptance"
                    raise Rollback()
            except Rollback:
                pass
        rows = list(DnsChallengeAttempt.objects.filter(kind="provider_mutation",
            owner__binding__reservation=reservation.pk).values("state", "provider_change_id"))
        assert seen == [[{"state": "prepared"}]], "The provider must observe a committed possibly-sent journal intent before its wire callback"
        assert rows == [{"state": "accepted", "provider_change_id": "C-test-owned"}], "Outer rollback must preserve the exact independently committed provider receipt"
        assert not DnsRecordReservation.objects.get(pk=reservation.pk).mutation_attempted, "The rolled-back mirror must not be mistaken for durable provider evidence"
    finally:
        domain.delete()
        group.delete()


@th.django_unit_test("durable hub retirement filters rolled-back leases before every compatibility union and preserves identical peer values")
def test_hub_barrier_survives_mirror_rollback(opts):
    from django.db import transaction
    from objict import objict
    from mojo.apps.account.models import Group
    from mojo.apps.dnsman.models import AcmeHubDelegation, AcmeHubParticipant, AcmeHubChallengeLease
    from mojo.apps.dnsman.services import acme_hub as hub, challenge_dns, challenge_attempts as attempts, acme_hub_upgrade as upgrade
    from mojo.helpers import dates
    token = uuid.uuid4().hex
    group = Group.objects.create(name="barrier-owner-" + token)
    group.get_uuid()
    allocation = AcmeHubDelegation.objects.create(group=group, owner_project_uuid=group.uuid,
        client_ref="owner", domain_name=token + ".example", source_name="_acme-challenge." + token + ".example",
        target_name=token + ".hub.example", allocation_zone_name="hub.example", allocation_zone_id="ZTESTOWNED")
    owner = AcmeHubParticipant.objects.create(allocation=allocation, group=group,
        consumer_project_uuid=group.uuid, client_ref="owner", role="owner")
    peer = AcmeHubParticipant.objects.create(allocation=allocation, group=group,
        consumer_project_uuid=group.uuid, client_ref="peer", role="trusted")
    old = AcmeHubChallengeLease.objects.create(allocation=allocation, participant=owner,
        challenge_ref="old", record_values=["retired_unique", "same"], state="active", expires_at=dates.add(seconds=600))
    live = AcmeHubChallengeLease.objects.create(allocation=allocation, participant=peer,
        challenge_ref="live", record_values=["peer_unique", "same"], state="active", expires_at=dates.add(seconds=600))
    class Provider:
        def __init__(self):
            self.writes = []
        def get_zone(self, zone):
            return objict(id=zone, name="hub.example", private=False, name_servers=["ns.test.example"])
        def upsert_record(self, zone, kind, name, values, **kwargs):
            self.writes.append(list(values))
            return "C-test-only"
        def list_records(self, *args):
            return []
    class DNS:
        normalize_name = staticmethod(lambda value: value.rstrip(".").lower())
        def find_zone_nameservers(self, name):
            return objict(zone=name, nameservers=["ns.test.example"], error=None)
        def query_txt(self, name):
            return objict(error=None, txt_values=["retired_unique", "same", "peer_unique"])
    provider, dns = Provider(), DNS()
    journal = attempts.Journal()
    bound = challenge_dns.hub_owner(allocation, owner, "old")
    try:
        try:
            with transaction.atomic(), hub._allocation_lock(allocation.pk):
                AcmeHubChallengeLease.objects.select_for_update().get(pk=old.pk)
                with attempts.scope_lock(challenge_dns.hub_scope(allocation)) as anchor:
                    journal.append("retirement_barrier", challenge_dns.hub_scope(allocation), bound,
                        "route53", {"retired": True}, anchor.identity)
                    AcmeHubChallengeLease.objects.filter(pk=old.pk).update(state="withdrawn")
                raise RuntimeError("test mirror rollback")
        except RuntimeError:
            pass
        old.refresh_from_db()
        assert old.state == "active", "The regression must actually restore the stale ACTIVE lease mirror"
        with upgrade.admission(state_reader=lambda: objict(mode="enabled")):
            for drive in (
                    lambda: hub.reconcile(allocation, route53_client=provider, dns_probe=dns),
                    lambda: hub._reconcile_locked(allocation, route53_client=provider, dns_probe=dns),
                    lambda: hub.sweep(route53_client=provider, dns_probe=dns)):
                drive()
                assert hub._desired_values(allocation) == ["peer_unique", "same"], "Every ordinary/compatibility union must filter the retired lease before unioning, preserving an identical live peer contribution"
        assert provider.writes and all(values == ["peer_unique", "same"] for values in provider.writes), "A caller rollback must never cause any actual later reconciliation send to re-add the retired unique value"
        with th.assert_raises(attempts.JournalError):
            challenge_dns.require_hub_publish(allocation, owner, "old")
        with th.assert_raises(attempts.JournalError):
            challenge_dns.require_hub_publish(allocation, None, "old")
    finally:
        AcmeHubChallengeLease.objects.filter(allocation=allocation).delete()
        AcmeHubParticipant.objects.filter(allocation=allocation).delete()
        AcmeHubDelegation.objects.filter(pk=allocation.pk).delete()
        group.delete()


@th.django_unit_test("qualified cleanup settles every exact prior ID and retry reuses final receipt without another mutation")
def test_cleanup_exact_finality_and_lost_reply(opts):
    from mojo.apps.dnsman.services import challenge_attempts as attempts, challenge_finality
    token = uuid.uuid4().hex
    journal = attempts.Journal(identity={"source_sha": "a" * 40, "manifest_sha256": "b" * 64})
    scope = {"provider": "route53", "account_id": "123456789012", "zone_id": "ZTESTOWNED",
        "name": token + ".example", "type": "TXT", "scope_class": "inline_acme_txt"}
    owner = {"identity": {"kind": "finality-test", "operation": token}, "binding": {"scope": scope}}
    with attempts.scope_lock(scope) as anchor:
        origin = journal.append("origin", scope, owner, "route53", {"legacy_history": False, "owned_values": ["owned_test"]}, anchor.identity)
        intent = journal.prepare("provider_mutation", scope, owner, "route53", {"request": "older"}, anchor)
        intent.send(lambda: None)
        journal.receipt(intent.row, "accepted", change_id="C-older")
    class Client:
        def __init__(self):
            from botocore.config import Config
            from types import SimpleNamespace
            self.meta = SimpleNamespace(config=Config(connect_timeout=3, read_timeout=5,
                retries={"total_max_attempts": 1, "mode": "standard"}))
            self.ids, self.mutations = [], []
        def get_change(self, Id):
            self.ids.append(Id)
            return {"ChangeInfo": {"Id": Id, "Status": "INSYNC"}}
        def list_resource_record_sets(self, **kwargs):
            return {"IsTruncated": False, "ResourceRecordSets": [{"Name": scope["name"], "Type": "TXT", "TTL": 72,
                "ResourceRecords": [{"Value": '"owned_test"'}, {"Value": '"external raw"'}]}]}
        def change_resource_record_sets(self, **kwargs):
            self.mutations.append(kwargs)
            return {"ChangeInfo": {"Id": "C-cleanup", "Status": "PENDING"}, "ResponseMetadata": {"RequestId": "test-request", "RetryAttempts": 0}}
        def close(self):
            pass
    client = Client()
    receipt = {"ledger_cutover_sequence": 0, "scopes": [{"provider": "route53", "account_id": "123456789012", "zone_id": "ZTESTOWNED", "classes": ["inline_acme_txt"]}]}
    engine = challenge_finality.Cleanup(journal=journal, coverage=lambda identity: (receipt, "c" * 64), client=lambda: client)
    first = engine.execute(owner, origin)
    assert first["complete"] and first["retirement_recorded"] and first["all_prior_final"], "Finality requires the exact older ID and this cleanup ID, plus independent retirement and source coverage"
    assert client.ids == ["C-older", "C-cleanup"], "The latest cleanup ID must never stand in for an earlier accepted request"
    changes = client.mutations[0]["ChangeBatch"]["Changes"]
    assert [item["Action"] for item in changes] == ["DELETE", "CREATE"], "Cleanup must submit one raw conditional batch without survivor UPSERT"
    assert changes[1]["ResourceRecordSet"]["ResourceRecords"] == [{"Value": '"external raw"'}], "Unrelated raw entries must survive qualified removal"
    again = engine.execute(owner, origin)
    assert again["complete"] and len(client.mutations) == 1, "A lost reply must resolve the same final receipt and barrier without a second provider mutation"


@th.django_unit_test("an older prepared request and missing coverage cannot be washed away by listing absence")
def test_prior_unknown_and_missing_coverage_block_cleanup(opts):
    from mojo.apps.dnsman.services import challenge_attempts as attempts, challenge_finality
    from mojo.deploy.acme_dns_coverage import CoverageError
    token = uuid.uuid4().hex
    scope = {"provider": "route53", "zone_id": "ZTESTOWNED", "account_id": "123456789012", "name": token + ".example", "scope_class": "inline_acme_txt"}
    owner = {"identity": {"kind": "unknown-test", "operation": token}, "binding": {"scope": scope}}
    journal = attempts.Journal()
    with attempts.scope_lock(scope) as anchor:
        origin = journal.append("origin", scope, owner, "route53", {"legacy_history": False, "owned_values": ["owned"]}, anchor.identity)
        journal.prepare("provider_mutation", scope, owner, "route53", {"request": "possibly_sent"}, anchor)
    calls = []
    receipt = {"ledger_cutover_sequence": 0, "scopes": [{"provider": "route53", "account_id": "123456789012", "zone_id": "ZTESTOWNED", "classes": ["inline_acme_txt"]}]}
    engine = challenge_finality.Cleanup(journal=journal, coverage=lambda identity: (receipt, "c" * 64), client=lambda: calls.append("forbidden"))
    projected = engine.execute(owner, origin)
    assert not projected["complete"] and projected["error_code"] == "prior_attempt_unknown" and not calls, "A possibly-sent prior request must block before even observing current provider absence"
    def missing(identity):
        raise CoverageError("producer_coverage_unavailable")
    engine = challenge_finality.Cleanup(journal=journal, coverage=missing, client=lambda: calls.append("forbidden"))
    with th.assert_raises(attempts.JournalError):
        engine.execute(owner, origin)
    assert not calls and not journal.project(owner=owner, kinds=["retirement_barrier"]), "Missing enrollment must never fabricate finality or mutate provider/retirement state"
