from django.db import migrations, models


def mark_existing_blocks_pending(apps, schema_editor):
    GeoLocatedIP = apps.get_model("account", "GeoLocatedIP")
    GeoLocatedIP.objects.filter(is_blocked=True).update(
        firewall_pending=True,
        firewall_sync_error="pending checked firewall reconciliation",
    )


class Migration(migrations.Migration):
    dependencies = [("account", "0053_llmcircuitbreaker_llmrequest")]

    operations = [
        migrations.AddField(
            model_name="geolocatedip",
            name="firewall_generation",
            field=models.PositiveBigIntegerField(db_index=True, default=0),
        ),
        migrations.AddField(
            model_name="geolocatedip",
            name="firewall_pending",
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.AddField(
            model_name="geolocatedip",
            name="firewall_sync_error",
            field=models.CharField(blank=True, default="", max_length=512),
        ),
        migrations.AddField(
            model_name="geolocatedip",
            name="firewall_observed_at",
            field=models.DateTimeField(blank=True, default=None, null=True),
        ),
        migrations.RunPython(
            mark_existing_blocks_pending, reverse_code=migrations.RunPython.noop),
    ]
