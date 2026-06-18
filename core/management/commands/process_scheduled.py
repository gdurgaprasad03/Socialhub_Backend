import time
import logging
from django.core.management.base import BaseCommand
from django.utils import timezone
from core.models import Post
from core.tasks import process_post

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Process overdue scheduled posts synchronously without needing a running Celery worker."

    def add_arguments(self, parser):
        parser.add_argument(
            "--loop",
            action="store_true",
            help="Run in an infinite loop checking for scheduled posts every 10 seconds.",
        )

    def handle(self, *args, **options):
        loop = options.get("loop")
        self.stdout.write(self.style.SUCCESS("Starting scheduled post processor..."))
        
        try:
            while True:
                now = timezone.now()
                # Find posts that are SCHEDULED and whose scheduled_time is in the past (<= now)
                overdue_posts = Post.objects.filter(
                    status=Post.Status.SCHEDULED,
                    scheduled_time__lte=now,
                )
                
                count = overdue_posts.count()
                if count > 0:
                    self.stdout.write(f"Found {count} overdue scheduled post(s) to process.")
                    for post in overdue_posts:
                        self.stdout.write(f"Processing Post #{post.id} (scheduled for {post.scheduled_time})...")
                        try:
                            # Execute the Celery task synchronously in the current thread
                            result = process_post.apply(args=(post.id,))
                            
                            # Fetch updated post to print its actual final status
                            updated_post = Post.objects.get(id=post.id)
                            self.stdout.write(
                                self.style.SUCCESS(
                                    f"Finished Post #{post.id}. Database status is now: '{updated_post.status}'"
                                )
                            )
                        except Exception as e:
                            self.stdout.write(self.style.ERROR(f"Failed to process Post #{post.id}: {e}"))
                            Post.objects.filter(id=post.id).update(status=Post.Status.FAILED)
                
                if not loop:
                    break
                time.sleep(10)
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("\nStopping scheduled post processor."))
