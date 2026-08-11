import json
import os
import subprocess
import sys
import tempfile
import types
from unittest import mock

from testit import helpers as th


@th.django_unit_test()
def test_expected_fim_changes_are_annotated_never_suppressed(opts):
    import datetime
    import hashlib
    import tempfile

    from mojo.mojosec.expected_changes import annotation, load_manifest

    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "settings.py")
        digest = hashlib.sha256(b"new bytes").hexdigest()
        manifest_path = os.path.join(root, "expected.json")
        manifest = {
            "schema": "mojosec.expected_changes", "version": 1,
            "entries": [{
                "path": path, "change": "modified", "sha256": digest,
                "expires_at": "2099-01-01T00:00:00Z",
                "deployment_id": "deploy-123",
            }],
        }
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        os.chmod(manifest_path, 0o600)

        entries = load_manifest(manifest_path, require_root=False)
        value = annotation(entries, path, "modified", {}, {"sha256": digest})
        th.assert_eq(value["deployment_id"], "deploy-123",
                     "an exact path+change+digest+live-expiry match must annotate")
        th.assert_eq(annotation(entries, path, "modified", {}, {"sha256": "0" * 64}), None,
                     "a digest mismatch must not be called an expected deploy change")
        expired = annotation(
            entries, path, "modified", {}, {"sha256": digest},
            now=datetime.datetime(2100, 1, 1, tzinfo=datetime.timezone.utc))
        th.assert_eq(expired, None, "expired deployment expectations must not annotate")


def _journal_config(max_records=10, max_bytes=65536):
    return {
        "max_records": max_records, "max_bytes_per_poll": max_bytes,
        "max_record_bytes": 16384, "timeout_seconds": 5, "lookback_seconds": 300,
    }


def _journal_record(cursor, source_ip):
    return {
        "__CURSOR": cursor, "SYSLOG_IDENTIFIER": "sshd",
        "_UID": "0", "_COMM": "sshd", "_EXE": "/usr/sbin/sshd",
        "MESSAGE": f"Accepted publickey for deploy from {source_ip} port 50221 ssh2",
    }


def _popen_stream(payload, commands):
    real_popen = subprocess.Popen

    def spawn(command, **kwargs):
        commands.append(command)
        script = "import sys; sys.stdout.buffer.write(" + repr(payload) + ")"
        return real_popen(
            [sys.executable, "-c", script], stdout=kwargs["stdout"],
            stderr=kwargs["stderr"], stdin=kwargs["stdin"], bufsize=kwargs["bufsize"],
        )

    return spawn


def _patch_journal_popen(journal_module, payload, commands):
    # Patch the collector's module reference, not subprocess.Popen on the
    # shared stdlib module: testit may run selected modules concurrently.
    proxy = types.SimpleNamespace(
        Popen=mock.Mock(side_effect=_popen_stream(payload, commands)),
        PIPE=subprocess.PIPE, DEVNULL=subprocess.DEVNULL,
        TimeoutExpired=subprocess.TimeoutExpired,
    )
    return mock.patch.object(journal_module, "subprocess", proxy)


@th.django_unit_test()
def test_journal_detector_keeps_logins_and_aggregates_failures(opts):
    from mojo.mojosec.detectors import detect_journal

    accepted = detect_journal({
        "SYSLOG_IDENTIFIER": "sshd",
        "_UID": "0", "_COMM": "sshd", "_EXE": "/usr/sbin/sshd",
        "MESSAGE": "Accepted publickey for deploy from 192.0.2.8 port 50221 ssh2",
        "__REALTIME_TIMESTAMP": "1786190400000000",
    })
    th.assert_eq(accepted["kind"], "auth.ssh_login",
                 "every accepted SSH login must become a host event")
    th.assert_eq(accepted["aggregate"], False,
                 "accepted SSH logins must never wait in a noise aggregation window")
    th.assert_eq(accepted["attributes"]["source_ip"], "192.0.2.8",
                 "the accepted-login source must be normalized for central policy")

    failed = detect_journal({
        "SYSLOG_IDENTIFIER": "sshd",
        "_UID": "0", "_COMM": "sshd", "_EXE": "/usr/sbin/sshd",
        "_SYSTEMD_UNIT": "sshd.service",
        "MESSAGE": "Failed password for invalid user admin from 198.51.100.9 port 43812 ssh2",
    })
    th.assert_eq(failed["kind"], "auth.ssh_failure",
                 "failed SSH authentication should be retained as a security signal")
    th.assert_eq(failed["aggregate"], True,
                 "repeated SSH failures should be aggregated before delivery")

    facility_only = detect_journal({
        "SYSLOG_FACILITY": "10", "_SYSTEMD_UNIT": "sshd.service",
        "_UID": "0", "_COMM": "sshd", "_EXE": "/usr/sbin/sshd",
        "MESSAGE": "Accepted publickey for deploy from 203.0.113.8 port 50221 ssh2",
    })
    th.assert_eq(facility_only["kind"], "auth.ssh_login",
                 "AL2023 facility-10 auth records must work without a stable systemd unit")

    sudo = detect_journal({
        "SYSLOG_IDENTIFIER": "sudo", "SYSLOG_FACILITY": "10",
        "_SYSTEMD_UNIT": "session-202.scope",
        "_UID": "0", "_COMM": "sudo", "_EXE": "/usr/bin/sudo",
        "MESSAGE": "deploy : TTY=pts/0 ; PWD=/opt/api ; USER=root ; COMMAND=/usr/bin/systemctl restart api",
    })
    th.assert_eq(sudo["kind"], "auth.sudo_command",
                 "sudo commands attached to transient AL2023 scopes must be retained")
    th.assert_eq(sudo["attributes"]["command"],
                 "/usr/bin/systemctl restart api",
                 "the protected sensor evidence must retain bounded command context")

    secret = "top-secret-password"
    sensitive_sudo = detect_journal({
        "SYSLOG_IDENTIFIER": "sudo", "SYSLOG_FACILITY": "10",
        "_UID": "0", "_COMM": "sudo", "_EXE": "/usr/bin/sudo",
        "MESSAGE": f"deploy : USER=root ; COMMAND=/usr/bin/curl --password {secret} https://example.invalid",
    })
    encoded = json.dumps(sensitive_sudo)
    th.assert_true(secret in encoded and "--password" in encoded,
                   "raw bounded command evidence must reach only the protected receipt layer")
    th.assert_eq(sensitive_sudo["attributes"]["command_path"], "/usr/bin/curl",
                 "sudo evidence should retain only the invoked executable path")

    for forged in (
            {"SYSLOG_IDENTIFIER": "sshd", "_UID": "1000", "_COMM": "python3",
             "MESSAGE": "Accepted publickey for deploy from 192.0.2.99 port 1 ssh2"},
            {"SYSLOG_IDENTIFIER": "sudo", "_UID": "1000", "_COMM": "python3",
             "MESSAGE": "deploy : TTY=pts/0 ; USER=root ; COMMAND=/usr/bin/id"}):
        th.assert_eq(detect_journal(forged), None,
                     "user-controlled journal identifiers/messages must not forge auth events")


