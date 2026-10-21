"""Raw conditional identity and actual single-attempt botocore HTTP bytes."""
import copy
import os
import threading
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest import mock

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from testit import helpers as th


def raw_set():
    return {"Name": "_acme-challenge.Example.test.", "Type": "TXT", "TTL": 187,
            "ResourceRecords": [{"Value": '"owned"'}, {"Value": '"unrelated" "-chunk"'},
                                {"Value": '"escaped\\\\value"'}]}


@th.django_unit_test("conditional cleanup preserves exact raw delete identity and survivor bytes")
def test_raw_delete_create_and_absence(opts):
    from mojo.helpers.aws import route53_challenge as api
    raw = raw_set()
    request = api.batch("ZTEST", raw["Name"], raw, ["owned"], comment="mojo-acme:test")
    changes = request["ChangeBatch"]["Changes"]
    assert [c["Action"] for c in changes] == ["DELETE", "CREATE"], "Cleanup must be one exact DELETE plus CREATE batch, never survivor UPSERT"
    assert changes[0]["ResourceRecordSet"] == raw, "DELETE must carry every original Name/TTL/raw TXT byte"
    assert changes[1]["ResourceRecordSet"] == dict(raw, ResourceRecords=raw["ResourceRecords"][1:]), "Unrelated raw chunks and escapes must survive byte-for-byte with original TTL"
    one = dict(raw, ResourceRecords=[raw["ResourceRecords"][0]])
    assert len(api.batch("ZTEST", raw["Name"], one, ["owned"], comment="x")["ChangeBatch"]["Changes"]) == 1, "Last owned value uses exact DELETE alone"
    created = api.batch("ZTEST", raw["Name"], None, [], ["owned"], comment="x")
    assert [c["Action"] for c in created["ChangeBatch"]["Changes"]] == ["CREATE"], "An absent first publish must condition on absence with CREATE alone"
    assert api.batch("ZTEST", raw["Name"], raw, ["owned"], ["owned"], comment="x") is None, "A peer's identical live desired value must preserve the existing raw set without a write"
    for bad in (dict(raw, TTL=None), dict(raw, TTL=True), dict(raw, SetIdentifier="weighted"),
                dict(raw, HealthCheckId="check"), dict(raw, AliasTarget={}),
                dict(raw, ResourceRecords=[{"Value": '"owned"'}, {"Value": '"ow" "ned"'}])):
        with th.assert_raises(api.ChallengeError):
            api.batch("ZTEST", raw["Name"], bad, ["owned"], comment="x")


@th.django_unit_test("targeted snapshot rejects incomplete and multiple matching identities")
def test_bounded_complete_snapshot(opts):
    from mojo.helpers.aws import route53_challenge as api
    raw = raw_set()
    class Client:
        def list_resource_record_sets(self, **arguments):
            self.arguments = arguments
            return self.result
    client = Client()
    client.result = {"ResourceRecordSets": [raw], "IsTruncated": False}
    assert api.snapshot(client, "ZTEST", raw["Name"]) == raw, "One complete raw simple TXT set must be accepted unchanged"
    assert client.arguments == {"HostedZoneId": "ZTEST", "StartRecordName": "_acme-challenge.example.test", "StartRecordType": "TXT", "MaxItems": "2"}, "Snapshot must be a single bounded name/type query"
    for result in ({"ResourceRecordSets": [raw, raw], "IsTruncated": False},
                   {"ResourceRecordSets": [raw], "IsTruncated": True},
                   {"ResourceRecordSets": [raw], "IsTruncated": True, "NextRecordName": raw["Name"], "NextRecordType": "TXT"},
                   {"ResourceRecordSets": [], "IsTruncated": "false"}):
        client.result = result
        with th.assert_raises(api.ChallengeError):
            api.snapshot(client, "ZTEST", raw["Name"])


@contextmanager
def sdk_endpoint(mode):
    received, release = [], threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if mode == "timeout":
                release.wait(2)
            body = b'<ErrorResponse xmlns="https://route53.amazonaws.com/doc/2013-04-01/"><Error><Type>Receiver</Type><Code>ServiceUnavailable</Code><Message>test-owned</Message></Error><RequestId>test-request</RequestId></ErrorResponse>'
            try:
                self.send_response(503)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port), received
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


@th.django_unit_test("actual botocore 503 and timeout produce one serialized mutation request")
def test_actual_sdk_single_wire_with_retry_defaults(opts):
    from mojo.helpers.aws import route53_challenge as api
    for mode in ("503", "timeout"):
        with sdk_endpoint(mode) as (endpoint, received), mock.patch.dict(os.environ, {"AWS_MAX_ATTEMPTS": "8", "AWS_RETRY_MODE": "standard"}):
            actual = boto3.Session(aws_access_key_id="test-owned-access", aws_secret_access_key="test-owned-secret", region_name="us-east-1")
            class Session:
                def client(self, service, config):
                    self.config = config
                    return actual.client(service, endpoint_url=endpoint,
                                         config=config.merge(Config(read_timeout=0.1)))
            session = Session()
            client = api.mutation_client(session)
            request = api.batch("ZTEST", raw_set()["Name"], raw_set(), ["owned"], comment="mojo-acme:wire-test")
            try:
                with th.assert_raises(api.ChallengeError):
                    api.submit(client, request)
            finally:
                client.close()
            assert session.config.retries == {"total_max_attempts": 1, "mode": "standard"}, "Actual session.client must receive total_max_attempts=1 despite higher environment defaults"
            assert len(received) == 1, f"The real botocore {mode} path must perform exactly one HTTP mutation"
            root = ET.fromstring(received[0])
            actions = [node.text for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "Action"]
            assert actions == ["DELETE", "CREATE"], "Actual SDK XML must contain the exact atomic conditional replacement"
            values = [node.text for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "Value"]
            assert raw_set()["ResourceRecords"][1]["Value"] in values, "The actual serialized request must preserve raw TXT chunk encoding"


@th.django_unit_test("external generation change rejects whole batch without survivor loss or retry")
def test_external_addition_conditions_the_entire_batch(opts):
    from mojo.helpers.aws import route53_challenge as api
    original = raw_set()
    current = copy.deepcopy(original)
    request = api.batch("ZTEST", original["Name"], original, ["owned"], comment="mojo-acme:race-test")
    current["ResourceRecords"].append({"Value": '"external-added"'})
    class Client:
        meta = SimpleNamespace(config=Config(connect_timeout=3, read_timeout=5, retries={"total_max_attempts": 1, "mode": "standard"}))
        calls = 0
        def change_resource_record_sets(self, **arguments):
            self.calls += 1
            assert arguments["ChangeBatch"]["Changes"][0]["ResourceRecordSet"] != current, "The concurrent external addition must invalidate the observed DELETE generation"
            raise ClientError({"Error": {"Code": "InvalidChangeBatch"}, "ResponseMetadata": {"RetryAttempts": 0}}, "ChangeResourceRecordSets")
    client = Client()
    try:
        api.submit(client, request)
        raise AssertionError("A changed RRset generation was accepted")
    except api.ChallengeError as error:
        assert error.no_change and error.error_code == "generation_changed", "Only the exact one-attempt documented rejection is conclusive no-change"
    assert client.calls == 1 and current["ResourceRecords"][-1]["Value"] == '"external-added"', "Generation mismatch must preserve all values and never issue another mutation"
