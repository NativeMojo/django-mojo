"""One platform-wide Setting row per key (#8201).

`unique_together = ("key", "group")` never kept platform-wide keys distinct:
their group is NULL, and NULLs do not collide in a PostgreSQL unique index. So
a database can already hold two `group IS NULL` rows for one key, and readers
disagree about which one wins. This migration adds the partial unique
constraint `account_setting_global_key_uniq`, and first repairs what it would
otherwise fail on:

- **Identical copies** (same `value`, `is_secret` and `mojo_secrets`) collapse
  to the lowest pk. No value changes, so nothing cached goes stale.
- **Conflicting copies** stop the migration. It names each key and its row
  ids, never a value, and changes nothing: a person decides which row stays.
  Every automatic choice could silently change a live setting.

Secret rows always count as conflicting: each is encrypted with a random salt
and nonce under a password that includes its own pk, so two equal secrets
never store equal text. That is the safe direction.

Idempotent: once clean, the repair matches nothing.
"""
from django.db import migrations, models


IDENTITY_FIELDS = ("value", "is_secret", "mojo_secrets")


class ConflictingGlobalSettings(RuntimeError):
    """Platform-wide duplicates with different values; a person must choose."""


def plan_global_dedup(rows):
    """Decide what to do with platform-wide Setting rows.

    `rows`: mappings with `pk`, `key` and the IDENTITY_FIELDS, in any order.
    Returns `(delete_pks, conflicts)`:
    - `delete_pks`: the rows to delete, every identical copy but the lowest pk;
    - `conflicts`: `[(key, [pk, ...])]`, sorted by key, for each key whose
      copies differ. No row of a conflicting key is ever deleted.
    """
    by_key = {}
    for row in rows:
        by_key.setdefault(row["key"], []).append(row)
    delete_pks = []
    conflicts = []
    for key in sorted(by_key):
        copies = sorted(by_key[key], key=lambda row: row["pk"])
        if len(copies) < 2:
            continue
        identities = {tuple(row[field] for field in IDENTITY_FIELDS)
                      for row in copies}
        if len(identities) == 1:
            delete_pks.extend(row["pk"] for row in copies[1:])
        else:
            conflicts.append((key, [row["pk"] for row in copies]))
    return delete_pks, conflicts


def conflict_message(conflicts):
    lines = [
        "Migration account 0055 stopped: these platform-wide settings have "
        "more than one row with different values, and only one row per key "
        "may remain. Nothing was changed.",
        "",
    ]
    for key, pks in conflicts:
        lines.append(f"  {key}: Setting rows {', '.join(str(pk) for pk in pks)}")
    lines += [
        "",
        "For each key, decide which row is correct and delete the others, "
        "for example in `manage.py shell`:",
        "  Setting.objects.filter(pk__in=[<unwanted ids>], group=None).delete()",
        "Then run migrate again.",
    ]
    return "\n".join(lines)


def collapse_global_duplicates(apps, schema_editor):
    Setting = apps.get_model("account", "Setting")
    duplicated = (Setting.objects.filter(group__isnull=True)
                  .values("key").annotate(n=models.Count("pk"))
                  .filter(n__gt=1).values_list("key", flat=True))
    rows = list(Setting.objects.filter(group__isnull=True, key__in=list(duplicated))
                .values("pk", "key", *IDENTITY_FIELDS))
    if not rows:
        return
    delete_pks, conflicts = plan_global_dedup(rows)
    if conflicts:
        raise ConflictingGlobalSettings(conflict_message(conflicts))
    Setting.objects.filter(pk__in=delete_pks, group__isnull=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("account", "0054_geolocatedip_firewall_reconciliation"),
    ]

    operations = [
        migrations.RunPython(collapse_global_duplicates,
                             reverse_code=migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="setting",
            constraint=models.UniqueConstraint(
                condition=models.Q(("group__isnull", True)),
                fields=("key",),
                name="account_setting_global_key_uniq"),
        ),
    ]
