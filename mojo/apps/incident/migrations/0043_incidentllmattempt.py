import django.db.models.deletion
import mojo.models.rest
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("incident", "0042_mojosecdeployment_mojosecexecutionattempt_and_more")]

    operations = [
        migrations.CreateModel(
            name="IncidentLLMAttempt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("modified", models.DateTimeField(auto_now=True, db_index=True)),
                ("feature", models.CharField(db_index=True, max_length=32)),
                ("logical_key", models.CharField(max_length=64, unique=True)),
                ("state", models.CharField(choices=[("claimed", "Claimed"), ("queued", "Queued"), ("running", "Running"), ("retryable", "Retryable"), ("succeeded", "Succeeded"), ("terminal", "Terminal")], db_index=True, default="claimed", max_length=16)),
                ("prior_status", models.CharField(blank=True, default="", max_length=50)),
                ("event_id", models.BigIntegerField(blank=True, default=None, null=True)),
                ("ruleset_id", models.BigIntegerField(blank=True, default=None, null=True)),
                ("ticket_id", models.BigIntegerField(blank=True, default=None, null=True)),
                ("note_id", models.BigIntegerField(blank=True, default=None, null=True)),
                ("job_id", models.CharField(blank=True, db_index=True, default="", max_length=32)),
                ("attempt_count", models.PositiveIntegerField(default=0)),
                ("max_attempts", models.PositiveIntegerField(default=3)),
                ("lease_owner", models.CharField(blank=True, default="", max_length=64)),
                ("lease_expires_at", models.DateTimeField(blank=True, default=None, null=True)),
                ("retry_at", models.DateTimeField(blank=True, db_index=True, default=None, null=True)),
                ("finished_at", models.DateTimeField(blank=True, default=None, null=True)),
                ("error_code", models.CharField(blank=True, default="", max_length=64)),
                ("incident", models.ForeignKey(blank=True, default=None, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="llm_attempts", to="incident.incident")),
            ],
            options={"ordering": ["created", "pk"]},
            bases=(models.Model, mojo.models.rest.MojoModel),
        ),
        migrations.AddConstraint(
            model_name="incidentllmattempt",
            constraint=models.UniqueConstraint(condition=models.Q(("state__in", ("claimed", "queued", "running", "retryable"))), fields=("incident", "feature"), name="incident_one_active_llm_attempt"),
        ),
        migrations.AddConstraint(
            model_name="incidentllmattempt",
            constraint=models.UniqueConstraint(condition=models.Q(("state__in", ("claimed", "queued", "running", "retryable")), ("ticket_id__isnull", False)), fields=("ticket_id", "feature"), name="incident_one_active_ticket_llm_attempt"),
        ),
        migrations.AddIndex(model_name="incidentllmattempt", index=models.Index(fields=["state", "retry_at"], name="incident_llma_retry_idx")),
        migrations.AddIndex(model_name="incidentllmattempt", index=models.Index(fields=["incident", "created"], name="incident_llma_inc_idx")),
    ]