@th.django_unit_test()
def test_live_al2023_auth_tuples_are_exactly_trusted(opts):
    from mojo.mojosec.detectors import detect_journal

    split = {
        "SYSLOG_IDENTIFIER": "attacker-controlled-and-irrelevant",
        "_UID": "0", "_COMM": "sshd-session",
        "_EXE": "/usr/libexec/openssh/sshd-session",
        "_SYSTEMD_UNIT": "sshd.service",
        "_BOOT_ID": "a" * 32, "_AUDIT_SESSION": "91", "_TTY": "pts/2",
        "MESSAGE": "Accepted publickey for deploy from 192.0.2.91 port 50221 ssh2",
    }
    login = detect_journal(split)
    th.assert_eq(login["kind"], "auth.ssh_login",
                 "the deployed split-OpenSSH tuple must produce a login event")
    th.assert_eq(login["attributes"]["source_ip"], "192.0.2.91",
                 "the accepted login must retain its validated remote address")

    for field, value in (
            ("_UID", "1000"), ("_COMM", "sshd"),
            ("_EXE", "/usr/sbin/sshd"), ("_SYSTEMD_UNIT", "session-91.scope")):
        mutated = dict(split, **{field: value})
        th.assert_eq(detect_journal(mutated), None,
                     f"mutating authoritative split-SSH field {field} must fail closed")

    sudo = {
        "SYSLOG_IDENTIFIER": "not-authoritative", "_UID": "1000",
        "_COMM": "sudo", "_EXE": "/usr/bin/sudo",
        "_SYSTEMD_UNIT": "session-202.scope", "_BOOT_ID": "a" * 32,
        "_AUDIT_SESSION": "91",
        "MESSAGE": "deploy : USER=root ; COMMAND=/usr/bin/id",
    }
    command = detect_journal(sudo)
    th.assert_eq(command["kind"], "auth.sudo_command",
                 "the deployed non-root invoking UID sudo tuple must be trusted")
    th.assert_true("tty" not in command["attributes"] and
                   "cwd" not in command["attributes"],
                   "missing sudo TTY and PWD must remain absent")
    for field, value in (
            ("_UID", "01000"), ("_COMM", "sudoedit"),
            ("_EXE", ""), ("_SYSTEMD_UNIT", "user@1000.service")):
        mutated = dict(sudo, **{field: value})
        th.assert_eq(detect_journal(mutated), None,
                     f"mutating authoritative sudo field {field} must fail closed")


@th.django_unit_test()
def test_systemd_user_session_open_is_local_rich_and_exact(opts):
    from mojo.mojosec.attribution import AttributionResolver
    from mojo.mojosec.detectors import detect_journal

    record = {
        "_UID": "0", "_PID": "4123", "_COMM": "(systemd)",
        "_EXE": "/usr/lib/systemd/systemd", "_SYSTEMD_UNIT": "user@80.service",
        "_BOOT_ID": "b" * 32, "_AUDIT_SESSION": "44", "_AUDIT_LOGINUID": "80",
        "MESSAGE": "pam_unix(systemd-user:session): session opened for user www(uid=80) by (uid=0)",
    }
    session = detect_journal(record, AttributionResolver([], [{
        "actor": "www", "tty": "pts/9", "source_ip": "198.51.100.80",
        "observed_at": 1786190400,
    }]))
    th.assert_eq((session["kind"], session["severity"], session["summary"]),
                 ("auth.session_open", "info", "PAM service session opened"),
                 "a trusted systemd-user open must be informational local PAM evidence")
    th.assert_eq(session["attributes"]["target_uid"], 80,
                 "the target UID must survive as structured evidence")
    th.assert_eq(session["attributes"]["producer_exe"], "/usr/lib/systemd/systemd",
                 "the trusted producer executable must survive as evidence")
    th.assert_true("source_ip" not in session["attributes"],
                   "generic PAM opens must never use the looser who fallback")

    for field, value in (
            ("_UID", "80"), ("_PID", "0"), ("_COMM", "systemd"),
            ("_EXE", "/tmp/systemd"), ("_SYSTEMD_UNIT", "user@81.service"),
            ("_AUDIT_LOGINUID", "81"), ("_BOOT_ID", "bad"),
            ("_AUDIT_SESSION", "4294967295"),
            ("MESSAGE", record["MESSAGE"].replace("by (uid=0)", "by (uid=1)"))):
        th.assert_eq(detect_journal(dict(record, **{field: value})), None,
                     f"mutating authoritative PAM field {field} must fail closed")


@th.django_unit_test()
def test_journal_collector_streams_forward_without_tail_skips(opts):
    import mojo.mojosec.collectors.journal as journal_module

    records = [_journal_record(f"cursor-{index}", f"192.0.2.{index}") for index in range(1, 4)]
    payload = b"".join(json.dumps(record).encode("utf-8") + b"\n" for record in records)
    commands = []
    collector = journal_module.JournalCollector(_journal_config(max_records=2))
    with _patch_journal_popen(journal_module, payload, commands):
        result = collector.poll("cursor-0")

    th.assert_eq(len(result["observations"]), 2,
                 "the record ceiling should return the first two records after the cursor")
    th.assert_eq(result["cursor"], "cursor-2",
                 "the durable cursor must stop at the last processed record, preserving the burst remainder")
    th.assert_true(not any(argument.startswith("--lines") for argument in commands[0]),
                   "journalctl tail semantics must never skip the beginning of a post-cursor burst")
    th.assert_in("--after-cursor=cursor-0", commands[0],
                 "journal collection must stream forward from the committed cursor")


