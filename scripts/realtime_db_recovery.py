#!/usr/bin/env python
"""End-to-end realtime database recovery and latency check (#5736 T5/T6, #5750 T3).

Runs against this checkout's local test server (``bin/asgi_local``), which it
restarts. It never touches another database or server.

    uv run python scripts/realtime_db_recovery.py --mode default
    uv run python scripts/realtime_db_recovery.py --mode pool --sockets 200

What it does:
1. Restarts the server, with ``DATABASE_POOL_OPTIONS`` in ``var/django.conf``
   for ``--mode pool``; the file is restored afterwards.
2. Opens ``--sockets`` sockets across several users, ``--concurrency`` at a
   time. Each logs in, subscribes to its user topic and pings. Login and
   subscribe latency are recorded.
3. With those sockets open, times one more login on its own, and the delivery
   of one topic message to every open socket (publish to receipt).
4. Terminates every database backend the server holds, as a failover does
   (skipped with ``--no-kill``, the latency control).
5. Checks that the open sockets still receive a published message, and that a
   second batch of sockets logs in and subscribes.
6. Closes everything. Checks that the disconnect hook saved for every user,
   that the server log gained no "the connection is closed" errors, and that
   the server's database connections return to the baseline (default mode) or
   stay within the pool size (pool mode).

Before #5750 each authenticated socket polled pub/sub on a default-executor
thread for up to a second at a time, so login latency grew with open sockets;
compare builds at the same ``--sockets`` and ``--concurrency``.

Prints one JSON summary and exits non-zero when a recovery check fails. The
summary's ``latency_targets`` compares against #5750's release bar (login p95
under 1 s, subscribe p95 under 100 ms, delivery p95 under 250 ms, a lone login
under 100 ms) but does not change the exit code. Latency figures are for
comparison between two builds on one machine only.
"""

import argparse
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TESTPROJECT = REPO / "testproject"
CONF = TESTPROJECT / "var" / "django.conf"
POOL_LINE = 'DATABASE_POOL_OPTIONS={"min_size": 2, "max_size": 20, "timeout": 10}'
LOG = TESTPROJECT / "var" / "logs" / "realtime.log"
USERS = 40  # WS_MAX_CONNECTIONS defaults to 10 per identity

