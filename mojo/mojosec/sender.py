"""Batched HTTPS delivery with per-event acknowledgements and retry."""

import gzip
import json
import os
import stat
import urllib.error
import urllib.request

from .protocol import ProtocolError, canonical_json, make_batch, validate_ack


MAX_CREDENTIAL_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def read_credential(path):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("MojoSec credential must be a regular file")
        if info.st_mode & 0o077:
            raise ValueError("MojoSec credential permissions must be 0600 or stricter")
        if os.geteuid() == 0 and info.st_uid != 0:
            raise ValueError("MojoSec credential must be owned by root")
        if info.st_size > MAX_CREDENTIAL_BYTES:
            raise ValueError("MojoSec credential is too large")
        payload = os.read(descriptor, MAX_CREDENTIAL_BYTES + 1)
    finally:
        os.close(descriptor)
    try:
        token = payload.decode("utf-8").strip()
    except UnicodeDecodeError as err:
        raise ValueError("MojoSec credential is not UTF-8") from err
    if (not token or len(token) > 8192 or
            any(ord(character) < 33 or ord(character) == 127 for character in token)):
        raise ValueError("MojoSec credential must contain one non-empty API key")
    return token


class HttpTransport:
    def __init__(self):
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        )

    def post(self, endpoint, body, headers, timeout):
        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            response = self.opener.open(request, timeout=timeout)
            with response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise ValueError("MojoSec receiver response is too large")
                return response.status, payload
        except urllib.error.HTTPError as err:
            payload = err.read(MAX_RESPONSE_BYTES + 1)
            return err.code, payload


class Sender:
    def __init__(self, config, store, transport=None):
        self.config = config
        self.store = store
        self.transport = transport or HttpTransport()

    def send_once(self):
        delivery = self.config["delivery"]
        self.store.flush_due()
        events = self.store.pending_batch(delivery["batch_events"], delivery["batch_bytes"])
        if not events:
            return {"sent": 0, "accepted": 0, "retry": 0}
        while events:
            batch = make_batch(self.config["sensor_id"], events, self.config["policy_revision"])
            raw = canonical_json(batch).encode("utf-8")
            if len(raw) <= delivery["batch_bytes"]:
                break
            events.pop()
        if not events:
            raise ProtocolError("configured batch_bytes cannot hold one valid event")
        token = read_credential(self.config["credential_path"])
        body = gzip.compress(raw) if delivery["gzip"] else raw
        headers = {
            "Authorization": f"apikey {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "django-mojo-mojosec/1",
        }
        if delivery["gzip"]:
            headers["Content-Encoding"] = "gzip"
        sent_ids = [event["id"] for event in events]
        try:
            status, response_body = self.transport.post(
                self.config["endpoint"], body, headers, delivery["timeout_seconds"]
            )
            if status < 200 or status >= 300:
                raise ValueError(f"receiver returned HTTP {status}")
            ack = validate_ack(json.loads(response_body.decode("utf-8")), expected_ids=sent_ids)
            results = ack["results"]
            self.store.mark_delivery(sent_ids, results)
        except Exception as err:
            self.store.mark_delivery(sent_ids, [], error=str(err)[:256])
            return {"sent": len(sent_ids), "accepted": 0, "retry": len(sent_ids),
                    "error": str(err)[:256]}
        accepted = sum(1 for item in results if item["status"] in ("accepted", "duplicate"))
        retry = len(sent_ids) - sum(1 for item in results if item["status"] in
                                    ("accepted", "duplicate", "rejected"))
        return {"sent": len(sent_ids), "accepted": accepted, "retry": retry}
