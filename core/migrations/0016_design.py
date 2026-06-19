from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0015_socialaccount_account_type'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='Design',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(blank=True, max_length=255)),
                ('source', models.CharField(choices=[('canva', 'Canva'), ('polotno', 'Polotno')], max_length=20)),
                ('image_url', models.URLField(max_length=1000)),
                ('cloudinary_public_id', models.CharField(blank=True, max_length=255)),
                ('canva_design_id', models.CharField(blank=True, max_length=255)),
                ('polotno_state', models.JSONField(blank=True, default=dict, help_text='Polotno editor JSON state — allows re-opening and editing the design')),
                ('width', models.IntegerField(default=0)),
                ('height', models.IntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='designs', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.AddIndex(
            model_name='design',
            index=models.Index(fields=['user', 'source'], name='core_design_user_id_source_idx'),
        ),
    ]
