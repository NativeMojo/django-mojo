import mojo.models.rest
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("account", "0052_oauthclient_oauthgrant_oauthcode")]

    operations = [
        migrations.CreateModel(
            name="LLMCircuitBreaker",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("modified", models.DateTimeField(auto_now=True, db_index=True)),
                ("provider", models.CharField(max_length=32)),
                ("credential_fingerprint", models.CharField(max_length=64)),
                ("state", models.CharField(db_index=True, default="closed", max_length=16)),
                ("generation", models.PositiveBigIntegerField(default=0)),
                ("failure_count", models.PositiveIntegerField(default=0)),
                ("error_code", models.CharField(blank=True, default="", max_length=64)),
                ("opened_until", models.DateTimeField(blank=True, db_index=True, default=None, null=True)),
                ("half_open_owner", models.CharField(blank=True, default="", max_length=64)),
                ("half_open_expires_at", models.DateTimeField(blank=True, default=None, null=True)),
            ],
            bases=(models.Model, mojo.models.rest.MojoModel),
        ),
        migrations.CreateModel(
            name="LLMRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("modified", models.DateTimeField(auto_now=True, db_index=True)),
                ("finished_at", models.DateTimeField(blank=True, default=None, null=True)),
                ("feature", models.CharField(db_index=True, max_length=32)),
                ("operation", models.CharField(max_length=64)),
                ("provider", models.CharField(db_index=True, max_length=32)),
                ("model", models.CharField(max_length=128)),
                ("credential_fingerprint", models.CharField(db_index=True, max_length=64)),
                ("policy_hash", models.CharField(db_index=True, max_length=64)),
                ("provider_request_id", models.CharField(blank=True, default="", max_length=128)),
                ("status", models.CharField(choices=[("started", "Started"), ("succeeded", "Succeeded"), ("failed", "Failed"), ("blocked", "Blocked"), ("unknown", "Unknown")], db_index=True, default="started", max_length=16)),
                ("error_code", models.CharField(blank=True, default="", max_length=64)),
                ("input_tokens", models.PositiveIntegerField(default=0)),
                ("output_tokens", models.PositiveIntegerField(default=0)),
                ("cache_read_input_tokens", models.PositiveIntegerField(default=0)),
                ("cache_creation_input_tokens", models.PositiveIntegerField(default=0)),
                ("reserved_tokens", models.PositiveIntegerField(default=0)),
                ("duration_ms", models.PositiveIntegerField(default=0)),
                ("job_id", models.CharField(blank=True, db_index=True, default="", max_length=64)),
                ("incident_id", models.BigIntegerField(blank=True, db_index=True, default=None, null=True)),
                ("conversation_id", models.BigIntegerField(blank=True, db_index=True, default=None, null=True)),
                ("file_id", models.BigIntegerField(blank=True, default=None, null=True)),
            ],
            options={"ordering": ["-created"]},
            bases=(models.Model, mojo.models.rest.MojoModel),
        ),
        migrations.AddConstraint(
            model_name="llmcircuitbreaker",
            constraint=models.UniqueConstraint(fields=("provider", "credential_fingerprint"), name="account_llm_breaker_credential_uniq"),
        ),
        migrations.AddIndex(model_name="llmcircuitbreaker", index=models.Index(fields=["provider", "state"], name="account_llmb_state_idx")),
        migrations.AddIndex(model_name="llmrequest", index=models.Index(fields=["provider", "credential_fingerprint", "created"], name="account_llmr_provider_idx")),
        migrations.AddIndex(model_name="llmrequest", index=models.Index(fields=["feature", "status", "created"], name="account_llmr_feature_idx")),
    ]