@th.django_unit_test()
def test_journal_poison_record_is_counted_and_cursor_advances(opts):
    import mojo.mojosec.collectors.journal as journal_module

    records = [_journal_record("cursor-1", "192.0.2.1"),
               _journal_record("cursor-2", "192.0.2.2")]
    payload = b"".join(json.dumps(record).encode("utf-8") + b"\n" for record in records)
    commands = []
    collector = journal_module.JournalCollector(_journal_config())
    with _patch_journal_popen(journal_module, payload, commands), \
            mock.patch.object(journal_module, "detect_journal", side_effect=(ValueError("poison"), None)):
        result = collector.poll()

    th.assert_eq(result["malformed"], 1,
                 "a detector failure must be counted as one malformed journal record")
    th.assert_eq(result["cursor"], "cursor-2",
                 "a poison record must not prevent progress to the next valid journal cursor")


@th.django_unit_test()
def test_journal_byte_ceiling_keeps_last_fully_processed_cursor(opts):
    import mojo.mojosec.collectors.journal as journal_module

    records = [_journal_record("cursor-1", "192.0.2.1"),
               _journal_record("cursor-2", "192.0.2.2")]
    encoded = [json.dumps(record).encode("utf-8") + b"\n" for record in records]
    payload = b"".join(encoded)
    commands = []
    config = _journal_config(max_bytes=len(encoded[0]) + len(encoded[1]) // 2)
    collector = journal_module.JournalCollector(config)
    with _patch_journal_popen(journal_module, payload, commands):
        result = collector.poll()

    th.assert_eq(len(result["observations"]), 1,
                 "the byte ceiling should retain only the fully processed first record")
    th.assert_eq(result["cursor"], "cursor-1",
                 "a partial normal record must remain after the cursor for the next poll")


@th.django_unit_test()
def test_nginx_detector_is_behavioral_and_quiet(opts):
    from mojo.mojosec.detectors import detect_nginx

    noisy_bot = detect_nginx({
        "time_iso8601": "2026-08-08T12:00:00+00:00", "status": 404,
        "request_method": "GET", "uri": "/ordinary-missing-page",
        "remote_addr": "192.0.2.3", "user_agent": "GPTBot/1.0",
    })
    th.assert_eq(noisy_bot, None,
                 "a user-agent claim and routine 404 must not create sensor noise")

    probe = detect_nginx({
        "time_iso8601": "2026-08-08T12:00:00+00:00", "status": 404,
        "request_method": "GET", "request_uri": "/wp-login.php?redirect=secret",
        "remote_addr": "198.51.100.5", "realip_remote_addr": "10.0.0.10",
        "referer": "https://example.invalid/private?token=secret",
    })
    th.assert_eq(probe["kind"], "web.probe",
                 "a known exploit path should create a high-signal probe event")
    th.assert_eq(probe["recommendation"], "block_ip",
                 "known exploit probes should carry an advisory block recommendation")
    th.assert_eq(probe["attributes"]["path"], "/wp-login.php",
                 "query strings must never be copied into incident evidence")
    th.assert_eq(probe["attributes"]["referrer"],
                 "https://example.invalid/private?token=secret",
                 "bounded raw referrer evidence must be available for central scrubbing")

    server_error = detect_nginx({
        "status": 502, "method": "POST", "path": "/api/orders",
        "source_ip": "203.0.113.22", "request_time": "1.250",
    })
    th.assert_eq(server_error["kind"], "web.error",
                 "nginx 5xx responses must be retained for operational detection")

    first_token = "AbCdEfGhIjKlMnOpQrStUvWxYz123456"
    second_token = "ZyXwVuTsRqPoNmLkJiHgFeDcBa654321"
    first_secret_path = detect_nginx({
        "status": 500, "method": "GET", "path": f"/api/reset/{first_token}",
    })
    second_secret_path = detect_nginx({
        "status": 500, "method": "GET", "path": f"/api/reset/{second_token}",
    })
    th.assert_true(first_token in first_secret_path["attributes"]["request_uri"],
                   "bounded raw request evidence must remain available to the protected receipt")
    th.assert_true(first_token not in first_secret_path["attributes"]["path"],
                   "the aggregation path must hash high-entropy URL segments")
    th.assert_eq(first_secret_path["fingerprint"], second_secret_path["fingerprint"],
                 "different reset tokens must share one bounded aggregation key")

    from mojo.mojosec.detectors import DetectorError
    with th.assert_raises(DetectorError):
        detect_nginx({"status": 700, "method": "GET", "path": "/bad-status"})
    with th.assert_raises(DetectorError):
        detect_nginx({"status": 500, "method": "GET", "path": "/slow",
                      "request_time": "NaN"})


@th.django_unit_test()
def test_nginx_collector_resumes_at_a_durable_byte_cursor(opts):
    from mojo.mojosec.collectors.nginx import NginxCollector

    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "security.json.log")
        first = {"status": 500, "method": "GET", "path": "/one", "source_ip": "192.0.2.1"}
        second = {"status": 502, "method": "GET", "path": "/two", "source_ip": "192.0.2.2"}
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(first) + "\n")
        collector = NginxCollector({
            "paths": [path], "max_bytes_per_poll": 65536, "max_line_bytes": 16384,
        })
        initial = collector.poll()
        th.assert_eq(len(initial["observations"]), 1,
                     "the first complete structured nginx line should be collected")

        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(second) + "\n")
        resumed = collector.poll(initial["cursor"])
        th.assert_eq(len(resumed["observations"]), 1,
                     "resuming from the committed byte cursor must collect only new lines")
        th.assert_eq(resumed["observations"][0]["attributes"]["path"], "/two",
                     "the resumed nginx event should be the newly appended record")

        os.replace(path, path + ".1")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "status": 503, "method": "GET", "path": "/after-rotate",
            }) + "\n")
        rotated = collector.poll(resumed["cursor"])
        th.assert_eq(len(rotated["observations"]), 1,
                     "a normal nginx rename+USR1 rotation must resume on the new inode")
        th.assert_eq(rotated["observations"][0]["attributes"]["path"],
                     "/after-rotate",
                     "the first complete record after rotation must be collected")

        # logrotate copytruncate preserves the inode. Regrow beyond the old
        # offset before the next poll to exercise the case a size-only cursor
        # would mistake for an ordinary append and silently skip.
        same_inode = os.stat(path).st_ino
        after_truncate = "/after-copytruncate"
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "status": 504, "method": "GET", "path": after_truncate,
                "padding": "x" * 400,
            }) + "\n")
        th.assert_eq(os.stat(path).st_ino, same_inode,
                     "the regression must model copytruncate on the active inode")
        truncated = collector.poll(rotated["cursor"])
        th.assert_eq(len(truncated["observations"]), 1,
                     "copytruncate/regrow must reset the durable cursor without stalling")
        th.assert_eq(truncated["observations"][0]["attributes"]["path"],
                     after_truncate,
                     "the first complete record after copytruncate must be collected")


