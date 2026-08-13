import json

from testit import helpers as th


@th.django_unit_test()
def test_mojosec_sudo_projection_exposes_exact_admin_command_and_context(opts):
    from mojo.apps.incident.models import Event
    from mojo.apps.incident.services import mojosec

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
        evidence = event.metadata["mojosec"]["evidence"]
        th.assert_eq(event.source_ip, "192.0.2.30",
                     "reliably attributed sudo evidence should populate Event.source_ip")
        th.assert_eq(evidence, {
            "actor": "deploy", "target_user": "root", "tty": "pts/2",
            "boot_id": "a" * 32, "audit_session": 91,
            "attribution": "audit_session", "command": raw_command,
            "command_path": "/usr/bin/curl", "cwd": "/opt/api",
            "command_family": "network_client",
        }, "the security-admin Event should expose the exact bounded sudo command and context")
        encoded = json.dumps(evidence, sort_keys=True)
        for value in ("hunter2", "AKIAABCDEFGHIJKLMNOP",
                      "Bearer short-authorization-secret", "mysql-short-secret",
                      "positional-secret"):
            th.assert_in(value, encoded,
                         f"authorized sudo evidence must not redact secret-looking text {value}")
        th.assert_true("0" * 64 not in encoded,
                       "the receipt-only full command digest must not be copied into Event evidence")
    finally:
        Event.objects.filter(pk=event.pk).delete()


@th.django_unit_test()
def test_mojosec_sudo_projection_enforces_exact_bounds_and_honest_attribution(opts):
    from mojo.mojosec.evidence import build_evidence
    from mojo.apps.incident.services.mojosec_evidence import project

    ascii_command = "x" * 2048
    ascii_path = "y" * 512
    ascii_exact = project("auth.sudo_command", {
        "command": ascii_command, "command_path": ascii_path, "cwd": ascii_path,
    })["evidence"]
    th.assert_eq((ascii_exact["command"], ascii_exact["command_path"],
                  ascii_exact["cwd"]), (ascii_command, ascii_path, ascii_path),
                 "ASCII sudo fields at each byte ceiling must remain exact")
    exact_command = "é" * 1024
    exact_path = "é" * 256
    exact = project("auth.sudo_command", {
        "command": exact_command, "command_path": exact_path, "cwd": exact_path,
        "command_truncated": True, "command_path_truncated": True,
        "cwd_truncated": True,
    })["evidence"]
    th.assert_eq(exact["command"], exact_command,
                 "a command at the exact 2,048-byte UTF-8 limit must remain byte-exact")
    th.assert_eq((exact["command_path"], exact["cwd"]), (exact_path, exact_path),
                 "path and cwd at the exact 512-byte UTF-8 limit must remain byte-exact")
    th.assert_true(exact["command_truncated"] is True and
                   exact["command_path_truncated"] is True and
                   exact["cwd_truncated"] is True,
                   "literal sensor truncation markers must project with accepted values")
    th.assert_true("command_family" not in exact,
                   "a truncated executable path must never drive command classification")

    invalid = project("auth.sudo_command", {
        "command": exact_command + "a", "command_path": exact_path + "a",
        "cwd": exact_path + "a", "command_truncated": 1,
        "command_path_truncated": True, "cwd_truncated": True,
    })["evidence"]
    for field in ("command", "command_path", "cwd", "command_truncated",
                  "command_path_truncated", "cwd_truncated", "command_family"):
        th.assert_true(field not in invalid,
                       f"overflow or marker-only sudo field {field} must fail soft by omission")

    poisoned = project("auth.sudo_command", {
        "command": "contains\x00nul", "command_path": ["/usr/bin/curl"],
        "cwd": 7, "actor": "deploy", "target_user": "root",
    })["evidence"]
    th.assert_eq(poisoned, {"actor": "deploy", "target_user": "root",
                            "attribution": "none"},
                 "invalid optional command context must not stringify or poison valid identity")
    empty = project("auth.sudo_command", {
        "command": "", "command_path": "", "cwd": "",
    })["evidence"]
    th.assert_eq(empty, {"attribution": "none"},
                 "empty exact sudo fields must be omitted independently")
    unencodable = project("auth.sudo_command", {
        "command": "\ud800", "command_path": "\ud800", "cwd": "\ud800",
    })["evidence"]
    th.assert_eq(unencodable, {"attribution": "none"},
                 "text that cannot encode as UTF-8 must be omitted without transformation")

    built = build_evidence("auth.sudo_command", {
        "command": "x" * 2049, "command_path": "/usr/bin/curl", "cwd": "/opt/api",
    })
    truncated = project("auth.sudo_command", built)["evidence"]
    th.assert_eq(truncated["command"], "x" * 2048,
                 "the receiver must expose the sensor-retained command prefix unchanged")
    th.assert_true(truncated["command_truncated"] is True,
                   "a sensor-truncated command must never be presented as complete")
    th.assert_eq(truncated["command_family"], "network_client",
                 "an independently complete executable path may add a command family")
    th.assert_true("command_sha256" not in truncated,
                   "the truncation digest remains receipt-only rather than replacing command text")

    base = {
        "source_ip": "192.0.2.30", "actor": "deploy", "target_user": "root",
        "tty": "pts/1", "boot_id": "b" * 32, "audit_session": 92,
        "command": "/usr/bin/true", "command_path": "/usr/bin/true",
    }
    audit = project("auth.sudo_command", dict(
        base, attribution_provenance="audit_session"))
    th.assert_eq((audit["source_ip"], audit["evidence"]["attribution"]),
                 ("192.0.2.30", "audit_session"),
                 "a complete trusted audit-session proof may promote its source IP")
    who = project("auth.sudo_command", dict(base, attribution_provenance="who"))
    th.assert_eq((who["source_ip"], who["evidence"]["attribution"]),
                 ("192.0.2.30", "who"),
                 "a complete unique actor-plus-TTY who proof may promote its source IP")
    cases = (
        dict(base, attribution_provenance="none"),
        dict(base, attribution_provenance="claimed"),
        dict(base, attribution_provenance="audit_session", source_ip="invalid"),
        dict(base, attribution_provenance="audit_session", boot_id="bad"),
        dict(base, attribution_provenance="who", tty="bad tty"),
    )
    for attributes in cases:
        result = project("auth.sudo_command", attributes)
        th.assert_eq((result["source_ip"], result["evidence"]["attribution"]),
                     (None, "none"),
                     f"an incomplete or invalid attribution proof must be explicit: {attributes}")


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

    non_strings = dict(
        attributes,
        request_uri={"hidden": "path-secret"}, path=["path-list-secret"],
        user_agent=["ua-list-secret"],
        referrer={"hidden": "https://ref.invalid/referrer-secret"},
        host=["host-secret.invalid"], method={"hidden": "METHOD-SECRET"},
        request_id=["request-id-secret"], scheme={"hidden": "scheme-secret"},
        protocol=["protocol-secret"], tls_protocol={"hidden": "tls-secret"},
    )
    projected = project("web.denied", non_strings, count=1,
                        last_seen="2026-08-11T02:44:00Z")["evidence"]
    encoded = json.dumps(projected, sort_keys=True)
    for secret in ("path-secret", "path-list-secret", "ua-list-secret",
                   "referrer-secret", "host-secret", "METHOD-SECRET",
                   "request-id-secret", "scheme-secret", "protocol-secret",
                   "tls-secret"):
        th.assert_true(secret not in encoded,
                       f"non-string textual web evidence leaked {secret} into Event")
    for field in ("path", "user_agent", "referrer", "referrer_origin", "host",
                  "method", "request_id", "scheme", "protocol", "tls_protocol"):
        th.assert_true(field not in projected,
                       f"non-string textual web field {field} must be omitted")
    no_fallback = project("web.denied", dict(
        attributes, request_uri={}, path="/must-not-fallback"), count=1,
        last_seen="2026-08-11T02:44:00Z")["evidence"]
    th.assert_true("path" not in no_fallback,
                   "a present non-string request_uri must not fall back to another field")

    too_many = dict(
        attributes,
        upstream_status=",".join(str(200 + index) for index in range(9)),
        upstream_response_time=",".join("0.1" for unused in range(9)),
        upstream_bytes_received=",".join(str(index) for index in range(9)),
    )
    projected = project("web.denied", too_many, count=1,
                        last_seen="2026-08-11T02:44:00Z")["evidence"]
    th.assert_true("upstream_status" not in projected and
                   "upstream_response_time_ms" not in projected and
                   "upstream_bytes_received" not in projected,
                   "nine-member numeric lists must be omitted rather than truncated")
    hyphen_overflow = project("web.denied", dict(
        attributes, upstream_status="-,-,-,-,-,-,-,-,200"), count=1,
        last_seen="2026-08-11T02:44:00Z")["evidence"]
    th.assert_true("upstream_status" not in hyphen_overflow,
                   "the eight-member cap must count '-' placeholders too")
    mixed = project("web.denied", dict(
        attributes, upstream_response_time="-,0.1,-,0.2"), count=1,
        last_seen="2026-08-11T02:44:00Z")["evidence"]
    th.assert_eq(mixed["upstream_response_time_ms"], [100, 200],
                 "mixed '-' upstream retries below the cap must preserve numeric members")

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

    controls = "Mozilla/5.0 safe\u202eevil zero\u200bwidth isolate\u2066text\u2069"
    display = project("web.denied", dict(attributes, user_agent=controls), count=1,
                      last_seen="2026-08-11T02:44:00Z")["evidence"]["user_agent"]["display"]
    for character in ("\u202e", "\u200b", "\u2066", "\u2069"):
        th.assert_true(character not in display,
                       "UA display must remove Unicode format and bidi controls")
    th.assert_in("Mozilla/5.0 safe evil zero width isolate text", display,
                 "UA display should retain readable text after format-control removal")


