"""ACME evidence committed independently of the caller's ownership transaction.

Only this module writes the journal. No owner FK, caller commit, row lock,
network call, Django debug cursor, or globally registered connection alias is
used by the independent seam. Prepared means possibly sent, never no-send.
"""
import hashlib
import json
import uuid
from contextlib import contextmanager

from django.db import connections

from mojo import errors as me

EPOCH = 1
MAX_JSON = 65536
MAX_ROWS = 1000
KINDS = {"origin", "provider_mutation", "hub_mutation", "retirement_barrier"}
TERMINAL = {"final", "rejected_no_change", "aborted_before_send"}
MUTATIONS = {"provider_mutation", "hub_mutation"}
FIELDS = ("id", "attempt_id", "producer_epoch", "kind", "scope_digest", "owner_digest",
          "provider", "owner", "backend", "source_sha", "manifest_sha256", "request_sha256",
          "request", "state", "version", "provider_change_id", "provider_request_id",
          "reason", "receipt_sha256", "created", "modified")


class JournalError(me.ValueException):
    def __init__(self, reason="journal_unavailable", status=503):
        self.error_code = reason
        super().__init__(reason, code=status, status=status)


def encoded(value):
    try:
        result = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(result.encode()) > MAX_JSON:
            raise ValueError()
        return result
    except (ValueError, TypeError, OverflowError):
        raise JournalError("journal_intent_invalid", 400) from None


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def owner_digest(owner):
    if not isinstance(owner, dict) or not isinstance(owner.get("identity"), dict) or not owner["identity"]:
        raise JournalError("journal_intent_invalid", 400)
    return digest(owner["identity"])


class BackendFence:
    """Keep the original primary session; a reconnect is not fence continuity."""
    def __init__(self):
        self.wrapper = connections["default"]
        self.wrapper.ensure_connection()
        self.driver = self.wrapper.connection
        self.identity = self._read()

    def _read(self):
        if (self.wrapper.connection is not self.driver or self.wrapper.needs_rollback
                or getattr(self.driver, "closed", True)):
            raise JournalError("owner_session_lost")
        try:
            with self.driver.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid(), backend_start::text FROM pg_stat_activity WHERE pid=pg_backend_pid()")
                row = cursor.fetchone()
            if not row:
                raise ValueError()
            return {"pid": row[0], "started": row[1]}
        except Exception:
            raise JournalError("owner_session_lost") from None

    def check(self):
        if self._read() != self.identity:
            raise JournalError("owner_session_lost")


@contextmanager
def scope_lock(scope):
    """Last owner fence, on the caller session, with no journal row locking."""
    anchor = BackendFence()
    lock_id = int(digest(scope)[:8], 16) % 2147483647
    with anchor.driver.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s,%s)", [0x41434d45, lock_id])
        admitted = cursor.fetchone()[0]
    if not admitted:
        raise JournalError("challenge_scope_busy", 409)
    try:
        yield anchor
    finally:
        if anchor.wrapper.connection is anchor.driver and not getattr(anchor.driver, "closed", True):
            try:
                with anchor.driver.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s,%s)", [0x41434d45, lock_id])
            except Exception:
                # Session loss releases its locks. Never reconnect to unlock.
                pass


def _identity():
    from mojo.apps.dnsman import acme_hub_feature
    result = {"source_sha": "", "manifest_sha256": acme_hub_feature.manifest()}
    try:
        result["source_sha"] = acme_hub_feature.installed_identity()["source_sha"]
    except Exception:
        # Unbound source can still record compatibility traffic. It cannot
        # satisfy a sealed producer-coverage receipt.
        pass
    return result