@th.django_unit_test()
def test_nginx_poison_numeric_is_counted_without_stalling_cursor(opts):
    from mojo.mojosec.collectors.nginx import NginxCollector

    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "security.json.log")
        poison = {"status": 500, "method": "GET", "path": "/poison", "request_time": "inf"}
        valid = {"status": 502, "method": "GET", "path": "/valid"}
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(poison) + "\n")
            handle.write(json.dumps(valid) + "\n")
        collector = NginxCollector({
            "paths": [path], "max_bytes_per_poll": 65536, "max_line_bytes": 16384,
        })
        result = collector.poll()
        th.assert_eq(result["malformed"], 1,
                     "a non-finite nginx numeric must be rejected and counted")
        th.assert_eq(len(result["observations"]), 1,
                     "a poison nginx record must not suppress the following valid event")
        th.assert_eq(result["cursor"][path]["offset"], os.path.getsize(path),
                     "the nginx cursor must advance past rejected records so they cannot stall collection")


@th.django_unit_test()
def test_nginx_rich_line_cap_accepts_header_limits_and_rejects_overflow(opts):
    from mojo.mojosec.collectors.nginx import MAX_STRUCTURED_LINE_BYTES, NginxCollector

    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "security.json.log")
        rich = {
            "status": 500, "method": "GET",
            "request_uri": "/wp-login.php?" + "q" * 8192,
            "referrer": "https://example.invalid/" + "r" * 8192,
            "user_agent": "Agent/1 " + "u" * 8192,
            "host": "example.invalid",
        }
        encoded = json.dumps(rich).encode() + b"\n"
        th.assert_true(len(encoded) > 16 * 1024,
                       "regression input must exceed the retired 16 KiB ceiling")
        with open(path, "wb") as handle:
            handle.write(encoded)
        collector = NginxCollector({
            "paths": [path], "max_bytes_per_poll": 2 * MAX_STRUCTURED_LINE_BYTES,
            "max_line_bytes": MAX_STRUCTURED_LINE_BYTES,
        })
        result = collector.poll()
        th.assert_eq(len(result["observations"]), 1,
                     "valid nginx-header-limit evidence must reach the per-kind byte builder")
        th.assert_true(result["observations"][0]["attributes"].get("request_uri_truncated"),
                       "the evidence builder, not ingress, must bound a rich request target")

        overflow = b'{"padding":"' + b"x" * MAX_STRUCTURED_LINE_BYTES + b'"}\n'
        valid = json.dumps({
            "status": 502, "method": "GET", "request_uri": "/after-overflow",
        }).encode() + b"\n"
        with open(path, "wb") as handle:
            handle.write(overflow + valid)
        result = collector.poll()
        th.assert_eq((result["malformed"], len(result["observations"])), (1, 1),
                     "derived-cap overflow must fail closed without wedging the next record")


@th.django_unit_test()
def test_fim_detects_changes_without_following_symlinks(opts):
    from mojo.mojosec.collectors.fim import FimCollector

    with tempfile.TemporaryDirectory() as root:
        root = os.path.realpath(root)
        watched = os.path.join(root, "watched")
        outside = os.path.join(root, "outside")
        os.mkdir(watched)
        os.mkdir(outside)
        config_path = os.path.join(watched, "settings.conf")
        outside_path = os.path.join(outside, "secret.txt")
        with open(config_path, "w", encoding="utf-8") as handle:
            handle.write("safe\n")
        with open(outside_path, "w", encoding="utf-8") as handle:
            handle.write("outside\n")
        link_path = os.path.join(watched, "outside-link")
        os.symlink(outside, link_path)

        collector = FimCollector({
            "targets": [{"path": watched, "recursive": True}],
            "max_entries": 100, "max_file_bytes": 1024 * 1024, "max_depth": 16,
        })
        initial = collector.scan()
        th.assert_eq(initial["snapshot"][link_path]["kind"], "symlink",
                     "FIM must record a symlink itself without traversing it")
        th.assert_true(outside_path not in initial["snapshot"],
                       "recursive FIM must never escape a target through a symlink")

        with open(config_path, "w", encoding="utf-8") as handle:
            handle.write("changed\n")
        changed = collector.scan()
        events = collector.diff(initial["snapshot"], changed)
        th.assert_eq(len(events), 1,
                     "one targeted file modification should produce one FIM event")
        th.assert_eq(events[0]["attributes"]["change"], "modified",
                     "the FIM event must identify a file modification")


@th.django_unit_test()
def test_fim_symlink_swap_fails_closed_and_pending_work_is_bounded(opts):
    import mojo.mojosec.collectors.fim as fim_module

    with tempfile.TemporaryDirectory() as root:
        root = os.path.realpath(root)
        watched = os.path.join(root, "watched")
        outside = os.path.join(root, "outside-secret")
        os.mkdir(watched)
        with open(outside, "w", encoding="utf-8") as handle:
            handle.write("must-not-be-hashed\n")
        victim = os.path.join(watched, "victim.conf")
        with open(victim, "w", encoding="utf-8") as handle:
            handle.write("initial\n")
        for index in range(10):
            with open(os.path.join(watched, f"bounded-{index}.conf"), "w", encoding="utf-8") as handle:
                handle.write(str(index))

        collector = fim_module.FimCollector({
            "targets": [{"path": watched, "recursive": True}],
            "max_entries": 100, "max_file_bytes": 1024 * 1024, "max_depth": 16,
        })
        real_open = fim_module.os.open
        swapped = {"done": False}

        def swap_before_open(name, flags, *args, **kwargs):
            if name == "victim.conf" and kwargs.get("dir_fd") is not None and not swapped["done"]:
                os.unlink(victim)
                os.symlink(outside, victim)
                swapped["done"] = True
            return real_open(name, flags, *args, **kwargs)

        with mock.patch.object(fim_module.os, "open", side_effect=swap_before_open):
            scan = collector.scan()

        th.assert_eq(scan["complete"], False,
                     "a file-to-symlink swap must make the FIM scan incomplete")
        th.assert_true(victim not in scan["snapshot"],
                       "a raced symlink target must never be hashed into the FIM baseline")

        bounded_collector = fim_module.FimCollector({
            "targets": [{"path": watched, "recursive": True}],
            "max_entries": 4, "max_file_bytes": 1024 * 1024, "max_depth": 16,
        })
        bounded = bounded_collector.scan()
        th.assert_true(len(bounded["snapshot"]) <= 4,
                       "descriptor traversal must keep discovered work within max_entries")

        with mock.patch.object(bounded_collector, "_descriptor_walk_supported", return_value=False):
            unsupported = bounded_collector.scan()
        th.assert_eq(unsupported["complete"], False,
                     "platforms without descriptor-relative no-follow traversal must fail closed")
        th.assert_eq(unsupported["snapshot"], {},
                     "the fail-closed platform path must not use an unsafe pathname fallback")