sys.path.insert(0, str(REPO))
sys.path.insert(0, str(TESTPROJECT / "config"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")


def server(action):
    subprocess.run([str(REPO / "bin" / "asgi_local"), action], cwd=REPO,
                   check=True, stdout=subprocess.DEVNULL)


def server_url():
    conf = dict(line.split("=", 1) for line in
                (TESTPROJECT / "config" / "dev_server.conf").read_text().split() if "=" in line)
    return f"ws://{conf.get('host', '127.0.0.1')}:{conf.get('port', '5555')}/ws/realtime/"


def server_backends():
    """Backends on this checkout's database, excluding this script's own."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pid FROM pg_stat_activity WHERE datname = current_database() "
            "AND pid <> pg_backend_pid() AND backend_type = 'client backend'")
        return [row[0] for row in cursor.fetchall() if row[0] not in OWN_PIDS]


OWN_PIDS = set()
TEST_DATABASE_PREFIX = "mojo_test"


def require_test_database():
    """Refuse to run against any database not named like a test database."""
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database()")
        name = cursor.fetchone()[0]
    if not name.startswith(TEST_DATABASE_PREFIX):
        raise SystemExit(f"refusing: database {name!r} does not start with {TEST_DATABASE_PREFIX!r}")


def kill_server_backends():
    from django.db import connection

    require_test_database()
    pids = server_backends()
    with connection.cursor() as cursor:
        for pid in pids:
            cursor.execute("SELECT pg_terminate_backend(%s)", [pid])
    return pids


def make_users():
    from mojo.apps.account.models import User
    from mojo.apps.account.utils.jwtoken import JWToken

    tag = uuid.uuid4().hex[:8]
    users = []
    for i in range(USERS):
        name = f"rt5736_e2e_{tag}_{i}"
        user = User(username=name, display_name=name, email=f"{name}@example.com",
                    is_email_verified=True)
        user.save()
        token = JWToken(user.get_auth_key()).create(uid=user.id).access_token
        users.append((user, token))
    return users


class Sampler(threading.Thread):
    """Samples the server's database connection count every 100ms."""

    def __init__(self):
        super().__init__(daemon=True)
        self.peak = 0
        self.running = True

    def run(self):
        from django.db import connection

        while self.running:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    OWN_PIDS.add(cursor.fetchone()[0])
                self.peak = max(self.peak, len(server_backends()))
            except Exception:
                connection.close()
            time.sleep(0.1)
        connection.close()


def open_socket(url, user, token):
    from testit.ws_client import WsClient

    ws = WsClient(url)
    ws.connect(timeout=15.0)
    ws.wait_for_type("auth_required", timeout=10.0)
    start = time.perf_counter()
    auth = ws.authenticate(token, timeout=20.0)
    login = time.perf_counter() - start
    topic = f"user:{user.id}"
    start = time.perf_counter()
    sub = ws.subscribe(topic, timeout=20.0)
    subscribe = time.perf_counter() - start
    if sub.get("type") != "subscribed":
        raise RuntimeError(f"subscribe failed: {sub}")
    ws.ping(timeout=10.0)
    return ws, auth, login, subscribe


def open_batch(url, users, count, concurrency):
    results, errors = [], []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        owners = [users[i % len(users)][0] for i in range(count)]
        futures = [pool.submit(open_socket, url, *users[i % len(users)]) for i in range(count)]
        for owner, future in zip(owners, futures):
            try:
                results.append(future.result() + (owner,))
            except Exception as exc:
                errors.append(str(exc)[:200])
    return results, errors


def measure_delivery(first, users):
    """Publish one message per user topic; time publish-to-receipt per socket."""
    from mojo.apps import realtime

    marker = uuid.uuid4().hex
    published = {}

    def wait(entry):
        ws, _auth, _login, _sub, user = entry
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            msg = ws.wait_for_type("message", timeout=max(0.1, deadline - time.monotonic()))
            if msg.data.get("data", {}).get("delivery") == marker:
                return time.perf_counter() - published[user.id]
        raise TimeoutError("delivery probe not received")

    with ThreadPoolExecutor(max_workers=max(1, len(first))) as pool:
        futures = [pool.submit(wait, entry) for entry in first]
        time.sleep(0.5)  # every waiter is listening before the first publish
        for user, _ in users:
            published[user.id] = time.perf_counter()
            realtime.publish_topic(f"user:{user.id}", {"delivery": marker})
        delays, missed = [], 0
        for future in futures:
            try:
                delays.append(future.result())
            except Exception:
                missed += 1
    return delays, missed


def pct(values, q):
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(q * len(values)))] * 1000, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--mode", choices=["default", "pool"], default="default")
    parser.add_argument("--sockets", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=32,
                        help="sockets opened at once")
    parser.add_argument("--no-kill", action="store_true",
                        help="skip the backend kill: a latency control for the second batch")
    parser.add_argument("--label", default="")
    args = parser.parse_args()

    original_conf = CONF.read_text()
    users = []
    if args.mode == "pool":
        CONF.write_text(original_conf.rstrip("\n") + "\n" + POOL_LINE + "\n")
    try:
        server("restart")
        server("wait")
        import django
        django.setup()
        from django.db import connection
        from mojo.apps import realtime

        require_test_database()
        url = server_url()
        log_offset = LOG.stat().st_size if LOG.exists() else 0
        users = make_users()
        time.sleep(1.0)
        baseline = len(server_backends())
        sampler = Sampler()
        sampler.start()

        first, first_errors = open_batch(url, users, args.sockets, args.concurrency)
        # One login on its own while the first batch sits open.
        lone, lone_errors = open_batch(url, users[-1:], 1, 1)
        for ws, *_ in lone:  # free its slot: WS_MAX_CONNECTIONS is per identity
            ws.close(wait=0.5)
        delays, missed = measure_delivery(first, users)
        killed = [] if args.no_kill else kill_server_backends()
        time.sleep(0.5)

        # Open sockets must keep receiving after the kill.
        probe = uuid.uuid4().hex
        for user, _ in users:
            realtime.publish_topic(f"user:{user.id}", {"probe": probe})
        received = 0
        for ws, *_ in first:
            try:
                msg = ws.wait_for_type("message", timeout=5.0)
                received += int(msg.data.get("data", {}).get("probe") == probe)
            except Exception:
                pass

        second, second_errors = open_batch(url, users, args.sockets, args.concurrency)

        # The disconnect hook saves realtime_disconnected_at on an executor
        # thread, so every user whose sockets close after the kill must have a
        # fresh value written.
        User = type(users[0][0])
        User.objects.filter(pk__in=[u.pk for u, _ in users]).update(metadata={})
        for ws, *_ in first + second:
            ws.close(wait=0.5)
        time.sleep(3.0)
        after = len(server_backends())
        used = users[:min(len(users), args.sockets)]
        hooks_ok = sum(1 for user, _ in used
                       if (User.objects.get(pk=user.pk).metadata or {}).get("realtime_disconnected_at"))
        with open(LOG) as log:
            log.seek(log_offset)
            closed_errors = log.read().count("the connection is closed")
        sampler.running = False
        sampler.join(timeout=2)

        logins = [r[2] for r in first]
        subs = [r[3] for r in first]
        logins_after = [r[2] for r in second]
        limit = baseline if args.mode == "default" else max(baseline, 20)
        summary = {
            "label": args.label, "mode": args.mode, "sockets_per_batch": args.sockets,
            "before_kill": {"ok": len(first), "errors": first_errors[:5], "error_count": len(first_errors)},
            "backends_killed": len(killed),
            "open_sockets_received": f"{received}/{len(first)}",
            "after_kill": {"ok": len(second), "errors": second_errors[:5], "error_count": len(second_errors)},
            "disconnect_hook_saved": f"{hooks_ok}/{len(used)}",
            "connection_closed_errors_in_log": closed_errors,
            "login_ms_before_kill": {"p50": pct(logins, 0.5), "p95": pct(logins, 0.95)},
            "login_ms_after_kill": {"p50": pct(logins_after, 0.5), "p95": pct(logins_after, 0.95)},
            "subscribe_ms_before_kill": {"p50": pct(subs, 0.5), "p95": pct(subs, 0.95)},
            "lone_login_ms": pct([r[2] for r in lone], 0.5), "lone_login_errors": lone_errors[:1],
            "delivery_ms": {"p50": pct(delays, 0.5), "p95": pct(delays, 0.95),
                            "received": f"{len(delays)}/{len(first)}", "missed": missed},
            "db_connections": {"baseline": baseline, "peak": sampler.peak, "after_close": after,
                               "allowed_after_close": limit},
        }
        checks = {
            "open_sockets_keep_receiving": received == len(first) and len(first) > 0,
            "new_sockets_log_in_after_kill": len(second) == args.sockets,
            "disconnect_hooks_save_after_kill": hooks_ok == len(used),
            "no_dead_connection_errors": closed_errors == 0,
            "connections_back_to_baseline": after <= limit,
        }
        summary["checks"] = checks

        def under(value, limit_ms):
            return value is not None and value < limit_ms

        summary["latency_targets"] = {
            "login_p95_under_1000ms": under(pct(logins, 0.95), 1000),
            "subscribe_p95_under_100ms": under(pct(subs, 0.95), 100),
            "delivery_p95_under_250ms": under(pct(delays, 0.95), 250) and missed == 0,
            "lone_login_under_100ms": under(summary["lone_login_ms"], 100),
        }
        summary["passed"] = all(checks.values())
        print(json.dumps(summary, indent=1))
        return 0 if summary["passed"] else 1
    finally:
        CONF.write_text(original_conf)
        if users:
            from django.db import connection

            connection.close()  # a failed run may have left it unusable
            for user, _ in users:
                user.delete()
            connection.close()
        if args.mode == "pool":
            server("restart")


if __name__ == "__main__":
    sys.exit(main())
