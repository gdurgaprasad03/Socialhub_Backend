from django.apps import AppConfig
import threading
import time
import logging


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        # Prevent the reloader process from spawning a duplicate thread
        import os
        if os.environ.get('RUN_MAIN') == 'true' or not os.environ.get('RUN_MAIN'):
            thread = threading.Thread(target=self.run_local_scheduler, daemon=True)
            thread.start()

    def run_local_scheduler(self):
        # Allow database connections to initialize
        time.sleep(5)
        logger = logging.getLogger(__name__)
        logger.info("Starting local background scheduled post runner thread...")
        
        while True:
            try:
                from django.utils import timezone
                from django.db.models import Q
                from django.db import connection
                from core.models import Post
                from core.tasks import process_post

                # Close stale connections to avoid Neon DB timeout issues
                connection.close()

                now = timezone.now()
                # Find posts that are PENDING, or SCHEDULED and overdue (<= now)
                overdue_posts = list(Post.objects.filter(
                    Q(status=Post.Status.PENDING) |
                    Q(status=Post.Status.SCHEDULED, scheduled_time__lte=now)
                ))

                if overdue_posts:
                    logger.info("Local Scheduler Thread: Found %d post(s) to process", len(overdue_posts))
                    for post in overdue_posts:
                        try:
                            # Process the post synchronously in the thread
                            process_post.apply(args=(post.id,))
                        except Exception as e:
                            logger.exception("Local Scheduler Thread failed for post %d", post.id)
            except Exception as e:
                logger.exception("Error in local scheduler thread loop")
            
            time.sleep(10)