class Journal:
    def __init__(self, identity=None, observer=None):
        self.identity = identity or _identity()
        self.observer = observer

    def _sql(self, sql, parameters):
        primary = connections["default"]
        copy = primary.copy(alias="acme_challenge_evidence")
        options = copy.settings_dict.setdefault("OPTIONS", {})
        options.pop("pool", None)
        options["connect_timeout"] = 3
        options["options"] = (options.get("options", "") + " -c statement_timeout=3000 -c lock_timeout=500").strip()
        copy.settings_dict["CONN_MAX_AGE"] = 0
        copy.settings_dict["AUTOCOMMIT"] = True
        try:
            copy.ensure_connection()
            copy.set_autocommit(True)
            # Raw driver cursor: private TXT/ref parameters must never reach
            # Django's SQL-debug recording, even when DEBUG is enabled.
            with copy.connection.cursor() as cursor:
                if self.observer:
                    self.observer("execute", sql, copy)
                cursor.execute(sql, parameters)
                rows = cursor.fetchall() if cursor.description else []
            result = [dict(zip(FIELDS, row)) for row in rows]
            for row in result:
                for key in ("owner", "backend", "request"):
                    if isinstance(row[key], str):
                        row[key] = json.loads(row[key])
            return result
        except JournalError:
            raise
        except Exception:
            raise JournalError() from None
        finally:
            copy.close()
            if self.observer:
                self.observer("closed", sql, copy)

    def project(self, *, scope=None, owner=None, attempt_id=None, kinds=None):
        conditions, arguments = [], []
        if scope is not None:
            conditions.append("scope_digest=%s")
            arguments.append(digest(scope))
        if owner is not None:
            conditions.append("owner_digest=%s")
            arguments.append(owner_digest(owner))
        if attempt_id is not None:
            conditions.append("attempt_id=%s::uuid")
            arguments.append(str(uuid.UUID(str(attempt_id))))
        if kinds is not None:
            if not set(kinds) <= KINDS:
                raise JournalError("journal_intent_invalid", 400)
            conditions.append("kind=ANY(%s)")
            arguments.append(list(kinds))
        if not conditions:
            raise JournalError("journal_intent_invalid", 400)
        rows = self._sql("SELECT " + ",".join(FIELDS) + " FROM dnsman_challenge_attempt WHERE "
                         + " AND ".join(conditions) + " ORDER BY id LIMIT 1001", arguments)
        if len(rows) > MAX_ROWS:
            raise JournalError("journal_projection_incomplete")
        return rows

    def append(self, kind, scope, owner, provider, request, backend, *, attempt_id=None):
        if kind not in KINDS or not isinstance(provider, str) or not 1 <= len(provider) <= 32:
            raise JournalError("journal_intent_invalid", 400)
        attempt_id = str(uuid.UUID(str(attempt_id))) if attempt_id else str(uuid.uuid4())
        state = "prepared" if kind in MUTATIONS else "recorded"
        args = [attempt_id, EPOCH, kind, digest(scope), owner_digest(owner), provider,
                encoded(owner), encoded(backend), self.identity["source_sha"], self.identity["manifest_sha256"],
                digest(request), encoded(request), state]
        conflict = " ON CONFLICT (kind,owner_digest) WHERE kind IN ('origin','retirement_barrier') DO NOTHING" if kind not in MUTATIONS else ""
        rows = self._sql("INSERT INTO dnsman_challenge_attempt (attempt_id,producer_epoch,kind,scope_digest,owner_digest,provider,owner,backend,source_sha,manifest_sha256,request_sha256,request,state,version,provider_change_id,provider_request_id,reason,receipt_sha256,created,modified) VALUES (%s::uuid,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s::jsonb,%s,1,'','','','',NOW(),NOW())"
                         + conflict + " RETURNING " + ",".join(FIELDS), args)
        if not rows:
            rows = self.project(owner=owner, kinds=[kind])
            if len(rows) != 1 or rows[0]["scope_digest"] != digest(scope) or rows[0]["request_sha256"] != digest(request):
                raise JournalError("journal_intent_conflict", 409)
        return rows[0]

    def _cas(self, row, state, *, change_id="", request_id="", reason="", receipt=""):
        for value, maximum in ((change_id, 256), (request_id, 256), (reason, 64), (receipt, 64)):
            if not isinstance(value, str) or len(value) > maximum:
                raise JournalError("journal_receipt_invalid", 400)
        if row["kind"] not in MUTATIONS:
            raise JournalError("journal_receipt_invalid", 400)
        if row["state"] in TERMINAL:
            if row["state"] == state and (not change_id or row["provider_change_id"] == change_id):
                return row
            raise JournalError("journal_receipt_conflict", 409)
        previous = row["provider_change_id"]
        if state in {"aborted_before_send", "rejected_no_change"} and row["state"] != "prepared":
            raise JournalError("journal_receipt_conflict", 409)
        if state == "final" and row["provider"] != "hub" and not previous:
            raise JournalError("journal_receipt_conflict", 409)
        if previous and change_id and previous != change_id:
            raise JournalError("journal_receipt_conflict", 409)
        rows = self._sql("UPDATE dnsman_challenge_attempt SET state=%s,version=version+1,provider_change_id=%s,provider_request_id=%s,reason=%s,receipt_sha256=%s,modified=NOW() WHERE attempt_id=%s::uuid AND version=%s AND request_sha256=%s AND owner_digest=%s RETURNING " + ",".join(FIELDS),
                         [state, change_id or previous, request_id or row["provider_request_id"], reason,
                          receipt or row["receipt_sha256"], str(row["attempt_id"]), row["version"], row["request_sha256"], row["owner_digest"]])
        if len(rows) != 1:
            raise JournalError("journal_receipt_changed", 409)
        return rows[0]

    def receipt(self, row, state, **kwargs):
        if state not in {"accepted", "final", "unknown", "rejected_no_change"}:
            raise JournalError("journal_receipt_invalid", 400)
        if state in {"accepted", "final"} and not (kwargs.get("change_id") or row["provider_change_id"]):
            raise JournalError("provider_receipt_unavailable")
        return self._cas(row, state, **kwargs)

    def prepare(self, kind, scope, owner, provider, request, anchor):
        if kind not in MUTATIONS:
            raise JournalError("journal_intent_invalid", 400)
        attempt_id = str(uuid.uuid4())
        try:
            row = self.append(kind, scope, owner, provider, request, anchor.identity, attempt_id=attempt_id)
        except JournalError:
            # This live invocation has not entered the wire. An uncertain
            # append may have committed; resolve/dispose only its exact UUID.
            try:
                found = self.project(attempt_id=attempt_id)
                if (len(found) == 1 and found[0]["state"] == "prepared"
                        and found[0]["request_sha256"] == digest(request)
                        and found[0]["owner_digest"] == owner_digest(owner)
                        and found[0]["scope_digest"] == digest(scope)
                        and found[0]["kind"] == kind and found[0]["provider"] == provider
                        and found[0]["backend"] == anchor.identity):
                    self._cas(found[0], "aborted_before_send", reason="append_reply_uncertain")
            except JournalError:
                pass
            raise
        return Intent(self, row, anchor)


class Intent:
    def __init__(self, journal, row, anchor):
        self.journal, self.row, self.anchor = journal, row, anchor
        self.entered_wire = False
        self.finished = False

    def send(self, callback, guard=None, no_change_if=None):
        if self.entered_wire or self.finished:
            raise JournalError("mutation_replay_refused", 409)
        try:
            self.anchor.check()
            if guard:
                guard()
            self.anchor.check()
        except Exception:
            self.finished = True
            self.row = self.journal._cas(self.row, "aborted_before_send", reason="controlled_guard_refusal")
            raise
        # No cursor/transaction/copy survives prepare or CAS. After this exact
        # assignment every exception is possibly-sent; never claim no-send.
        self.entered_wire = True
        try:
            return callback()
        except Exception as error:
            try:
                conclusive = no_change_if is not None and no_change_if(error) is True
                self.row = self.journal.receipt(self.row, "rejected_no_change" if conclusive else "unknown",
                    reason="provider_rejected_no_change" if conclusive else "wire_outcome_unknown")
            except JournalError:
                pass
            raise
        finally:
            self.finished = True


def all_final(rows):
    return all(row["state"] in TERMINAL for row in rows if row["kind"] in MUTATIONS)