@th.django_unit_test()
def test_system_kind_projection_names_unit_and_failure_kind(opts):
    from mojo.apps.incident.services.mojosec_evidence import project

    failure = project("system.service_error", {
        "unit": "api.service", "failure_kind": "exit-code", "priority": 4,
        "message": "api.service: Failed with result 'exit-code'.",
    })
    th.assert_eq(failure["evidence"],
                 {"unit": "api.service", "failure_kind": "exit-code"},
                 "the central Event must name the failed unit and failure kind, "
                 "and nothing else")
    th.assert_eq(failure["source_ip"], None,
                 "system kinds carry no actor address to promote")

    oom = project("system.oom", {
        "unit": "kernel",
        "message": "Out of memory: Killed process 21437 (gunicorn)",
    })
    th.assert_eq(oom["evidence"], {"unit": "kernel"},
                 "kernel OOM projection must carry the kernel attribution only")

    for bad_unit in ("nodots", "../x.service", 7, None):
        result = project("system.service_error", {
            "unit": bad_unit, "failure_kind": "exit-code",
        })
        th.assert_true("unit" not in result["evidence"],
                       f"malformed unit {bad_unit!r} must be omitted from projection")
    malformed_kind = project("system.service_error", {
        "unit": "api.service", "failure_kind": "Exit Code!",
    })
    th.assert_true("failure_kind" not in malformed_kind["evidence"],
                   "a failure kind outside systemd's result vocabulary must be omitted")


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
