import os

from celery import Celery
from celery.signals import task_prerun, task_postrun

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hub.settings")

app = Celery("hub")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()


@task_prerun.connect
def _close_old_connections_before_task(**kwargs):
    from django.db import close_old_connections
    close_old_connections()


@task_postrun.connect
def _close_old_connections_after_task(**kwargs):
    from django.db import close_old_connections
    close_old_connections()
