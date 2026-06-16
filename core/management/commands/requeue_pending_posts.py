"""
Management command: requeue_pending_posts

Re-queues all posts that are stuck in 'pending' or 'processing' status
with no platform results. This is useful when the Celery worker was down
and posts were not processed.

Usage:
    python manage.py requeue_pending_posts
    python manage.py requeue_pending_posts --post-id 81
    python manage.py requeue_pending_posts --dry-run
"""

import logging

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Post
from core.tasks import process_post

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Re-queues stuck pending/processing posts to be published via Celery."

    def add_arguments(self, parser):
        parser.add_argument(
            "--post-id",
            type=int,
            help="Re-queue a specific post by ID (optional). If not set, all stuck posts are re-queued.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show which posts would be re-queued without actually queuing them.",
        )

    def handle(self, *args, **options):
        post_id = options.get("post_id")
        dry_run = options.get("dry_run")

        if post_id:
            posts = Post.objects.filter(id=post_id)
            if not posts.exists():
                self.stderr.write(self.style.ERROR(f"Post with ID={post_id} not found."))
                return
        else:
            # Find all posts stuck in pending or processing with no platform results
            posts = Post.objects.filter(
                status__in=[Post.Status.PENDING, Post.Status.PROCESSING],
            ).filter(platform_results={})

        count = posts.count()
        if count == 0:
            self.stdout.write(self.style.SUCCESS("No stuck posts found. Everything looks good!"))
            return

        self.stdout.write(f"Found {count} stuck post(s) to re-queue:")

        for post in posts:
            age_minutes = (timezone.now() - post.created_at).total_seconds() / 60
            self.stdout.write(
                f"  >> Post ID={post.id} | status={post.status} | "
                f"accounts={post.target_accounts} | created {age_minutes:.0f}m ago"
            )

            if dry_run:
                continue

            try:
                # Reset status back to PENDING before re-queuing
                Post.objects.filter(id=post.id).update(
                    status=Post.Status.PENDING,
                    celery_task_id=None,
                )
                task = process_post.delay(post.id)
                Post.objects.filter(id=post.id).update(celery_task_id=task.id)
                self.stdout.write(
                    self.style.SUCCESS(f"     ✓ Re-queued as Celery task {task.id}")
                )
            except Exception as exc:
                self.stderr.write(
                    self.style.ERROR(f"     ✗ Failed to re-queue post {post.id}: {exc}")
                )
                logger.exception("requeue_pending_posts failed for post_id=%s", post.id)

        if dry_run:
            self.stdout.write(self.style.WARNING("\nDry-run mode — no posts were actually re-queued."))
        else:
            self.stdout.write(self.style.SUCCESS(f"\nDone! Re-queued {count} post(s)."))
