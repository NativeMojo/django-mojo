"""Idempotently rewrite legacy disable metadata into the new disable.* namespace.

Legacy keys (under metadata.protected): disable_warned, disable_warn_date, no_disable.
New shape (under metadata.protected.disable): exempt_from_auto_disable, warning.{sent_at, days_until_disable_at_send}.

Legacy keys are LEFT IN PLACE for one release of dual-read support; a follow-up
migration in the next release removes them.
"""
from django.db import migrations


def _migrate(entity):
    meta = entity.metadata or {}
    protected = meta.get("protected") or {}
    if not protected:
        return False

    disable_block = protected.get("disable") or {}
    already_migrated = (
        disable_block.get("warning")
        or "exempt_from_auto_disable" in disable_block
        or disable_block.get("reason")
    )
    if already_migrated:
        return False

    has_legacy = (
        protected.get("disable_warned") is not None
        or protected.get("disable_warn_date") is not None
        or protected.get("no_disable") is not None
    )
    if not has_legacy:
        return False

    if protected.get("no_disable") is True:
        disable_block["exempt_from_auto_disable"] = True
    if protected.get("disable_warned") is True:
        disable_block["warning"] = {
            "sent_at": protected.get("disable_warn_date"),
            "days_until_disable_at_send": None,
        }

    protected["disable"] = disable_block
    meta["protected"] = protected
    entity.metadata = meta
    entity.save(update_fields=["metadata"])
    return True


def forward(apps, schema_editor):
    User = apps.get_model("account", "User")
    Group = apps.get_model("account", "Group")
    for Model in (User, Group):
        qs = Model.objects.filter(metadata__has_key="protected")
        for entity in qs.iterator():
            _migrate(entity)


class Migration(migrations.Migration):

    dependencies = [
        ("account", "0040_publicmessage"),
    ]

    operations = [
        migrations.RunPython(forward, reverse_code=migrations.RunPython.noop),
    ]
