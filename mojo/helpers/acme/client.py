"""A minimal ACMEv2 (RFC 8555) client -- DNS-01 only.

Scope is deliberately narrow: everything dnsman needs to obtain and renew a
Let's Encrypt certificate centrally over DNS-01, and nothing else. There is no
HTTP-01 support, no key-change flow, and no account deactivation, because
dnsman does not use them.

Transport is plain ``requests`` at module level (never a session), so a test
can patch ``mojo.helpers.acme.client.requests`` and be certain no socket is
opened. Nothing here imports Django.
"""
import json
import time

import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.x509.oid import NameOID
from objict import objict

from mojo import errors as me

from . import jws


DEFAULT_TIMEOUT = 30
DEFAULT_POLL_TIMEOUT = 300
DEFAULT_POLL_INTERVAL = 3
MAX_POLL_INTERVAL = 10
# A hostile or confused CA must not be able to park a worker forever.
MAX_RETRY_AFTER = 60

USER_AGENT = "django-mojo-acme/1.0"
JOSE_CONTENT_TYPE = "application/jose+json"
PEM_CHAIN_ACCEPT = "application/pem-certificate-chain"

BAD_NONCE = "urn:ietf:params:acme:error:badNonce"

# Order/authorization states that end a poll loop.
ORDER_DONE = ("valid", "invalid")
ORDER_READY = ("ready", "valid", "invalid")

LETSENCRYPT_PRODUCTION = "https://acme-v02.api.letsencrypt.org/directory"
LETSENCRYPT_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"


def _log(level, message):
    """Best-effort log line.

    logit is imported lazily so this module stays importable without a
    configured project (logit resolves its log directory at import time).
    """
    try:
        from mojo.helpers import logit
        getattr(logit, level)(message)
    except Exception:
        pass


def make_csr(key, names):
    """Build a DER CSR covering every name, signed with SHA-256.

    Every name -- including wildcards like ``*.example.com`` -- goes into the
    SubjectAlternativeName extension. Modern CAs ignore the subject CN and
    issue purely from the SANs, so a name that only appeared in the CN would
    simply be missing from the certificate.
    """
    if isinstance(names, str):
        names = [names]
    ordered = []
    for name in names:
        name = (name or "").strip().rstrip(".").lower()
        if not name:
            continue
        if name not in ordered:
            ordered.append(name)
    if not ordered:
        raise me.ValueException("A certificate request needs at least one name")

    builder = x509.CertificateSigningRequestBuilder()
    # The CN is capped at 64 characters by X.509; a longer first name is simply
    # left out rather than failing the request, since the SANs carry the truth.
    if len(ordered[0]) <= 64:
        builder = builder.subject_name(x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, ordered[0])]))
    else:
        builder = builder.subject_name(x509.Name([]))
    builder = builder.add_extension(
        x509.SubjectAlternativeName([x509.DNSName(n) for n in ordered]),
        critical=False)
    csr = builder.sign(key, hashes.SHA256())
    return csr.public_bytes(serialization.Encoding.DER)