@th.django_unit_test()
def test_standard_profile_is_immutable_bounded_and_covers_system_python(opts):
    from mojo.mojosec.config import build_config
    from mojo.mojosec.profiles import PRIVATE_TREES, resolve_profile

    profile = resolve_profile("al2023-web-v2")
    th.assert_eq(len(profile["digest"]), 64,
                 "the immutable profile must carry one deterministic SHA-256 identity")
    th.assert_eq(profile["tiers"]["fast"]["interval_seconds"], 60,
                 "the standard persistence graph must run every minute")
    th.assert_eq(profile["tiers"]["slow"]["interval_seconds"], 21600,
                 "the binary integrity graph must run every six hours")
    fast_paths = [item["path"] for item in profile["tiers"]["fast"]["targets"]]
    th.assert_eq(fast_paths.count("/usr/local/lib"), 1,
                 "the root-pip system site tree must have exactly one traversal owner")
    etc = next(item for item in profile["tiers"]["fast"]["targets"]
               if item["path"] == "/etc")
    th.assert_true("mojosec/**" in etc["exclude"],
                   "the expanded broad /etc graph must subtract MojoSec private state")
    th.assert_true(all(not any(path.startswith(private + "/") or path == private
                               for private in PRIVATE_TREES)
                       for path in fast_paths),
                   "no final submitted target may directly enter private MojoSec state")

    config = build_config({
        "sensor_id": "profile-test",
        "endpoint": "https://example.invalid/api/incident/mojosec/batch",
        "profile": "al2023-web-v2",
    })
    th.assert_eq(set(config["collectors"]["fim"]["tiers"]), {"fast", "slow"},
                 "selecting the profile name must resolve the packaged graph")
    th.assert_true(config["collectors"]["rpm"]["enabled"],
                   "the standard profile must monitor the imported system Python environment")


@th.django_unit_test()
def test_al2023_v2_uses_canonical_cloud_instance_tree_without_following_alias(opts):
    from mojo.mojosec.collectors.fim import FimCollector
    from mojo.mojosec.profiles import resolve_profile

    v1 = resolve_profile("al2023-web-v1")
    v2 = resolve_profile("al2023-web-v2")
    th.assert_eq(v1["digest"],
                 "6a45b233ae8dd87e77456d88a037ea435e6e9240d07884814fae4a23d30f3e0e",
                 "the retained v1 profile must keep its published immutable identity")
    th.assert_eq(v2["digest"],
                 "1cb26a911e1a2c2418d5a6428c873bc4118a39c8731c4d8442d7c4f7f3cbd14f",
                 "the v2 profile graph must keep its published immutable identity")
    v1_paths = [target["path"] for target in v1["tiers"]["fast"]["targets"]]
    v2_paths = [target["path"] for target in v2["tiers"]["fast"]["targets"]]
    th.assert_true("/var/lib/cloud/instance/scripts" in v1_paths,
                   "v1 must remain byte-stable for retained baselines and rollback")
    th.assert_true("/var/lib/cloud/instance/scripts" not in v2_paths,
                   "v2 must remove the descendant of AL2023's mutable instance alias")
    th.assert_true("/var/lib/cloud/instances" in v2_paths,
                   "v2 must retain canonical recursive cloud-init instance coverage")

    with tempfile.TemporaryDirectory() as root:
        root = os.path.realpath(root)
        cloud = os.path.join(root, "cloud")
        instances = os.path.join(cloud, "instances")
        instance = os.path.join(cloud, "instance")
        scripts = os.path.join(instances, "i-test", "scripts")
        os.makedirs(scripts)
        script = os.path.join(scripts, "part-001")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\necho ready\n")
        os.symlink(os.path.join("instances", "i-test"), instance)

        alias_scan = FimCollector({
            "targets": [{"path": os.path.join(instance, "scripts"), "recursive": True}],
            "max_entries": 100, "max_file_bytes": 1024 * 1024, "max_depth": 16,
        }).scan()
        th.assert_eq(alias_scan["complete"], False,
                     "descriptor-safe traversal must fail closed at the instance symlink")
        th.assert_true(script not in alias_scan["snapshot"],
                       "a symlink-descendant target must never be followed into a baseline")

        canonical_scan = FimCollector({
            "targets": [{"path": instances, "recursive": True}],
            "max_entries": 100, "max_file_bytes": 1024 * 1024, "max_depth": 16,
        }).scan()
        th.assert_eq(canonical_scan["complete"], True,
                     "the canonical cloud-init instances tree must scan completely")
        th.assert_true(script in canonical_scan["snapshot"],
                       "canonical recursive coverage must retain cloud-init script bytes")


