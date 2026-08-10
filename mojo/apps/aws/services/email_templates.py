"""Shared missing-only installer for email templates shipped with django-mojo."""

import json
import os
import re

from django.db import IntegrityError, transaction


_NAME = re.compile(r"^[a-z0-9_]{1,80}$")


def seed_directory():
    return os.path.join(os.path.dirname(os.path.dirname(__file__)), "seeds", "email_templates")


def load_shipped_templates(path=None):
    directory = os.path.realpath(path or seed_directory())
    if not os.path.isdir(directory):
        raise ValueError("Email template seed directory is unavailable")
    templates = []
    for filename in sorted(os.listdir(directory)):
        if not filename.endswith(".json"):
            continue
        file_path = os.path.realpath(os.path.join(directory, filename))
        if not file_path.startswith(directory + os.sep):
            raise ValueError("Email template seed path is invalid")
        with open(file_path, "r", encoding="utf-8") as stream:
            payload = json.load(stream)
        name = str(payload.get("name") or "").strip()
        if not _NAME.fullmatch(name):
            raise ValueError("Email template seed name is invalid")
        templates.append({
            "name": name,
            "subject_template": str(payload.get("subject_template") or ""),
            "html_template": str(payload.get("html_template") or ""),
            "text_template": str(payload.get("text_template") or ""),
            "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {},
        })
    return templates


def shipped_status(path=None):
    from mojo.apps.aws.models import EmailTemplate
    templates = load_shipped_templates(path)
    names = [row["name"] for row in templates]
    existing = set(EmailTemplate.objects.filter(name__in=names).values_list("name", flat=True))
    return {"total": len(names), "missing": [name for name in names if name not in existing]}


def install_missing(path=None, dry_run=False):
    """Create absent rows and preserve every customized/existing template byte-for-byte."""
    from mojo.apps.aws.models import EmailTemplate
    templates = load_shipped_templates(path)
    created = []
    skipped = []
    with transaction.atomic():
        for payload in templates:
            existing = EmailTemplate.objects.select_for_update().filter(name=payload["name"]).first()
            if existing is not None:
                skipped.append(payload["name"])
                continue
            if dry_run:
                created.append(payload["name"])
                continue
            try:
                # The savepoint is required: a concurrent installer may win
                # the unique name after our absent-row read (which locks
                # nothing). The losing insert rolls back locally and adopts
                # the now-existing row without poisoning the outer transaction.
                with transaction.atomic():
                    EmailTemplate.objects.create(**payload)
                created.append(payload["name"])
            except IntegrityError:
                if not EmailTemplate.objects.filter(name=payload["name"]).exists():
                    raise
                skipped.append(payload["name"])
        if dry_run:
            transaction.set_rollback(True)
    return {"created": created, "skipped": skipped, "total": len(templates)}
