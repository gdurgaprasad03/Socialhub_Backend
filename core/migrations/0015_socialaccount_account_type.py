from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0014_add_post_idempotency_key'),
    ]

    operations = [
        migrations.AddField(
            model_name='socialaccount',
            name='account_type',
            field=models.CharField(
                choices=[
                    ('personal', 'Personal'),
                    ('page', 'Page / Business'),
                    ('organization', 'Organization'),
                    ('channel', 'Channel'),
                ],
                default='personal',
                help_text='Whether this is a personal profile, business page, organization, or channel',
                max_length=20,
            ),
        ),
    ]