class AcmeClient(object):
    """An ACMEv2 account bound to one directory URL and one P-256 account key."""

    def __init__(self, directory_url, account_key, contact_email=None,
                 kid=None, timeout=DEFAULT_TIMEOUT):
        self.directory_url = directory_url
        self.account_key = account_key
        self.contact_email = contact_email
        # The account URL. Set by new_account(), or passed in when the account
        # was registered in an earlier run and persisted.
        self.kid = kid
        self.timeout = timeout
        self.nonce = None
        self._directory = None

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    def _headers(self, extra=None):
        headers = {"User-Agent": USER_AGENT}
        if extra:
            headers.update(extra)
        return headers

    def _update_nonce(self, resp):
        value = resp.headers.get("Replay-Nonce")
        if value:
            self.nonce = value
        return value

    def _retry_after(self, resp):
        """Seconds to wait, per the response's Retry-After header (0 if absent)."""
        value = resp.headers.get("Retry-After")
        if not value:
            return 0
        try:
            seconds = int(str(value).strip())
        except (TypeError, ValueError):
            # HTTP-date form. We do not parse it; fall back to a short wait
            # rather than pretending we can honour it precisely.
            return DEFAULT_POLL_INTERVAL
        if seconds < 0:
            return 0
        return min(seconds, MAX_RETRY_AFTER)

    def _json(self, resp):
        try:
            data = resp.json()
        except Exception:
            return {}
        if isinstance(data, dict):
            return data
        return {}

    def _problem(self, resp):
        """Return the ACME problem document for a failed response, else None."""
        status = getattr(resp, "status_code", 200)
        if status < 400:
            return None
        data = self._json(resp)
        if data.get("type") or data.get("detail"):
            return data
        text = getattr(resp, "text", "") or ""
        return {
            "type": "urn:ietf:params:acme:error:serverInternal",
            "detail": f"HTTP {status} from the ACME server: {text[:200]}"
        }

    def _raise_problem(self, problem, url):
        """Map an ACME problem document onto a ValueException, verbatim.

        The CA's ``type`` and ``detail`` are preserved word for word so a rate
        limit or a failed validation is legible where it lands (a certificate
        row's last_error) instead of surfacing as an opaque 500.
        """
        ptype = problem.get("type") or "about:blank"
        detail = problem.get("detail") or "no detail supplied"
        parts = [f"ACME error from {url}: {ptype}: {detail}"]
        for sub in problem.get("subproblems") or []:
            identifier = (sub.get("identifier") or {}).get("value") or ""
            parts.append(f"[{identifier}] {sub.get('type')}: {sub.get('detail')}")
        error = me.ValueException(" | ".join(parts))
        error.problem = problem
        error.problem_type = ptype
        error.detail = detail
        raise error

    def directory(self):
        """Fetch (once) and cache the CA's directory document."""
        if self._directory is None:
            resp = requests.get(
                self.directory_url,
                headers=self._headers({"Accept": "application/json"}),
                timeout=self.timeout)
            self._update_nonce(resp)
            problem = self._problem(resp)
            if problem is not None:
                self._raise_problem(problem, self.directory_url)
            self._directory = objict.from_dict(self._json(resp))
        return self._directory

    def _directory_url_for(self, name):
        url = self.directory().get(name)
        if not url:
            raise me.ValueException(
                f"The ACME directory at {self.directory_url} does not advertise '{name}'")
        return url

    def new_nonce(self):
        """HEAD newNonce and cache the returned Replay-Nonce."""
        url = self._directory_url_for("newNonce")
        resp = requests.head(url, headers=self._headers(), timeout=self.timeout)
        self._update_nonce(resp)
        if not self.nonce:
            raise me.ValueException(f"The ACME server returned no nonce from {url}")
        return self.nonce

    def _post(self, url, payload, kid=None, use_jwk=False, key=None,
              accept=None, _retry=True):
        """Sign and POST a JWS to url, returning the raw response.

        The protected header carries exactly ``alg``, ``nonce``, ``url`` plus
        one of ``jwk`` / ``kid`` -- never both (RFC 8555 section 6.2). ``jwk``
        is used for newAccount, and for a revokeCert authenticated with the
        certificate's own key; everything else is ``kid``.

        A ``badNonce`` problem is retried exactly once with a fresh nonce,
        which is the CA's documented way of resynchronizing.
        """
        signing_key = key or self.account_key
        kid = kid or self.kid
        nonce = self.nonce or self.new_nonce()
        protected = {"alg": jws.ALG, "nonce": nonce, "url": url}
        if use_jwk:
            protected["jwk"] = jws.jwk(signing_key)
        else:
            if not kid:
                raise me.ValueException(
                    "This ACME request needs an account URL (kid) -- register the account first")
            protected["kid"] = kid

        body = jws.sign(payload, signing_key, protected)
        headers = {"Content-Type": JOSE_CONTENT_TYPE}
        if accept:
            headers["Accept"] = accept
        resp = requests.post(
            url,
            data=json.dumps(body),
            headers=self._headers(headers),
            timeout=self.timeout)
        self._update_nonce(resp)

        problem = self._problem(resp)
        if problem is None:
            return resp

        if _retry and problem.get("type") == BAD_NONCE:
            # The rejecting response carries a usable nonce of its own; if it
            # did not, force a fresh HEAD rather than replaying the bad one.
            if self.nonce == nonce:
                self.nonce = None
            wait = self._retry_after(resp)
            if wait:
                time.sleep(wait)
            _log("info", f"acme: badNonce from {url}, retrying once with a fresh nonce")
            return self._post(url, payload, kid=kid, use_jwk=use_jwk,
                              key=key, accept=accept, _retry=False)

        self._raise_problem(problem, url)

    # ------------------------------------------------------------------
    # account
    # ------------------------------------------------------------------

    def new_account(self, contact=None):
        """Register (or look up) the account and return its URL -- the kid."""
        contact = contact or self.contact_email
        payload = {"termsOfServiceAgreed": True}
        contacts = []
        if contact:
            if isinstance(contact, str):
                contact = [contact]
            for entry in contact:
                if not entry:
                    continue
                if not entry.startswith("mailto:"):
                    entry = f"mailto:{entry}"
                contacts.append(entry)
        if contacts:
            payload["contact"] = contacts

        resp = self._post(self._directory_url_for("newAccount"), payload, use_jwk=True)
        account_url = resp.headers.get("Location")
        if not account_url:
            raise me.ValueException(
                "The ACME server did not return an account URL (Location header)")
        self.kid = account_url
        return account_url

    # ------------------------------------------------------------------
    # orders and authorizations
    # ------------------------------------------------------------------

    def new_order(self, identifiers):
        """Create an order for one or more DNS identifiers.

        The returned order carries a ``url`` member taken from the Location
        header -- that is the URL to poll, and it is not in the body.
        """
        if isinstance(identifiers, str):
            identifiers = [identifiers]
        names = [n.strip().rstrip(".").lower() for n in identifiers if n and n.strip()]
        if not names:
            raise me.ValueException("An ACME order needs at least one identifier")
        payload = {"identifiers": [{"type": "dns", "value": n} for n in names]}
        resp = self._post(self._directory_url_for("newOrder"), payload)
        order = objict.from_dict(self._json(resp))
        order.url = resp.headers.get("Location")
        return order

    def get_order(self, url):
        """POST-as-GET an order URL."""
        resp = self._post(url, "")
        order = objict.from_dict(self._json(resp))
        order.url = url
        return order

    def get_authorization(self, url):
        """POST-as-GET an authorization URL."""
        resp = self._post(url, "")
        authz = objict.from_dict(self._json(resp))
        authz.url = url
        return authz

    def dns01_challenge(self, authz):
        """Return ``(challenge_url, token)`` for the authorization's dns-01 challenge."""
        for challenge in authz.get("challenges") or []:
            if challenge.get("type") == "dns-01":
                url = challenge.get("url")
                token = challenge.get("token")
                if not url or not token:
                    raise me.ValueException(
                        "The dns-01 challenge is missing its url or token")
                return url, token
        identifier = (authz.get("identifier") or {}).get("value") or "the identifier"
        raise me.ValueException(f"No dns-01 challenge was offered for {identifier}")

    def key_authorization(self, token):
        """The RFC 8555 key authorization for a challenge token."""
        return jws.key_authorization(token, self.account_key)

    def key_authorization_digest(self, token):
        """The DNS-01 TXT value to publish at ``_acme-challenge.<name>``."""
        return jws.dns_txt_value(token, self.account_key)

    def answer_challenge(self, url):
        """Tell the CA the challenge is ready (an empty JSON object payload)."""
        resp = self._post(url, {})
        return objict.from_dict(self._json(resp))

    # ------------------------------------------------------------------
    # polling
    # ------------------------------------------------------------------

    def _poll(self, url, until, timeout, interval, what):
        """POST-as-GET url with backoff until its status is in until.

        Returns the resource. Callers must inspect ``status`` -- an ``invalid``
        result is a normal, non-exceptional outcome that carries the CA's
        error detail with it.
        """
        deadline = time.monotonic() + timeout
        wait = interval
        while True:
            resp = self._post(url, "")
            resource = objict.from_dict(self._json(resp))
            resource.url = url
            status = resource.get("status")
            if status in until:
                return resource
            if time.monotonic() >= deadline:
                raise me.ValueException(
                    f"Timed out after {timeout}s waiting for {what} {url} "
                    f"to settle (last status: {status})")
            after = self._retry_after(resp)
            time.sleep(after or wait)
            wait = min(wait * 1.5, MAX_POLL_INTERVAL)

    def poll_order(self, url, until=None, timeout=DEFAULT_POLL_TIMEOUT,
                   interval=DEFAULT_POLL_INTERVAL):
        """Poll an order until it reaches a terminal status.

        Defaults to ``valid``/``invalid``. Pass ``until=ORDER_READY`` when
        waiting for validations to finish before finalizing.
        """
        return self._poll(url, until or ORDER_DONE, timeout, interval, "order")

    def poll_authorization(self, url, until=None, timeout=DEFAULT_POLL_TIMEOUT,
                           interval=DEFAULT_POLL_INTERVAL):
        """Poll an authorization until it is valid or invalid."""
        return self._poll(url, until or ORDER_DONE, timeout, interval, "authorization")

    # ------------------------------------------------------------------
    # issuance
    # ------------------------------------------------------------------

    def finalize(self, order, csr_der):
        """Submit the CSR. Accepts an order object or a bare finalize URL."""
        if isinstance(order, str):
            finalize_url = order
        else:
            finalize_url = order.get("finalize")
        if not finalize_url:
            raise me.ValueException("The ACME order has no finalize URL")
        resp = self._post(finalize_url, {"csr": jws.b64(csr_der)})
        return objict.from_dict(self._json(resp))

    def download_certificate(self, url):
        """POST-as-GET the certificate URL and return the full PEM chain."""
        resp = self._post(url, "", accept=PEM_CHAIN_ACCEPT)
        return getattr(resp, "text", "") or ""

    def revoke_certificate(self, cert_der, reason=0, key=None):
        """Revoke a certificate.

        Signed with the account key (kid) by default. Pass ``key=`` to
        authenticate with the certificate's own private key instead, which
        RFC 8555 section 6.2 requires be sent as ``jwk``.
        """
        payload = {"certificate": jws.b64(cert_der), "reason": reason}
        self._post(self._directory_url_for("revokeCert"), payload,
                   use_jwk=key is not None, key=key)
        return True
