"""Mask sign-in codes and links already stored in the SMS log.

Rows sent before SMS.send(secret=...) existed keep the full text. This
command rewrites the text after a known auth prefix to [redacted] and cuts
every row's metadata['remote'] down to REMOTE_METADATA_KEYS.

Dry run by default: prints counts only. --apply writes. Idempotent. It never
prints a body, code, number or metadata value.
"""
from django.core.management.base import BaseCommand
from django.db.models import Q

from mojo.apps.phonehub.models import SMS
from mojo.apps.phonehub.models.sms import REDACTED, remote_metadata

AUTH_PREFIXES = (
    "Your verification code is: ",
    "Your password reset code is: ",
    "Your login link: ",
    "Your phone change verification code is: ",
)
BATCH_SIZE = 500


def _masked_body(body):
    for prefix in AUTH_PREFIXES:
        if body.startswith(prefix):
            return prefix + REDACTED
    return body


def mask_rows(apply=False, batch_size=BATCH_SIZE):
    """Return counts {scanned, bodies, metadata}; write only when apply."""
    match = Q(metadata__has_key="remote")
    for prefix in AUTH_PREFIXES:
        match |= Q(body__startswith=prefix)
    counts = dict(scanned=0, bodies=0, metadata=0)
    last_pk = 0
    while True:
        rows = list(SMS.objects.filter(match, pk__gt=last_pk)
                    .order_by("pk").only("pk", "body", "metadata")[:batch_size])
        if not rows:
            return counts
        last_pk = rows[-1].pk
        changed = []
        for sms in rows:
            counts["scanned"] += 1
            dirty = False
            body = _masked_body(sms.body or "")
            if body != (sms.body or ""):
                sms.body = body
                counts["bodies"] += 1
                dirty = True
            md = sms.metadata if isinstance(sms.metadata, dict) else {}
            if "remote" in md:
                remote = remote_metadata(md["remote"])
                if remote != md["remote"]:
                    md["remote"] = remote
                    sms.metadata = md
                    counts["metadata"] += 1
                    dirty = True
            if dirty:
                changed.append(sms)
        if apply and changed:
            SMS.objects.bulk_update(changed, ["body", "metadata"])


class Command(BaseCommand):
    help = "Mask sign-in codes and links stored in the SMS log (dry run unless --apply)"

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the masked rows")

    def handle(self, *args, **options):
        apply = options["apply"]
        counts = mask_rows(apply=apply)
        verb = "masked" if apply else "would mask"
        self.stdout.write(
            f"scanned {counts['scanned']} rows; {verb} {counts['bodies']} bodies "
            f"and {counts['metadata']} relay metadata copies"
            + ("" if apply else " (dry run; pass --apply to write)"))
