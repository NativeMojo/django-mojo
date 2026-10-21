"""Pure local projections and explicit, independently durable cleanup evidence.

A listing is used only to prepare an exact conditional request. It is never an
outcome witness for an earlier request. Root producer enrollment remains a
separate prerequisite and this module cannot create that enrollment.
"""
import hmac
import time
import uuid
from hashlib import sha256

from django.conf import settings
from django.core import signing

from mojo.apps.dnsman.services import challenge_attempts as attempts
from mojo.deploy import acme_dns_coverage
from mojo.helpers import dates
from mojo.helpers.aws import route53_challenge as raw


def generation(value, actor=None):
    identity = getattr(getattr(actor, "user", actor), "pk", None)
    value = {"binding": opaque(value), "actor": opaque(identity), "window": int(time.time() // 300)}
    return signing.dumps(value, salt="dnsman.challenge.cleanup.v1", compress=False)


def check_generation(token, value, actor=None):
    try:
        data = signing.loads(token, salt="dnsman.challenge.cleanup.v1", max_age=300)
        expected = {"binding": opaque(value), "actor": opaque(getattr(getattr(actor, "user", actor), "pk", None)),
                    "window": data["window"]}
        if data != expected:
            raise ValueError()
    except (signing.BadSignature, TypeError, ValueError, KeyError):
        raise attempts.JournalError("generation_changed", 409) from None


def opaque(value):
    return hmac.new(str(settings.SECRET_KEY).encode(), attempts.encoded(value).encode(), sha256).hexdigest()


class Cleanup:
    """Internal dependency seam; public services supply stored owners only."""
    def __init__(self, journal=None, coverage=None, client=None):
        self.journal = journal or attempts.Journal()
        self.coverage_reader = coverage or acme_dns_coverage.load
        self.client_factory = client or raw.mutation_client

    def projection(self, owner, origin):
        scope = owner["binding"]["scope"]
        rows = self.journal.project(scope=scope)
        own = attempts.owner_digest(owner)
        barriers = [row for row in rows if row["kind"] == "retirement_barrier" and row["owner_digest"] == own]
        cleanups = [row for row in rows if row["owner"].get("binding", {}).get("retired_owner") == own]
        prior = [row for row in rows if row not in cleanups]
        coverage_state, coverage_hash, reason = "unavailable", "", "producer_coverage_unavailable"
        try:
            receipt, coverage_hash = self.coverage_reader(self.journal.identity)
            reason = acme_dns_coverage.qualifies(receipt, scope, origin)
            coverage_state = "qualified" if reason is None else "legacy_unknown" if reason == "legacy_history_unknown" else "unavailable"
        except (acme_dns_coverage.CoverageError, OSError, ValueError, TypeError):
            pass
        all_prior = attempts.all_final(prior)
        cleanup_mutations = [row for row in cleanups if row["kind"] in attempts.MUTATIONS]
        no_change = any(row["kind"] == "origin" and row["request"].get("cleanup_no_change") is True for row in cleanups)
        cleanup_final = bool(barriers and all_prior and (no_change or cleanup_mutations and all(row["state"] == "final" for row in cleanup_mutations)))
        qualified = coverage_state == "qualified"
        final = bool(qualified and all_prior and cleanup_final)
        if not all_prior and reason is None:
            reason = "prior_attempt_unknown" if any(row["state"] in {"prepared", "unknown"} for row in prior if row["kind"] in attempts.MUTATIONS) else "provider_change_pending"
        if qualified and all_prior and not cleanup_final:
            reason = "provider_change_pending" if cleanup_mutations else None
        binding = {"owner": own, "origin": str(origin["attempt_id"]), "coverage": coverage_hash,
                   "rows": [[str(row["attempt_id"]), row["version"], row["state"]] for row in rows]}
        return {"rows": rows, "coverage_state": coverage_state, "coverage_hash": coverage_hash,
                "all_prior_final": all_prior, "cleanup_final": cleanup_final, "complete": final,
                "retirement_recorded": bool(barriers), "error_code": reason, "binding": binding,
                "state": "final" if final else "unknown" if not all_prior else "pending" if cleanup_mutations else "not_started"}

    def poll(self, projection):
        """Explicit action only: exact stored ID checks, one CAS per receipt."""
        client = None
        try:
            for row in projection["rows"]:
                if row["kind"] != "provider_mutation" or row["state"] in attempts.TERMINAL:
                    continue
                if not row["provider_change_id"]:
                    continue
                if client is None:
                    client = self.client_factory()
                status = raw.status(client, row["provider_change_id"])
                if status == "INSYNC":
                    self.journal.receipt(row, "final", change_id=row["provider_change_id"],
                        receipt=attempts.digest({"id": row["provider_change_id"], "status": "INSYNC"}))
        finally:
            if client is not None:
                client.close()

    def execute(self, owner, origin, *, guard=None, after_barrier=None, desired=None):
        scope = owner["binding"]["scope"]
        with attempts.scope_lock(scope) as anchor:
            projection = self.projection(owner, origin)
            if projection["coverage_state"] != "qualified":
                raise attempts.JournalError(projection["error_code"] or "producer_coverage_unavailable")
            if owner["binding"]["scope"]["provider"] != "route53":
                raise attempts.JournalError("provider_conditional_unsupported")
            self.poll(projection)
            projection = self.projection(owner, origin)
            if not projection["all_prior_final"]:
                return projection
            if projection["complete"]:
                return projection
            if guard:
                guard()
            self.journal.append("retirement_barrier", scope, owner, "route53", {"retired": True}, anchor.identity)
            if after_barrier:
                after_barrier()
            child = {"identity": {"kind": "conditional_cleanup", "owner": attempts.owner_digest(owner)},
                     "binding": {"scope": scope, "retired_owner": attempts.owner_digest(owner)}}
            client = self.client_factory()
            try:
                observed = raw.snapshot(client, scope["zone_id"], scope["name"])
                request = raw.batch(scope["zone_id"], scope["name"], observed,
                    origin["request"].get("owned_values", []), desired() if desired else [],
                    comment="mojo-acme:" + uuid.uuid4().hex)
                if request is None:
                    self.journal.append("origin", scope, child, "route53",
                        {"cleanup_no_change": True}, anchor.identity)
                else:
                    prior_cleanup = self.journal.project(owner=child)
                    if not attempts.all_final(prior_cleanup):
                        return self.projection(owner, origin)
                    intent = self.journal.prepare("provider_mutation", scope, child, "route53", request, anchor)
                    result = intent.send(lambda: raw.submit(client, request), guard=guard,
                        no_change_if=lambda error: isinstance(error, raw.ChallengeError) and error.no_change is True)
                    self.journal.receipt(intent.row, "accepted", change_id=result["change_id"], request_id=result["request_id"])
            finally:
                client.close()
            result = self.projection(owner, origin)
            self.poll(result)
            return self.projection(owner, origin)


def owned_dto(owner, origin, projection, actor=None):
    provider = owner["binding"]["scope"]["provider"]
    supported = provider == "route53"
    reason = projection["error_code"] if supported else "provider_conditional_unsupported"
    executable = bool(supported and projection["coverage_state"] == "qualified" and projection["all_prior_final"] and not projection["complete"])
    return {"version": 1, "state": "complete" if projection["complete"] else "eligible" if executable else "unavailable" if reason else "pending",
            "generation": generation(projection["binding"], actor), "provider": provider,
            "coverage_state": projection["coverage_state"], "prior_attempts_final": projection["all_prior_final"],
            "cleanup_final": projection["cleanup_final"], "can_execute": executable,
            "checked_at": dates.utcnow().isoformat(), "error_code": reason,
            "next_action": {"actor": "deployment_operator", "route": "dns", "panel": "challenge-recovery",
                            "operation": str(origin["attempt_id"])}}
