import json
import hashlib

from testit import helpers as th


@th.django_unit_test()
def test_mojosec_sudo_projection_redacts_adversarial_secret_classes(opts):
    from mojo.apps.incident.models import Event
    from mojo.apps.incident.services import mojosec

    secret_values = (
        "hunter2", "eyJhbGciOiJIUzI1NiJ9.abcdefghijk.signature123",
        "AKIAABCDEFGHIJKLMNOP", "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "VeryLongHighEntropyValue_0123456789_ABCDEFG", "-----BEGIN_PRIVATE_KEY-----",
        "Bearer short-authorization-secret", "mysql-short-secret", "positional-secret",
    )
    raw_command = (
        "/usr/bin/curl --password hunter2 "
        "TOKEN=eyJhbGciOiJIUzI1NiJ9.abcdefghijk.signature123 "
        "https://user:pass@example.invalid/private?token=AKIAABCDEFGHIJKLMNOP "
        "ghp_abcdefghijklmnopqrstuvwxyz123456 "
        "VeryLongHighEntropyValue_0123456789_ABCDEFG -----BEGIN_PRIVATE_KEY----- "
        "-H 'Authorization: Bearer short-authorization-secret' "
        "mysql -pmysql-short-secret positional-secret"
    )
    sensor_event = {
        "id": "b" * 64, "kind": "auth.sudo_command",
        "observed_at": "2026-08-09T12:00:00Z",
        "first_seen": "2026-08-09T12:00:00Z",
        "last_seen": "2026-08-09T12:00:00Z", "severity": "high",
        "summary": "raw summary secret must not project", "count": 1,
        "recommendation": "review",
        "attributes": {
            "source_ip": "192.0.2.30", "actor": "deploy", "target_user": "root",
            "tty": "pts/2", "boot_id": "a" * 32, "audit_session": "91",
            "attribution_provenance": "audit_session", "cwd": "/opt/api",
            "command_path": "/usr/bin/curl", "command_sha256": "0" * 64,
            "command": raw_command,
        },
    }
    batch = {
        "sensor_id": "adversarial-sensor", "installation_key_id": 42,
        "version": 1, "policy_revision": "secret-policy-label",
    }
    event = mojosec._event_projection(batch, sensor_event)
    try:
        encoded = json.dumps({
            "metadata": event.metadata, "title": event.title, "details": event.details,
        }, sort_keys=True)
        for secret in secret_values:
            th.assert_true(secret not in encoded,
                           f"Event projection leaked adversarial secret class {secret[:12]}")
            th.assert_true(hashlib.sha256(secret.encode()).hexdigest()[:12] not in encoded,
                           "Event projection must not expose per-token secret digests")
        th.assert_eq(event.source_ip, "192.0.2.30",
                     "reliably attributed sudo evidence should populate Event.source_ip")
        command = event.metadata["mojosec"]["evidence"]["command"]
        th.assert_eq(command, {"family": "network_client", "detail": "<redacted>"},
                     "Event should expose only a server-owned family and constant marker")
        th.assert_true("/usr/bin/curl" not in encoded and "0" * 64 not in encoded,
                       "raw executable and command digest must remain receipt-only")
    finally:
        Event.objects.filter(pk=event.pk).delete()


