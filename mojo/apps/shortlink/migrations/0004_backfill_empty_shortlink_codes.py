"""Backfill codes for ShortLink rows saved with code="".

Before the on_rest_pre_save hook existed, the RestMeta create path
(POST /api/shortlink/link) persisted rows without generating a code,
leaving at most one row per database with code="" (the field is unique).
Such a row is unresolvable via /s/<code> and blocks every subsequent
REST create with a unique-constraint collision. Assign it a real code
instead of deleting user data. Idempotent — matches zero rows once clean.
"""
from django.db import migrations


def backfill_empty_codes(apps, schema_editor):
    from mojo.helpers import crypto

    ShortLink = apps.get_model("shortlink", "ShortLink")
    for link in ShortLink.objects.filter(code=""):
        for _ in range(5):
            code = crypto.random_string(7, True, True, False)
            if not ShortLink.objects.filter(code=code).exists():
                link.code = code
                link.save(update_fields=["code"])
                break
        else:
            raise RuntimeError(
                f"Failed to generate a unique code for ShortLink id={link.pk}"
            )


class Migration(migrations.Migration):

    dependencies = [
        ("shortlink", "0003_shortlink_rendition"),
    ]

    operations = [
        migrations.RunPython(backfill_empty_codes, reverse_code=migrations.RunPython.noop),
    ]
