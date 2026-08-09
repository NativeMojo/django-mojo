import json

from testit import helpers as th


@th.django_unit_test()
def test_mojosec_sudo_projection_redacts_adversarial_secret_classes(opts):
    from mojo.apps.incident.models import Event
    from mojo.apps.incident.services import mojosec

    secret_values = (
        "hunter2", "eyJhbGciOiJIUzI1NiJ9.abcdefghijk.signature123",
        "AKIAABCDEFGHIJKLMNOP", "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "VeryLongHighEntropyValue_0123456789_ABCDEFG", "-----BEGIN_PRIVATE_KEY-----",
    )
    raw_command = (
        "/usr/bin/curl --password hunter2 "
        "TOKEN=eyJhbGciOiJIUzI1NiJ9.abcdefghijk.signature123 "
        "https://user:pass@example.invalid/private?token=AKIAABCDEFGHIJKLMNOP "
        "ghp_abcdefghijklmnopqrstuvwxyz123456 "
        "VeryLongHighEntropyValue_0123456789_ABCDEFG -----BEGIN_PRIVATE_KEY-----"
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
        th.assert_eq(event.source_ip, "192.0.2.30",
                     "reliably attributed sudo evidence should populate Event.source_ip")
        command = event.metadata["mojosec"]["evidence"]["command"]
        th.assert_eq(command["executable"], "/usr/bin/curl",
                     "scrubbed command context should retain the executable")
        th.assert_true("sha256" in command and "redacted" in command,
                       "scrubbed command context should retain digest and safe shape")
    finally:
        Event.objects.filter(pk=event.pk).delete()


@th.django_unit_test()
def test_mojosec_ambiguous_sudo_command_falls_back_to_executable_and_digest(opts):
    from mojo.apps.incident.services.mojosec_evidence import project

    projected = project("auth.sudo_command", {
        "actor": "deploy", "target_user": "root", "tty": "pts/1",
        "command": "/usr/bin/curl 'unterminated --password secret",
        "command_path": "/usr/bin/curl", "command_sha256": "a" * 64,
    })["evidence"]["command"]
    th.assert_eq(set(projected), {"executable", "sha256"},
                 "ambiguous shell parsing must expose only executable plus digest")


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