@th.django_unit_test()
def test_mojosec_sudo_unknown_projection_has_constant_non_oracle_shape(opts):
    from mojo.apps.incident.services.mojosec_evidence import project

    unknown_inputs = (
        ("sudo short-positional-secret", "short-positional-secret"),
        ("/opt/path-secret/curl --version", "/opt/path-secret/curl"),
        ("'unterminated secret", "secret-executable"),
    )
    projections = []
    for raw, command_path in unknown_inputs:
        projected = project("auth.sudo_command", {
            "actor": "deploy", "target_user": "root", "tty": "pts/1",
            "command": raw, "command_path": command_path,
            "command_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        })["evidence"]["command"]
        projections.append(projected)
        encoded = json.dumps(projected, sort_keys=True)
        th.assert_true(raw not in encoded and command_path not in encoded and
                       hashlib.sha256(raw.encode()).hexdigest() not in encoded,
                       "unknown commands must expose no raw string, path, or digest oracle")
    th.assert_eq(projections, [{"family": "unknown", "detail": "<redacted>"}] * 3,
                 "all unknown command shapes must project identically")

    mysql = project("auth.sudo_command", {
        "command": "/usr/bin/mysql -pguessable-secret", "command_path": "/usr/bin/mysql",
        "command_sha256": hashlib.sha256(b"guessable-secret").hexdigest(),
    })["evidence"]["command"]
    th.assert_eq(mysql, {"family": "database_client", "detail": "<redacted>"},
                 "a known executable may map only to its server-owned command family")
    th.assert_true("guessable-secret" not in json.dumps(mysql),
                   "known-family projection must not leak inline mysql passwords")


@th.django_unit_test()
def test_mojosec_web_projection_never_exposes_token_path_or_query(opts):
    from mojo.apps.incident.services.mojosec_evidence import project

    secret = "short-secret"
    projected = project("web.error", {
        "source_ip": "192.0.2.5", "method": "GET", "status": 500,
        "request_uri": f"/password/{secret}?authorization=another-secret",
        "referrer": "https://user:pass@example.invalid/private?token=secret",
    })["evidence"]
    encoded = json.dumps(projected, sort_keys=True)
    th.assert_eq(projected["path"], "/password/~token",
                 "token-context path segments must use a shared safe marker")
    th.assert_true(secret not in encoded and "another-secret" not in encoded,
                   "path and query secrets must never enter Event evidence")
    th.assert_true("referrer_origin" not in projected,
                   "credential-bearing referrers must be rejected rather than repaired")


@th.django_unit_test()
def test_mojosec_web_projection_is_rich_scrubbed_and_aggregate_truthful(opts):
    from mojo.apps.incident.services.mojosec_evidence import project

    attributes = {
        "source_ip": "192.0.2.5", "peer_ip": "10.0.0.8", "method": "GET",
        "status": 403, "host": "example.invalid", "request_uri": "/private?id=secret",
        "upstream_status": "403", "request_id": "a" * 32, "scheme": "https",
        "protocol": "HTTP/2.0", "tls_protocol": "TLSv1.3",
        "tls_cipher": "TLS_AES_256_GCM_SHA384", "remote_port": 52344,
        "peer_port": 443, "server_port": 443, "request_length": 512,
        "response_bytes": 1024, "response_body_bytes": 900, "request_time": "0.125",
        "upstream_connect_time": "0.010", "upstream_header_time": "0.020",
        "upstream_response_time": "0.030", "upstream_response_length": "777",
        "upstream_bytes_received": "800", "upstream_bytes_sent": "400",
        "referrer": "https://ref.invalid/reset/short-secret?q=secret#fragment",
        "user_agent": "curl/8.9 Authorization: Bearer secret-token-value",
    }
    one = project("web.denied", attributes, count=1,
                  last_seen="2026-08-11T02:44:00Z")["evidence"]
    th.assert_eq(one["response_bytes"], 1024,
                 "count-one Event evidence must expose validated response bytes")
    th.assert_eq(one["referrer"], "https://ref.invalid/reset/~token",
                 "public referrer must retain useful origin/path without query or token")
    th.assert_eq(one["referrer_origin"], "https://ref.invalid",
                 "legacy referrer_origin compatibility must remain")
    encoded = json.dumps(one, sort_keys=True)
    th.assert_true("secret-token-value" not in encoded and "q=secret" not in encoded,
                   "public web evidence must scrub UA/referrer secret material")
    th.assert_in("<redacted>", one["user_agent"]["display"],
                 "the public UA display must mark scrubbed authorization material")

    many = project("web.denied", attributes, count=2,
                   last_seen="2026-08-11T02:44:01Z")["evidence"]
    th.assert_true("response_bytes" not in many and "user_agent" not in many,
                   "volatile request values must not appear as aggregate invariants")
    sample = many["last_occurrence_sample"]
    th.assert_eq((sample["semantics"], sample["observed_at"], sample["request_id"]),
                 ("last_occurrence", "2026-08-11T02:44:01Z", "a" * 32),
                 "aggregate sample semantics and authoritative last_seen must be explicit")

    poisoned = dict(attributes, response_bytes="not-a-number",
                    upstream_response_time="0.1,broken", remote_port="70000")
    projected = project("web.denied", poisoned, count=1,
                        last_seen="2026-08-11T02:44:00Z")["evidence"]
    th.assert_true("response_bytes" not in projected and
                   "upstream_response_time_ms" not in projected and
                   "remote_port" not in projected,
                   "invalid projected subfields must be omitted independently without raising")

    secrets = (
        "eyJhbGciOiJIUzI1NiJ9.abcdefghijk.signature123",
        "-----BEGIN PRIVATE KEY-----", "AKIAABCDEFGHIJKLMNOP",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "VeryLongHighEntropyValue_0123456789_ABCDEFG", "token=short-secret",
    )
    adversarial = project("web.denied", dict(
        attributes, user_agent="Agent/1.0 " + " ".join(secrets)), count=1,
        last_seen="2026-08-11T02:44:00Z")["evidence"]["user_agent"]
    encoded = json.dumps(adversarial, sort_keys=True)
    for secret in secrets:
        th.assert_true(secret not in encoded,
                       f"the UA display leaked adversarial secret class {secret[:12]}")


@th.django_unit_test()
def test_mojosec_local_session_projection_is_distinct_from_remote_login(opts):
    from mojo.apps.incident.services.mojosec_evidence import project

    projected = project("auth.session_open", {
        "source_ip": "192.0.2.80", "attribution_provenance": "audit_session",
        "service": "systemd-user", "target_user": "www", "target_uid": 80,
        "opener_uid": 0, "producer_uid": 0, "producer_pid": 4123,
        "producer_comm": "(systemd)", "producer_exe": "/usr/lib/systemd/systemd",
        "systemd_unit": "user@80.service", "boot_id": "b" * 32,
        "audit_session": 44, "audit_loginuid": 80,
    })
    th.assert_eq(projected["source_ip"], "192.0.2.80",
                 "only exact audit-session attribution may populate local session source IP")
    th.assert_eq(projected["evidence"]["target_uid"], 80,
                 "local session evidence must identify the target account")

    from mojo.apps.incident.models import Event
    from mojo.apps.incident.services import mojosec
    sensor_event = {
        "id": "c" * 64, "kind": "auth.session_open", "severity": "info",
        "summary": "PAM service session opened", "count": 1,
        "recommendation": "none", "attributes": {
            "service": "systemd-user", "target_user": "www", "target_uid": 80,
            "opener_uid": 0, "producer_uid": 0, "producer_pid": 4123,
            "producer_comm": "(systemd)",
            "producer_exe": "/usr/lib/systemd/systemd",
            "systemd_unit": "user@80.service", "boot_id": "b" * 32,
            "audit_session": 44, "audit_loginuid": 80,
        },
        "observed_at": "2026-08-11T02:44:00Z",
        "first_seen": "2026-08-11T02:44:00Z",
        "last_seen": "2026-08-11T02:44:00Z",
    }
    event = mojosec._event_projection({
        "sensor_id": "session-test", "installation_key_id": 42,
        "version": 1, "policy_revision": "test",
    }, sensor_event)
    try:
        th.assert_eq(event.level, 2,
                     "local PAM service opens must be informational centrally")
        th.assert_in("local PAM service session", event.title,
                     "the server-owned title must not call a local PAM open a host login")
    finally:
        Event.objects.filter(pk=event.pk).delete()


@th.django_unit_test()
def test_mojosec_ssh_projection_retains_safe_session_context(opts):
    from mojo.apps.incident.services.mojosec_evidence import project

    projected = project("auth.ssh_login", {
        "source_ip": "192.0.2.91", "user": "deploy", "auth_method": "publickey",
        "tty": "pts/2", "boot_id": "a" * 32, "audit_session": 91,
    })
    th.assert_eq(projected["source_ip"], "192.0.2.91",
                 "trusted SSH evidence must populate the canonical source IP")
    th.assert_eq(projected["evidence"]["boot_id"], "a" * 32,
                 "SSH Event evidence must retain the safe boot identity")
    th.assert_eq(projected["evidence"]["audit_session"], 91,
                 "SSH Event evidence must retain the safe audit session")