@th.django_unit_test()
def test_fim_reuses_strict_metadata_and_retains_alias_anomalies(opts):
    from mojo.mojosec.collectors.fim import FimCollector

    with tempfile.TemporaryDirectory() as root:
        root = os.path.realpath(root)
        watched = os.path.join(root, "site-packages")
        outside = os.path.join(root, "outside")
        os.mkdir(watched)
        os.mkdir(outside)
        module = os.path.join(watched, "module.py")
        alias = os.path.join(watched, "module-alias.py")
        pth = os.path.join(watched, "startup.pth")
        escape = os.path.join(watched, "escape")
        with open(module, "w", encoding="utf-8") as handle:
            handle.write("VALUE = 1\n")
        os.link(module, alias)
        with open(pth, "w", encoding="utf-8") as handle:
            handle.write("import attacker_startup\n")
        os.symlink(outside, escape)
        collector = FimCollector({
            "targets": [{"path": watched, "recursive": True}],
            "max_entries": 100, "max_file_bytes": 1024 * 1024, "max_depth": 16,
        })
        first = collector.scan()
        th.assert_eq(first["snapshot"][alias]["sha256"], first["snapshot"][module]["sha256"],
                     "hardlink aliases must retain logical observations while sharing content")
        th.assert_true(all("digest_reused" not in entry
                           for entry in first["snapshot"].values()),
                       "digest reuse is transient scan state, never baseline evidence")
        th.assert_eq(first["snapshot"][pth]["anomaly"], "pth_executable",
                     "an executable .pth directive must be explicit integrity evidence")
        th.assert_eq(first["snapshot"][escape]["anomaly"], "symlink_escape",
                     "a symlink escaping the approved site root must be an anomaly")

        second = collector.scan(first["snapshot"])
        th.assert_eq(collector.diff(first["snapshot"], second), [],
                     "an unchanged second scan, including hardlinks, must diff empty")
        th.assert_eq(second["snapshot"], first["snapshot"],
                     "digest reuse must not alter persisted comparison state")


@th.django_unit_test()
def test_rpm_parser_is_strict_and_system_site_roots_are_constrained(opts):
    from mojo.mojosec.collectors.rpm import RpmCollector, RpmError, parse_verify_output

    parsed = parse_verify_output(
        "S.5....T.  c /usr/lib/python3.11/site-packages/mojo/__init__.py\n",
        ["/usr/lib/python3.11/site-packages"],
    )
    th.assert_eq(parsed["/usr/lib/python3.11/site-packages/mojo/__init__.py"]["status"],
                 "S.5....T.",
                 "all nine documented RPM verification columns must be retained")
    with th.assert_raises(RpmError):
        parse_verify_output(
            "X........ /usr/lib/python3.11/site-packages/mojo/__init__.py\n",
            ["/usr/lib/python3.11/site-packages"],
        )

    calls = []

    def runner(argv, accepted=(0,)):
        calls.append(argv)
        return 0, json.dumps([
            "/usr/lib/python3.11/site-packages",
            "/usr/lib64/python3.11/site-packages",
            "/usr/local/lib/python3.11/site-packages",
            "/opt/api/.venv/lib/python3.11/site-packages",
            "/tmp/attacker/site-packages",
        ]), ""

    collector = RpmCollector({
        "interpreter": "/usr/bin/python3", "interval_seconds": 21600,
        "max_entries": 100, "max_packages": 10, "max_owner_queries": 100,
        "max_output_bytes": 65536, "timeout_seconds": 5,
        "max_file_bytes": 1024, "max_depth": 16,
    }, {"name": "al2023-web-v1", "version": 1, "digest": "a" * 64}, runner=runner)
    roots = collector.discover_site_roots()
    th.assert_eq(roots, [
        "/usr/lib/python3.11/site-packages",
        "/usr/lib64/python3.11/site-packages",
        "/usr/local/lib/python3.11/site-packages",
    ], "system interpreter discovery must reject project and temporary environments")
    th.assert_eq(calls[0][:2], ["/usr/bin/python3", "-I"],
                 "site discovery must use the trusted isolated system interpreter")


@th.django_unit_test()
def test_rpm_ownership_is_rebuilt_for_every_slow_scan(opts):
    from mojo.mojosec.collectors.rpm import RpmCollector

    root = "/usr/local/lib/python3.11/site-packages"
    path = root + "/example.py"
    owners = iter(("example-1-1.x86_64", "example-2-1.x86_64"))
    calls = []

    def runner(argv, accepted=(0,)):
        calls.append(argv)
        return 0, next(owners) + "\n", ""

    collector = RpmCollector({
        "interpreter": "/usr/bin/python3", "interval_seconds": 21600,
        "max_entries": 100, "max_packages": 10, "max_owner_queries": 100,
        "max_output_bytes": 65536, "timeout_seconds": 5,
        "max_file_bytes": 1024, "max_depth": 16,
    }, {"name": "al2023-web-v1", "version": 1, "digest": "a" * 64}, runner=runner)
    shared = {
        path: {
            "kind": "file", "mode": 0o644, "uid": 0, "gid": 0,
            "size": 4, "sha256": "b" * 64,
        },
    }
    with mock.patch.object(collector, "discover_site_roots", return_value=[root]), \
            mock.patch.object(collector, "_verify_packages", return_value={}):
        first = collector.scan(shared_snapshot=shared)
        second = collector.scan(shared_snapshot=shared)

    th.assert_eq(first["snapshot"][path]["rpm_owner"], "example-1-1.x86_64",
                 "the first slow scan must record its current RPM owner")
    th.assert_eq(second["snapshot"][path]["rpm_owner"], "example-2-1.x86_64",
                 "the next slow scan must query ownership again after package changes")
    th.assert_eq(len(calls), 2,
                 "RPM ownership cache and its query budget must be scan-local")


@th.django_unit_test()
def test_fim_paths_with_undecodable_bytes_stay_deliverable(opts):
    from mojo.mojosec.collectors.fim import FimCollector
    from mojo.mojosec.events import bounded_text
    from mojo.mojosec.protocol import make_event, validate_event

    collector = FimCollector({"targets": []})
    bad_path = b"/etc/bad-\xff".decode("utf-8", "surrogateescape")
    sibling_path = b"/etc/bad-\xfe".decode("utf-8", "surrogateescape")

    def one_change(path):
        scan = {"snapshot": {path: {"kind": "file", "size": 1}}, "complete": True}
        found = collector.diff({}, scan)
        th.assert_eq(len(found), 1,
                     "one created path must yield exactly one FIM change")
        return found[0]

    first = one_change(bad_path)
    second = one_change(sibling_path)
    try:
        first["attributes"]["path"].encode("utf-8")
    except UnicodeEncodeError:
        th.assert_true(False,
                       "a surrogateescape filesystem path must be normalized into "
                       "storable evidence, not shipped raw")
    event = make_event("sensor-test", first)
    validate_event(event)
    th.assert_true(first["fingerprint"] != second["fingerprint"],
                   "paths differing only in invalid bytes must keep distinct event "
                   "identities via the raw-path fingerprint")

    normalized = bounded_text("summary \udcff tail", 64)
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError:
        th.assert_true(False,
                       "bounded_text must normalize lone surrogates so every summary "
                       "is storable by construction")


