"""One platform-wide Setting row per key (#8201).

`unique_together = ("key", "group")` never kept platform-wide keys distinct:
their group is NULL, and NULLs do not collide in a PostgreSQL unique index. So
a database can already hold two `group IS NULL` rows for one key, and readers
disagree about which one wins. This migration adds the partial unique
constraint `account_setting_global_key_uniq`, and first repairs what it would
otherwise fail on. It never stops a deploy (Ian's decision on #8201, option B:
"we cannot stop an upgrade"):

- **Every duplicated key keeps its oldest row**, the lowest pk, and the other
  copies are deleted. For identical copies (same `value`, `is_secret` and
  `mojo_secrets`) no value changes.
- **Conflicting copies** (any difference) are logged: the key, the row kept and
  the rows deleted, never a value. Before this migration a reader could get
  either copy, so the operator can check the kept value afterwards.
- **The cache**: after the migration commits, each repaired key is removed from
  the `settings:global` Redis hash, so no reader keeps serving a deleted copy.
  Redis being down never fails the migration; the hash's one-hour TTL is the
  backstop.

Secret rows always count as conflicting: each is encrypted with a random salt
and nonce under a password that includes its own pk, so two equal secrets
never store equal text.

The repair locks the settings table against writers before it reads a row,
and holds that lock until the migration commits, through the new unique index.
No write can change or add a copy between the read, the delete and the index,
so the index can never fail on a row inserted meanwhile. Reads continue.

Idempotent: once clean, the repair matches nothing.
"""
from django.db import migrations, models, transaction


IDENTITY_FIELDS = ("value", "is_secret", "mojo_secrets")
REDIS_GLOBAL_KEY = "settings:global"


def plan_global_dedup(rows):
    """Decide what to do with platform-wide Setting rows.

    `rows`: mappings with `pk`, `key` and the IDENTITY_FIELDS, in any order.
    Returns `(delete_pks, conflicts)`:
    - `delete_pks`: every copy of a duplicated key except its oldest (lowest
      pk), whether the copies agree or not;
    - `conflicts`: `[(key, kept_pk, [deleted_pk, ...])]`, sorted by key, for
      each key whose copies differ, for the log.
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
        removed = [row["pk"] for row in copies[1:]]
        delete_pks.extend(removed)
        identities = {tuple(row[field] for field in IDENTITY_FIELDS)
                      for row in copies}
        if len(identities) > 1:
            conflicts.append((key, copies[0]["pk"], removed))
    return delete_pks, conflicts


def repair_message(conflicts):
    """The log text for conflicting copies: keys and row ids, never a value."""
    lines = [
        "Migration account 0055 kept the oldest row of each platform-wide "
        "setting below and deleted the other rows, whose values differed. "
        "Before this migration either value could have been served; check "
        "that the kept value is the one you want.",
    ]
    for key, kept, deleted in conflicts:
        lines.append(
            f"  {key}: kept Setting row {kept}; deleted rows "
            f"{', '.join(str(pk) for pk in deleted)}")
    return "\n".join(lines)


def lock_out_writers(Setting, connection):
    """Hold off every settings writer until this migration commits.

    Without it the repair decides from a snapshot: a writer could change a copy
    after it was read, and the delete would discard that committed change
    (review 103840 on #8201), or insert a new copy that makes the unique index
    fail and stops the deploy. SHARE ROW EXCLUSIVE conflicts with the ROW
    EXCLUSIVE lock every INSERT, UPDATE and DELETE takes, so writes wait while
    reads go on. It is held to the end of the migration's transaction, through
    the delete and the new unique index.
    """
    if connection.vendor != "postgresql":
        return
    if not connection.in_atomic_block:
        raise RuntimeError(
            "migration account 0055 must run inside a transaction, so that "
            "settings writers can be held off while duplicates are repaired")
    with connection.cursor() as cursor:
        cursor.execute(
            "LOCK TABLE %s IN SHARE ROW EXCLUSIVE MODE"
            % connection.ops.quote_name(Setting._meta.db_table))


def clear_cached_globals(keys):
    """Remove the repaired keys from the platform-wide settings cache.

    Runs after the migration commits. Never raises: a deploy must not stop on
    Redis, and the hash's TTL rebuilds it from the database anyway.
    """
    if not keys:
        return
    try:
        from mojo.helpers.redis import get_connection
        client = get_connection()
        if client is not None:
            client.hdel(REDIS_GLOBAL_KEY, *keys)
    except Exception as exc:
        print(f"Migration account 0055: could not clear {len(keys)} repaired "
              f"key(s) from the settings cache ({type(exc).__name__}); they "
              "expire within the cache TTL.")


def _log(text):
    print(text)
    try:
        from mojo.helpers import logit
        logit.warning(text)
    except Exception:
        pass


def collapse_global_duplicates(apps, schema_editor):
    Setting = apps.get_model("account", "Setting")
    connection = schema_editor.connection
    lock_out_writers(Setting, connection)
    duplicated = (Setting.objects.filter(group__isnull=True)
                  .values("key").annotate(n=models.Count("pk"))
                  .filter(n__gt=1).values_list("key", flat=True))
    rows = list(Setting.objects.filter(group__isnull=True, key__in=list(duplicated))
                .values("pk", "key", *IDENTITY_FIELDS))
    if not rows:
        return
    delete_pks, conflicts = plan_global_dedup(rows)
    Setting.objects.filter(pk__in=delete_pks, group__isnull=True).delete()
    if conflicts:
        _log(repair_message(conflicts))
    keys = sorted({row["key"] for row in rows})
    transaction.on_commit(lambda: clear_cached_globals(keys), using=connection.alias)


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
