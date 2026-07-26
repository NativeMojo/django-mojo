"""
Route53 adapter — wraps the model-free primitives in mojo/helpers/aws/route53.py.

Route53 specifics this adapter owns:
  - record names are FQDN (no conversion needed, but they are still normalized),
  - TXT values are stored quoted and 255-chunked; the helper quotes on write, so
    this adapter only has to UNQUOTE on read to hand callers raw text,
  - a write returns a ChangeId whose status can be polled, so propagation gates
    on `get_change()` reaching INSYNC *before* the authoritative DNS probe.
"""

import time

from mojo import errors as me
from mojo.helpers import logit
from mojo.helpers.aws import route53
from mojo.helpers.dns import probe
from mojo.helpers.settings import settings

from .base import (
    DEFAULT_PROPAGATION_TIMEOUT, TXT_TYPES, DnsProvider, as_value_list,
    make_record, unquote_txt)


logger = logit.get_logger("dnsman", "dnsman.log")

# How often to re-ask Route53 whether a change batch has gone INSYNC.
CHANGE_POLL_INTERVAL = 5


class Route53Provider(DnsProvider):

    name = "route53"

    def __init__(self, domain):
        super().__init__(domain)
        self._zone_id = domain.hosted_zone_id or None
        # Change ids from writes made through THIS adapter instance, so a
        # caller that reuses the adapter gets the INSYNC gate for free.
        self._change_ids = {}

    @property
    def zone_id(self):
        """
        The hosted zone id, resolved lazily and cached on the adapter.

        Resolution is deliberately not written back to the Domain row: this is
        the mechanism layer and a read path must not produce a surprise write.
        """
        if not self._zone_id:
            found = route53.find_zone_id(self.zone_name)
            if not found:
                raise me.ValueException(
                    f"No Route53 hosted zone exists for '{self.zone_name}'")
            self._zone_id = found
        return self._zone_id

    # -- reads ---------------------------------------------------------------

    def list_records(self):
        records = []
        for record in route53.list_records(self.zone_id):
            values = list(record.record_values or [])
            if (record.type or "").upper() in TXT_TYPES:
                values = [unquote_txt(value) for value in values]
            records.append(make_record(record.type, record.name, values, record.ttl))
        return records

    # -- writes --------------------------------------------------------------

    def upsert_record(self, rtype, name, record_values, ttl=300):
        rtype = (rtype or "").strip().upper()
        values = as_value_list(record_values)
        if not values:
            raise me.ValueException("At least one record value is required")
        # The helper quotes/chunks TXT itself and always sends the whole value
        # list, which is what makes a wildcard and its apex share one
        # _acme-challenge record set carrying two digests.
        change_id = route53.upsert_record(
            self.zone_id, rtype, name, values, ttl=int(ttl), zone_name=self.zone_name)
        self._change_ids[(rtype, name)] = change_id
        return change_id

    def delete_record(self, rtype, name, record_values=None):
        rtype = (rtype or "").strip().upper()
        values = as_value_list(record_values) or None
        change_id = route53.delete_record(
            self.zone_id, rtype, name, values=values, zone_name=self.zone_name)
        self._change_ids[(rtype, name)] = change_id
        return change_id

    # -- propagation ---------------------------------------------------------

    def _wait_for_insync(self, change_id, deadline):
        """Poll the change batch until Route53 reports INSYNC or time runs out."""
        while True:
            change = route53.get_change(change_id)
            if change.insync:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(max(min(CHANGE_POLL_INTERVAL, remaining), 0))

    def wait_for_propagation(self, rtype, name, record_values, timeout=None, change_id=None):
        rtype = (rtype or "").strip().upper()
        values = as_value_list(record_values)
        if timeout is None:
            timeout = settings.get(
                "DNSMAN_DNS_PROPAGATION_TIMEOUT", DEFAULT_PROPAGATION_TIMEOUT)
        deadline = time.monotonic() + max(float(timeout or 0), 0)

        change_id = change_id or self._change_ids.get((rtype, name))
        if change_id and not self._wait_for_insync(change_id, deadline):
            logger.warning(
                f"dnsman: route53 change {change_id} for {rtype} '{name}' never went INSYNC")
            return False, []

        if rtype in TXT_TYPES:
            remaining = max(deadline - time.monotonic(), 0)
            return probe.wait_for_txt(
                name, [unquote_txt(value) for value in values], timeout=remaining)

        # Only TXT has an authoritative probe (it is the one type the ACME and
        # SES gates need). For everything else INSYNC is the real signal.
        return True, values