@th.django_unit_test()
def test_system_service_error_requires_pid1_failure_and_collapses_by_unit(opts):
    from mojo.mojosec.detectors import detect_journal
    from mojo.mojosec.events import fingerprint

    kex_noise = (
        "error: kex_exchange_identification: Connection closed by remote host",
        "error: kex_exchange_identification: read: Connection reset by peer",
        'error: kex_exchange_identification: client sent invalid protocol '
        'identifier "GET / HTTP/1.1"',
    )
    for message in kex_noise:
        record = {
            "SYSLOG_IDENTIFIER": "sshd", "_UID": "0", "_COMM": "sshd",
            "_EXE": "/usr/sbin/sshd", "_SYSTEMD_UNIT": "sshd.service",
            "PRIORITY": "3", "MESSAGE": message,
        }
        th.assert_eq(detect_journal(record), None,
                     "routine err-level daemon lines must not become service failures")

    failure = {
        "_PID": "1", "_UID": "0", "_COMM": "systemd",
        "SYSLOG_IDENTIFIER": "systemd", "_SYSTEMD_UNIT": "init.scope",
        "PRIORITY": "4", "MESSAGE": "api.service: Failed with result 'exit-code'.",
        "__REALTIME_TIMESTAMP": "1786190400000000",
    }
    fired = detect_journal(failure)
    th.assert_eq(fired["kind"], "system.service_error",
                 "a PID 1 unit-failure declaration must fire a service failure")
    th.assert_eq(fired["severity"], "high",
                 "genuine unit failures must stay high severity")
    th.assert_eq(fired["aggregate"], True,
                 "unit failures must aggregate so restart loops collapse")
    th.assert_eq(fired["attributes"]["unit"], "api.service",
                 "the failed unit must be named, not systemd's init.scope bookkeeping unit")
    th.assert_eq(fired["attributes"]["failure_kind"], "exit-code",
                 "the systemd result vocabulary must reach evidence as failure_kind")
    th.assert_true("Failed with result" in fired["attributes"].get("message", ""),
                   "the bounded failure message must stay in evidence")
    repeat = detect_journal(dict(failure, __REALTIME_TIMESTAMP="1786190460000000"))
    th.assert_eq(fired["fingerprint"], repeat["fingerprint"],
                 "repeats of one unit failure must share one aggregation key")
    th.assert_eq(fired["fingerprint"],
                 fingerprint("system.service_error", ("api.service", "exit-code")),
                 "the fingerprint must be (unit, failure_kind) with no volatile message text")

    entered = detect_journal(dict(
        failure, MESSAGE="api.service: Unit entered failed state."))
    th.assert_eq((entered["kind"], entered["attributes"]["failure_kind"]),
                 ("system.service_error", "failed"),
                 "the mid-era entered-failed-state wording must fire with kind 'failed'")
    legacy = detect_journal(dict(
        failure, MESSAGE="Unit api.service entered failed state."))
    th.assert_eq(legacy["attributes"]["unit"], "api.service",
                 "the pre-230 legacy failure wording must still name the unit")
    scope_unit = detect_journal(dict(
        failure, MESSAGE="session-42.scope: Failed with result 'timeout'."))
    th.assert_eq((scope_unit["attributes"]["unit"], scope_unit["attributes"]["failure_kind"]),
                 ("session-42.scope", "timeout"),
                 "non-service unit types must be accepted by the failure grammar")
    drifted = detect_journal(dict(
        failure, MESSAGE="api.service has gone belly-up.",
        MESSAGE_ID="d9b373ed55a64feb8242e02dbe79a49c", UNIT="api.service"))
    th.assert_eq((drifted["attributes"]["unit"], drifted["attributes"]["failure_kind"]),
                 ("api.service", "unknown"),
                 "the unit-failed MESSAGE_ID fallback must survive future wording drift")

    for mutation in (
            dict(failure, _PID="200"),
            dict(failure, _UID="1000"),
            dict(failure, MESSAGE="api.service: Main process exited, "
                                  "code=exited, status=1/FAILURE"),
            dict(failure, MESSAGE="Failed to start LSB: api daemon."),
            dict(failure, _PID="4242", _COMM="myapp",
                 SYSLOG_IDENTIFIER="myapp", _SYSTEMD_UNIT="myapp.service",
                 MESSAGE="api.service: Failed with result 'exit-code'."),
            dict(failure, MESSAGE="api.service has gone belly-up.",
                 MESSAGE_ID="d9b373ed55a64feb8242e02dbe79a49c", UNIT="../etc"),
            dict(failure, MESSAGE="api.service has gone belly-up.",
                 MESSAGE_ID="d9b373ed55a64feb8242e02dbe79a49c"),
    ):
        th.assert_eq(detect_journal(mutation), None,
                     "anything but a PID 1 root unit-failure declaration must fail closed")


