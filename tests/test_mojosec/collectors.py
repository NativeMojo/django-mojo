import json
import os
import subprocess
import sys
import tempfile
from unittest import mock

from testit import helpers as th


def _journal_config(max_records=10, max_bytes=65536):
    return {
        "max_records": max_records, "max_bytes_per_poll": max_bytes,
        "max_record_bytes": 16384, "timeout_seconds": 5, "lookback_seconds": 300,
    }


def _journal_record(cursor, source_ip):
    return {
        "__CURSOR": cursor, "SYSLOG_IDENTIFIER": "sshd",
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


@th.django_unit_test()
def test_journal_detector_keeps_logins_and_aggregates_failures(opts):
    from mojo.mojosec.detectors import detect_journal

    accepted = detect_journal({
        "SYSLOG_IDENTIFIER": "sshd",
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
        "_SYSTEMD_UNIT": "session-187.scope",
        "MESSAGE": "Failed password for invalid user admin from 198.51.100.9 port 43812 ssh2",
    })
    th.assert_eq(failed["kind"], "auth.ssh_failure",
                 "failed SSH authentication should be retained as a security signal")
    th.assert_eq(failed["aggregate"], True,
                 "repeated SSH failures should be aggregated before delivery")

    facility_only = detect_journal({
        "SYSLOG_FACILITY": "10", "_SYSTEMD_UNIT": "session-201.scope",
        "MESSAGE": "Accepted publickey for deploy from 203.0.113.8 port 50221 ssh2",
    })
    th.assert_eq(facility_only["kind"], "auth.ssh_login",
                 "AL2023 facility-10 auth records must work without a stable systemd unit")

    sudo = detect_journal({
        "SYSLOG_IDENTIFIER": "sudo", "SYSLOG_FACILITY": "10",
        "_SYSTEMD_UNIT": "session-202.scope",
        "MESSAGE": "deploy : TTY=pts/0 ; PWD=/opt/api ; USER=root ; COMMAND=/usr/bin/systemctl restart api",
    })
    th.assert_eq(sudo["kind"], "auth.sudo_command",
                 "sudo commands attached to transient AL2023 scopes must be retained")
    th.assert_true("command" not in sudo["attributes"],
                   "sudo command arguments must never be persisted as incident evidence")

    secret = "top-secret-password"
    sensitive_sudo = detect_journal({
        "SYSLOG_IDENTIFIER": "sudo", "SYSLOG_FACILITY": "10",
        "MESSAGE": f"deploy : USER=root ; COMMAND=/usr/bin/curl --password {secret} https://example.invalid",
    })
    encoded = json.dumps(sensitive_sudo)
    th.assert_true(secret not in encoded and "--password" not in encoded,
                   "sudo arguments and inline secrets must be replaced by an executable and digest")
    th.assert_eq(sensitive_sudo["attributes"]["command_path"], "/usr/bin/curl",
                 "sudo evidence should retain only the invoked executable path")


@th.django_unit_test()
def test_journal_collector_streams_forward_without_tail_skips(opts):
    import mojo.mojosec.collectors.journal as journal_module

    records = [_journal_record(f"cursor-{index}", f"192.0.2.{index}") for index in range(1, 4)]
    payload = b"".join(json.dumps(record).encode("utf-8") + b"\n" for record in records)
    commands = []
    collector = journal_module.JournalCollector(_journal_config(max_records=2))
    with mock.patch.object(
            journal_module.subprocess, "Popen", side_effect=_popen_stream(payload, commands)):
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
    with mock.patch.object(
            journal_module.subprocess, "Popen", side_effect=_popen_stream(payload, commands)), \
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
    with mock.patch.object(
            journal_module.subprocess, "Popen", side_effect=_popen_stream(payload, commands)):
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
    th.assert_true("referer" not in probe["attributes"],
                   "client-controlled referrers must never enter MojoSec evidence")

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
    th.assert_true(first_token not in json.dumps(first_secret_path),
                   "high-entropy URL segments must be hashed before persistence")
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
