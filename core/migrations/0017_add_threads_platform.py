from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0016_design'),
    ]

    operations = [
        migrations.AlterField(
            model_name='socialaccount',
            name='platform',
            field=models.CharField(
                choices=[
                    ('linkedin', 'LinkedIn'),
                    ('facebook', 'Facebook'),
                    ('instagram', 'Instagram'),
                    ('twitter', 'Twitter'),
                    ('youtube', 'YouTube'),
                    ('threads', 'Threads'),
                ],
                max_length=20,
            ),
        ),
        migrations.AlterField(
            model_name='oauthstate',
            name='platform',
            field=models.CharField(
                choices=[
                    ('linkedin', 'LinkedIn'),
                    ('facebook', 'Facebook'),
                    ('instagram', 'Instagram'),
                    ('twitter', 'Twitter'),
                    ('youtube', 'YouTube'),
                    ('threads', 'Threads'),
                ],
                max_length=20,
            ),
        ),
    ]