@th.django_unit_test()
def test_system_oom_is_kernel_transport_only_and_single_fire(opts):
    from mojo.mojosec.detectors import detect_journal

    kill_line = (
        "Out of memory: Killed process 21437 (gunicorn) total-vm:1482912kB, "
        "anon-rss:1201234kB, file-rss:0kB, shmem-rss:0kB, UID:1001 "
        "pgtables:2891kB oom_score_adj:0"
    )
    genuine = detect_journal({
        "SYSLOG_IDENTIFIER": "kernel", "_TRANSPORT": "kernel",
        "PRIORITY": "3", "MESSAGE": kill_line,
    })
    th.assert_eq(genuine["kind"], "system.oom",
                 "a kernel-transport OOM kill line must fire the critical OOM event")
    th.assert_eq(genuine["severity"], "critical",
                 "kernel OOM kills must stay critical")
    th.assert_eq(genuine["aggregate"], False,
                 "a kernel OOM kill is a discrete incident, never aggregated away")
    th.assert_eq(genuine["attributes"]["unit"], "kernel",
                 "kernel-origin OOM evidence must attribute to the kernel")
    memcg = detect_journal({
        "SYSLOG_IDENTIFIER": "kernel", "_TRANSPORT": "kernel", "PRIORITY": "3",
        "MESSAGE": "Memory cgroup out of memory: Killed process 991 (api) "
                   "total-vm:100kB, anon-rss:90kB",
    })
    th.assert_eq(memcg["kind"], "system.oom",
                 "cgroup OOM kill lines must fire like global kills")

    app_text = {
        "SYSLOG_IDENTIFIER": "myapp", "_SYSTEMD_UNIT": "myapp.service",
        "_TRANSPORT": "journal", "PRIORITY": "6",
        "MESSAGE": "watchdog: killed process tree for job 7",
    }
    th.assert_eq(detect_journal(app_text), None,
                 "application text echoing OOM phrases must never fire a critical event")
    th.assert_eq(detect_journal(dict(app_text, PRIORITY="3")), None,
                 "the err-level catch-all is gone: app text stays silent at priority 3")
    forged = detect_journal({
        "SYSLOG_IDENTIFIER": "kernel", "_TRANSPORT": "syslog",
        "PRIORITY": "3", "MESSAGE": kill_line,
    })
    th.assert_eq(forged, None,
                 "a forged kernel identifier without kernel transport must fail closed")

    for detail_line in (
            "oom-kill:constraint=CONSTRAINT_NONE,nodemask=(null),cpuset=/,"
            "mems_allowed=0,task=gunicorn,pid=21437,uid=1001",
            "python3 invoked oom-killer: gfp_mask=0x140cca(GFP_HIGHUSER_MOVABLE"
            "|__GFP_COMP), order=0, oom_score_adj=0",
    ):
        record = {"SYSLOG_IDENTIFIER": "kernel", "_TRANSPORT": "kernel",
                  "PRIORITY": "3", "MESSAGE": detail_line}
        th.assert_eq(detect_journal(record), None,
                     "one kernel OOM kill must fire exactly one event, not one per log line")

    routed = detect_journal({
        "_PID": "1", "_UID": "0", "_COMM": "systemd",
        "SYSLOG_IDENTIFIER": "systemd", "_SYSTEMD_UNIT": "init.scope",
        "PRIORITY": "4", "MESSAGE": "api.service: Failed with result 'oom-kill'.",
    })
    th.assert_eq((routed["kind"], routed["attributes"]["failure_kind"]),
                 ("system.service_error", "oom-kill"),
                 "PID 1's oom-kill unit failure must route to service_error, not system.oom")


@th.django_unit_test()
def test_incomplete_fim_scan_emits_only_overflow_until_reconciled(opts):
    from mojo.mojosec.collectors.fim import FimCollector

    with tempfile.TemporaryDirectory() as root:
        root = os.path.realpath(root)
        watched = os.path.join(root, "watched")
        os.mkdir(watched)
        for index in range(10):
            with open(os.path.join(watched, f"file-{index}.conf"), "w", encoding="utf-8") as handle:
                handle.write(f"content {index}\n")

        bounded = FimCollector({
            "targets": [{"path": watched, "recursive": True}],
            "max_entries": 4, "max_file_bytes": 1024 * 1024, "max_depth": 16,
        })
        partial = bounded.scan()
        th.assert_eq(partial["complete"], False,
                     "a scan capped below the tree size must report itself incomplete")
        th.assert_true(len(partial["snapshot"]) > 0,
                       "the capped scan must still carry a non-empty partial snapshot")

        observations = bounded.diff({}, partial)
        th.assert_eq([item["kind"] for item in observations], ["fim.overflow"],
                     "an incomplete scan must yield exactly one overflow and zero change events")
        th.assert_eq(observations[0]["severity"], "critical",
                     "the incomplete-scan overflow signal must stay critical")
        th.assert_eq(observations[0]["attributes"]["entries"], len(partial["snapshot"]),
                     "the overflow evidence must report the partial snapshot size")

        for _ in range(3):
            repeat = bounded.diff({}, partial)
            th.assert_eq([item["kind"] for item in repeat], ["fim.overflow"],
                         "every interval of a persistently incomplete scan must emit only the overflow")

        manifest_path = os.path.join(root, "expected.json")
        with open(manifest_path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with_manifest = FimCollector({
            "targets": [{"path": watched, "recursive": True}],
            "max_entries": 4, "max_file_bytes": 1024 * 1024, "max_depth": 16,
        }, expected_changes_path=manifest_path)
        noisy = with_manifest.scan()
        th.assert_eq(noisy["complete"], False,
                     "the manifest-bearing collector must also overflow at the same cap")
        th.assert_eq([item["kind"] for item in with_manifest.diff({}, noisy)], ["fim.overflow"],
                     "a malformed manifest must stay silent while the scan is incomplete")

        adequate = FimCollector({
            "targets": [{"path": watched, "recursive": True}],
            "max_entries": 100, "max_file_bytes": 1024 * 1024, "max_depth": 16,
        })
        baseline_scan = adequate.scan()
        th.assert_eq(baseline_scan["complete"], True,
                     "an adequately bounded scan over the fixture must complete")
        baseline = baseline_scan["snapshot"]

        modified_path = os.path.join(watched, "file-0.conf")
        deleted_path = os.path.join(watched, "file-1.conf")
        with open(modified_path, "w", encoding="utf-8") as handle:
            handle.write("changed content\n")
        os.remove(deleted_path)

        rescan = adequate.scan()
        th.assert_eq(rescan["complete"], True,
                     "the reconcile fixture scan must complete")
        fake_incomplete = dict(rescan)
        fake_incomplete["complete"] = False
        th.assert_eq([item["kind"] for item in adequate.diff(baseline, fake_incomplete)],
                     ["fim.overflow"],
                     "real pending changes must stay withheld while a scan is incomplete")

        reconciled = adequate.diff(baseline, rescan)
        th.assert_true(all(item["kind"] == "fim.change" for item in reconciled),
                       "a complete reconcile scan must emit only change events, no overflow")
        changes = {item["attributes"]["path"]: item["attributes"]["change"]
                   for item in reconciled}
        th.assert_eq(changes.get(modified_path), "modified",
                     "the first complete scan must report the modification deferred while incomplete")
        th.assert_eq(changes.get(deleted_path), "deleted",
                     "the first complete scan must report the deletion deferred while incomplete")
        th.assert_true("created" not in changes.values(),
                       "a reconcile against the retained baseline must not invent created paths")
