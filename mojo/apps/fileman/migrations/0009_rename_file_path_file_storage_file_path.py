# Generated by Django 4.2.21 on 2025-06-08 17:32

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('fileman', '0008_file_category'),
    ]

    operations = [
        migrations.RenameField(
            model_name='file',
            old_name='file_path',
            new_name='storage_file_path',
        ),
    ]
