# Generated for Instagram Login (direct) connection flow

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0012_remove_socialaccount_unique_user_platform_account_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='oauthstate',
            name='login_method',
            field=models.CharField(
                blank=True,
                help_text="Connection variant for a platform, e.g. 'instagram' for direct "
                          "Instagram Login vs the default Facebook-based flow",
                max_length=20,
            ),
        ),
    ]
