"""
Re-encrypt MojoSecrets rows between the legacy format and MOJO_SECRETS_KEY (v2).

Never runs on upgrade or migration; an operator runs it. Dry run by default.

    manage.py rewrap_secrets                      # report only
    manage.py rewrap_secrets --apply              # legacy / fallback-key rows -> primary key
    manage.py rewrap_secrets --to-legacy --apply  # v2 rows -> legacy (rollback)

Each row is locked, decrypted and swapped only if its blob is unchanged, so a
concurrent write wins and is picked up by the next run. model.save() is not
used: no cache push, no protected-row refusal, no `modified` change.
"""
import hashlib

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from mojo.helpers import crypto
from mojo.helpers.crypto import aes
from mojo.helpers.crypto.keys import secrets_keys
from mojo.models.secrets import MojoSecrets, MIN_SECRETS_KEY_LENGTH

EMPTY_MARKERS = ("{}", "")
COUNT_FIELDS = ("total", "v2_primary", "v2_fallback", "legacy", "empty_marker", "rewrapped")


class RewrapError(Exception):
    """A row could not be read; nothing was written for it."""


def key_fingerprint(key):
    """Short, non-reversible id so operators can confirm every host has the same key."""
    if not key:
        return None
    return hashlib.sha256(b"mojo-secrets-fingerprint:" + key.encode("utf-8")).hexdigest()[:12]


def secrets_models(labels=None):
    found = []
    for model in apps.get_models():
        if not issubclass(model, MojoSecrets) or model._meta.proxy:
            continue
        if labels and model._meta.label_lower not in labels:
            continue
        found.append(model)
    if labels:
        missing = set(labels) - {m._meta.label_lower for m in found}
        if missing:
            raise RewrapError(f"unknown MojoSecrets model(s): {', '.join(sorted(missing))}")
    return sorted(found, key=lambda m: m._meta.label_lower)


def _writes_v2(model):
    return model._get_secrets_password is MojoSecrets._get_secrets_password


def _read(row, blob, keys):
    """Returns (data, kind) where kind is v2_primary, v2_fallback or legacy."""
    try:
        if aes.is_v2(blob):
            data, index = aes.decrypt_v2(blob, keys, row._secrets_context())
            kind = "v2_primary" if index == 0 else "v2_fallback"
        else:
            data = crypto.decrypt(blob, row._get_secrets_password(), False)
            kind = "legacy"
    except Exception as error:
        raise RewrapError(f"{row._meta.label_lower} pk={row.pk}: cannot decrypt ({type(error).__name__})") from None
    if not isinstance(data, dict):
        raise RewrapError(f"{row._meta.label_lower} pk={row.pk}: decrypted secrets are not a mapping")
    return data, kind


def _target(model, kind, to_legacy):
    """Format a row of this kind should move to, or None to leave it."""
    if to_legacy:
        return "legacy" if kind in ("v2_primary", "v2_fallback") else None
    if not _writes_v2(model):
        return None
    return "v2" if kind in ("legacy", "v2_fallback") else None


def rewrap(models=None, pk=None, apply=False, to_legacy=False, batch_size=200, keys=None, out=None):
    """Walk MojoSecrets rows, count formats and (with apply) re-encrypt.

    keys: candidate key list (primary first); defaults to MOJO_SECRETS_KEY(_FALLBACKS).
    Returns {model_label: counts}. Stops with RewrapError at the first unreadable row.
    """
    keys = list(secrets_keys() if keys is None else keys)
    primary = keys[0] if keys else ""
    if apply and not to_legacy:
        if not primary:
            raise RewrapError("--apply needs MOJO_SECRETS_KEY set; nothing to rewrap to")
        if len(primary) < MIN_SECRETS_KEY_LENGTH:
            raise RewrapError(f"MOJO_SECRETS_KEY must be at least {MIN_SECRETS_KEY_LENGTH} characters")
    if apply and to_legacy and primary:
        raise RewrapError("--to-legacy --apply is refused while MOJO_SECRETS_KEY is set; "
                          "move it into MOJO_SECRETS_KEY_FALLBACKS first")
    write = (lambda line: out(line)) if out else (lambda line: None)
    write(f"primary key fingerprint: {key_fingerprint(primary) or 'none (not opted in)'}")
    write(f"mode: {'to-legacy' if to_legacy else 'to-primary'} {'APPLY' if apply else 'dry run'}")

    report = {}
    for model in secrets_models(models):
        counts = dict.fromkeys(COUNT_FIELDS, 0)
        report[model._meta.label_lower] = counts
        qs = model.objects.exclude(mojo_secrets__isnull=True)
        if pk is not None:
            qs = qs.filter(pk=pk)
        last_pk = None
        while True:
            batch_qs = qs.order_by("pk")
            if last_pk is not None:
                batch_qs = batch_qs.filter(pk__gt=last_pk)
            pks = list(batch_qs.values_list("pk", flat=True)[:batch_size])
            if not pks:
                break
            last_pk = pks[-1]
            for row_pk in pks:
                _rewrap_row(model, row_pk, keys, primary, apply, to_legacy, counts)
        write(f"{model._meta.label_lower}: " + " ".join(f"{k}={counts[k]}" for k in COUNT_FIELDS))
    return report


def _rewrap_row(model, row_pk, keys, primary, apply, to_legacy, counts):
    with transaction.atomic():
        lock = model.objects.select_for_update() if apply else model.objects
        row = lock.filter(pk=row_pk).first()
        if row is None or row.mojo_secrets is None:
            return
        counts["total"] += 1
        blob = row.mojo_secrets
        if blob in EMPTY_MARKERS:
            counts["empty_marker"] += 1
            return
        data, kind = _read(row, blob, keys)
        counts[kind] += 1
        target = _target(model, kind, to_legacy)
        if target is None or not apply:
            return
        if target == "v2":
            new_blob = aes.encrypt_v2(data, primary, row._secrets_context())
        else:
            new_blob = crypto.encrypt(data, row._get_secrets_password())
        updated = model.objects.filter(pk=row_pk, mojo_secrets=blob).update(mojo_secrets=new_blob)
        counts["rewrapped"] += updated


class Command(BaseCommand):
    help = "Re-encrypt MojoSecrets rows to MOJO_SECRETS_KEY (or back to legacy). Dry run by default."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
        parser.add_argument("--to-legacy", action="store_true", help="Move v2 rows back to the legacy format")
        parser.add_argument("--model", action="append", dest="models", default=None,
                            help="app_label.Model to limit to (repeatable)")
        parser.add_argument("--pk", type=int, default=None, help="Only this primary key")
        parser.add_argument("--batch-size", type=int, default=200)

    def handle(self, *args, **options):
        labels = [label.lower() for label in options["models"]] if options["models"] else None
        try:
            rewrap(models=labels, pk=options["pk"], apply=options["apply"],
                   to_legacy=options["to_legacy"], batch_size=max(1, options["batch_size"]),
                   out=self.stdout.write)
        except RewrapError as error:
            raise CommandError(str(error))
