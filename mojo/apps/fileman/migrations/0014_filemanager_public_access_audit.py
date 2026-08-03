from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("fileman", "0013_file_shortlink_code_filerendition_shortlink_code"),
    ]

    operations = [
        migrations.AddField(
            model_name="filemanager",
            name="public_access_audit",
            field=models.JSONField(
                blank=True,
                default=None,
                editable=False,
                help_text="Internal versioned evidence for the effective public-access classification",
                null=True,
            ),
        ),
    ]
