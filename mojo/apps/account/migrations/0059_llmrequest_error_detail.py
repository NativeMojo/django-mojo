from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('account', '0058_llmrequest_estimate'),
    ]

    operations = [
        migrations.AddField(
            model_name='llmrequest',
            name='error_detail',
            field=models.CharField(blank=True, default='', max_length=512),
        ),
    ]
