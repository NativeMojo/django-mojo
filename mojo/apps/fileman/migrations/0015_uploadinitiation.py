from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("account", "0049_bouncersignal_account_bou_ip_addr_e101ed_idx"),
        ("fileman", "0014_filemanager_public_access_audit"),
    ]

    operations = [
        migrations.CreateModel(
            name="UploadInitiation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created", models.DateTimeField(auto_now_add=True, db_index=True, editable=False)),
                ("modified", models.DateTimeField(auto_now=True, db_index=True)),
                ("key_digest", models.CharField(max_length=64)),
                ("fingerprint", models.CharField(max_length=64)),
                ("actor", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="file_upload_initiations", to="account.user")),
                ("file", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="upload_initiation", to="fileman.file")),
            ],
            options={
                "indexes": [models.Index(fields=["actor", "created"], name="fileman_upl_actor_i_23f033_idx")],
                "constraints": [models.UniqueConstraint(fields=("actor", "key_digest"), name="fileman_upload_actor_key_uniq")],
            },
        ),
    ]
