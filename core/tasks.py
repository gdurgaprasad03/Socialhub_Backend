import hashlib
import logging

from celery import shared_task
from django.utils import timezone

from .models import Post, SocialAccount
from .services.factory import get_service

logger = logging.getLogger(__name__)

VIDEO_FAILED_STATUSES = {"PROCESSING_FAILED", "FAILED", "DELETED", "ERROR", "EXPIRED"}
VIDEO_SUCCESS_STATUS = "AVAILABLE"
SYNC_VIDEO_PLATFORMS = {"facebook", "youtube"}


@shared_task(bind=True, max_retries=3, retry_backoff=60, retry_jitter=True)
def process_post(self, post_id):
    try:
        post = Post.objects.select_related("user").get(id=post_id)
    except Post.DoesNotExist:
        logger.warning("Post not found for processing: post_id=%s", post_id)
        return {"post_id": post_id, "status": "missing", "results": {}}

    if post.status == Post.Status.PUBLISHED:
        logger.info("Skipping already published post: post_id=%s", post_id)
        return {"post_id": post.id, "status": post.status, "results": post.platform_results}

    if not post.target_accounts:
        Post.objects.filter(id=post_id).update(
            status=Post.Status.DRAFT,
            platform_results={"error": "No target accounts configured. Saved as draft."},
            celery_task_id=None,
        )
        return {"post_id": post.id, "status": Post.Status.DRAFT, "results": {}}

    transitioned = Post.objects.filter(
        id=post_id,
        status__in=[Post.Status.PENDING, Post.Status.SCHEDULED, Post.Status.FAILED],
    ).update(status=Post.Status.PROCESSING)

    if not transitioned:
        logger.info(
            "Skipping post in non-processable state: post_id=%s status=%s",
            post.id, post.status,
        )
        return {"post_id": post.id, "status": post.status, "results": post.platform_results}

    # Fetch all target social accounts
    target_accounts = list(SocialAccount.objects.filter(
        id__in=post.target_accounts, user=post.user
    ))

    if not target_accounts:
        # No valid accounts found — save as draft so user can fix and retry
        Post.objects.filter(id=post_id).update(
            status=Post.Status.DRAFT,
            platform_results={"error": "Target accounts not found. Saved as draft."},
            celery_task_id=None,
        )
        return {"post_id": post.id, "status": Post.Status.DRAFT, "results": {}}

    try:
        if post.has_video:
            return _process_video_post(post, target_accounts)
        return _process_image_post(post, target_accounts)
    except Exception as exc:
        # Unexpected top-level failure — save as draft so work is not lost
        logger.exception(
            "Unexpected failure processing post — saving as draft: post_id=%s", post_id
        )
        Post.objects.filter(id=post_id).update(
            status=Post.Status.DRAFT,
            platform_results={"error": f"Processing interrupted: {str(exc)}. Saved as draft."},
            celery_task_id=None,
        )
        return {"post_id": post_id, "status": Post.Status.DRAFT, "results": {}}


def _get_post_for_account(post, account):
    """Apply account-specific content override and platform options."""
    content_overrides = post.content_overrides or {}
    override = content_overrides.get(str(account.id))
    if override:
        post.content = override

    platform_options = post.platform_options or {}
    account_options = platform_options.get(str(account.id), {})

    if account.platform == "instagram" and "post_type" in account_options:
        merged = dict(platform_options)
        merged["instagram"] = {"post_type": account_options["post_type"]}
        post.platform_options = merged

    if account.platform == "youtube" and "privacy" in account_options:
        merged = dict(platform_options)
        merged["youtube"] = {"privacy": account_options["privacy"]}
        post.platform_options = merged

    return post


def _process_video_post(post, target_accounts):
    results = {}
    upload_count = 0
    original_content = post.content
    original_platform_options = dict(post.platform_options or {})

    for account in target_accounts:
        account_key = str(account.id)
        processed_at = timezone.now().isoformat()

        post.content = original_content
        post.platform_options = dict(original_platform_options)

        try:
            post = _get_post_for_account(post, account)
            service = get_service(account.platform, post.user, account=account)
            service.account = account

            if account.platform in SYNC_VIDEO_PLATFORMS:
                response_data = service.create_post(post)
                platform_result = {
                    "success": True,
                    "platform": account.platform,
                    "account_id": account.account_id,
                    "display_name": account.display_name,
                    "processed_at": processed_at,
                }
                if isinstance(response_data, dict):
                    platform_result.update(response_data)
                results[account_key] = platform_result
                upload_count += 1
                continue

            if not hasattr(service, "upload_video"):
                raise NotImplementedError(f"{account.platform} does not support video upload.")

            video_urn = service.upload_video(post)
            poll_video_status.apply_async(
                args=[post.id, account_key, account.platform, video_urn],
                countdown=10,
            )

            results[account_key] = {
                "success": True,
                "platform": account.platform,
                "account_id": account.account_id,
                "display_name": account.display_name,
                "video_status": "processing",
                "video_urn": video_urn,
                "processed_at": processed_at,
            }
            upload_count += 1

        except Exception as exc:
            logger.exception(
                "Video upload failed: post_id=%s account=%s", post.id, account.display_name
            )
            results[account_key] = {
                "success": False,
                "platform": account.platform,
                "account_id": account.account_id,
                "display_name": account.display_name,
                "error": str(exc),
                "processed_at": processed_at,
            }

    post.content = original_content
    post.platform_options = original_platform_options

    async_accounts = [
        str(acc.id) for acc in target_accounts
        if acc.platform not in SYNC_VIDEO_PLATFORMS
    ]
    sync_results = {k: v for k, v in results.items() if k not in async_accounts}
    sync_all_success = all(r.get("success") for r in sync_results.values()) if sync_results else True

    if not async_accounts:
        final_status = Post.Status.PUBLISHED if sync_all_success else Post.Status.FAILED
        published_at = timezone.now() if sync_all_success else None
        Post.objects.filter(id=post.id).update(
            platform_results=results,
            status=final_status,
            published_at=published_at,
            celery_task_id=None,
        )
    else:
        final_status = Post.Status.PROCESSING if upload_count > 0 else Post.Status.FAILED
        Post.objects.filter(id=post.id).update(
            platform_results=results,
            status=final_status,
            celery_task_id=None,
        )

    # ── Track Usage ────────────────────────────────────────────────────────
    if final_status in [Post.Status.PUBLISHED, Post.Status.PARTIAL]:
        try:
            from billing.models import PostUsage
            usage = PostUsage.get_or_create_for_user(post.user)
            usage.increment()
        except Exception as e:
            logger.error("Failed to increment usage: %s", e)

    return {"post_id": post.id, "status": final_status, "results": results}


def _process_image_post(post, target_accounts):
    results = {}
    success_count = 0
    original_content = post.content
    original_platform_options = dict(post.platform_options or {})

    for account in target_accounts:
        account_key = str(account.id)
        processed_at = timezone.now().isoformat()

        post.content = original_content
        post.platform_options = dict(original_platform_options)

        try:
            post = _get_post_for_account(post, account)
            service = get_service(account.platform, post.user, account=account)
            service.account = account

            response_data = service.create_post(post)

            platform_result = {
                "success": True,
                "platform": account.platform,
                "account_id": account.account_id,
                "display_name": account.display_name,
                "processed_at": processed_at,
            }
            if isinstance(response_data, dict):
                if "post_urn" in response_data:
                    platform_result["post_urn"] = response_data["post_urn"]
                if "post_id" in response_data:
                    platform_result["post_id"] = response_data["post_id"]
                platform_result["response"] = response_data.get("body", response_data)
            else:
                platform_result["response"] = response_data

            results[account_key] = platform_result
            success_count += 1

        except Exception as exc:
            logger.exception(
                "Post publish failed: post_id=%s account=%s", post.id, account.display_name
            )
            results[account_key] = {
                "success": False,
                "platform": account.platform,
                "account_id": account.account_id,
                "display_name": account.display_name,
                "error": str(exc),
                "processed_at": processed_at,
            }

    post.content = original_content
    post.platform_options = original_platform_options

    total = len(target_accounts)
    if success_count == total:
        final_status = Post.Status.PUBLISHED
        published_at = timezone.now()
    elif success_count == 0:
        final_status = Post.Status.FAILED
        published_at = None
    else:
        final_status = Post.Status.PARTIAL
        published_at = timezone.now()

    Post.objects.filter(id=post.id).update(
        platform_results=results,
        status=final_status,
        published_at=published_at,
        celery_task_id=None,
    )

    # ── Track Usage ────────────────────────────────────────────────────────
    if final_status in [Post.Status.PUBLISHED, Post.Status.PARTIAL]:
        try:
            from billing.models import PostUsage
            usage = PostUsage.get_or_create_for_user(post.user)
            usage.increment()
        except Exception as e:
            logger.error("Failed to increment usage: %s", e)
    return {"post_id": post.id, "status": final_status, "results": results}


@shared_task(bind=True, max_retries=60, default_retry_delay=5)
def poll_video_status(self, post_id, account_key, platform, video_urn):
    try:
        post = Post.objects.select_related("user").get(id=post_id)
    except Post.DoesNotExist:
        logger.warning("Post not found during video polling: post_id=%s", post_id)
        return

    try:
        account = SocialAccount.objects.get(id=int(account_key), user=post.user)
    except SocialAccount.DoesNotExist:
        logger.warning("Account not found during video polling: account_key=%s", account_key)
        return

    try:
        service = get_service(platform, post.user, account=account)
        service.account = account

        video_status = service.get_video_asset_status(video_urn)
        logger.info(
            "Video asset status: post_id=%s account=%s asset=%s status=%s",
            post_id, account.display_name, video_urn, video_status,
        )

        if video_status == VIDEO_SUCCESS_STATUS:
            response_data = service.publish_video_post(post, video_urn)
            post_urn = response_data.get("post_urn") or response_data.get("post_id")

            updated_results = dict(post.platform_results)
            updated_results[account_key] = {
                "success": True,
                "platform": platform,
                "account_id": account.account_id,
                "display_name": account.display_name,
                "video_status": "published",
                "video_urn": video_urn,
                "post_urn": post_urn,
                "processed_at": timezone.now().isoformat(),
            }

            all_done = all(
                r.get("video_status") in ("published", "failed", "processing_failed")
                or not r.get("video_status")
                for r in updated_results.values()
                if isinstance(r, dict)
            )
            all_success = all(
                r.get("success") for r in updated_results.values() if isinstance(r, dict)
            )

            if all_done:
                final_status = Post.Status.PUBLISHED if all_success else Post.Status.PARTIAL
                Post.objects.filter(id=post_id).update(
                    platform_results=updated_results,
                    status=final_status,
                    published_at=timezone.now(),
                )
                
                # ── Track Usage ────────────────────────────────────────────────────
                try:
                    from billing.models import PostUsage
                    usage = PostUsage.get_or_create_for_user(post.user)
                    usage.increment()
                except Exception as e:
                    logger.error("Failed to increment usage: %s", e)
            else:
                Post.objects.filter(id=post_id).update(platform_results=updated_results)

        elif video_status in VIDEO_FAILED_STATUSES:
            logger.error(
                "Video processing failed: post_id=%s account=%s status=%s",
                post_id, account.display_name, video_status,
            )
            updated_results = dict(post.platform_results)
            updated_results[account_key] = {
                "success": False,
                "platform": platform,
                "display_name": account.display_name,
                "video_status": "processing_failed",
                "video_urn": video_urn,
                "error": f"{platform.title()} could not process the video (status: {video_status}).",
                "processed_at": timezone.now().isoformat(),
            }
            Post.objects.filter(id=post_id).update(
                platform_results=updated_results,
                status=Post.Status.FAILED,
            )

        else:
            raise self.retry(countdown=5)

    except self.MaxRetriesExceededError:
        logger.error("Video polling timed out: post_id=%s account=%s", post_id, account_key)
        updated_results = dict(post.platform_results)
        updated_results[account_key] = {
            "success": False,
            "video_status": "timeout",
            "error": "Video processing timed out after 5 minutes.",
            "processed_at": timezone.now().isoformat(),
        }
        Post.objects.filter(id=post_id).update(
            platform_results=updated_results,
            status=Post.Status.FAILED,
        )

    except Exception as exc:
        if any(s in str(exc) for s in VIDEO_FAILED_STATUSES):
            logger.error("Terminal video error, not retrying: %s", exc)
            return
        logger.exception(
            "Unexpected error polling video: post_id=%s account=%s", post_id, account_key
        )
        raise self.retry(exc=exc, countdown=5)


# ── Production Safety Net ──────────────────────────────────────────────────────

@shared_task(name="core.tasks.recover_stuck_posts")
def recover_stuck_posts():
    """
    Periodic task (runs every 5 min via Celery Beat) that automatically
    recovers posts stuck in PENDING or PROCESSING with no platform_results.

    This covers the case where:
      - The Celery worker was temporarily down when the post was created.
      - Redis was restarted and queued task messages were lost.
      - A worker process crashed before it could process the task.

    Only re-queues posts older than 5 minutes to avoid racing with a normally
    running worker that hasn't had time to start yet.
    """
    from datetime import timedelta
    cutoff = timezone.now() - timedelta(minutes=5)

    stuck = Post.objects.filter(
        status__in=[Post.Status.PENDING, Post.Status.PROCESSING],
        platform_results={},
        created_at__lte=cutoff,
    )

    count = stuck.count()
    if count == 0:
        logger.debug("recover_stuck_posts: no stuck posts found")
        return {"recovered": 0}

    logger.warning("recover_stuck_posts: found %d stuck post(s) — re-queuing", count)

    recovered = 0
    for post in stuck:
        try:
            Post.objects.filter(id=post.id).update(
                status=Post.Status.PENDING,
                celery_task_id=None,
            )
            task = process_post.delay(post.id)
            Post.objects.filter(id=post.id).update(celery_task_id=task.id)
            logger.info(
                "recover_stuck_posts: re-queued post_id=%d as task %s",
                post.id, task.id,
            )
            recovered += 1
        except Exception as exc:
            logger.exception(
                "recover_stuck_posts: failed to re-queue post_id=%d: %s",
                post.id, exc,
            )

    logger.info("recover_stuck_posts: recovered %d/%d post(s)", recovered, count)
    return {"recovered": recovered, "total_stuck": count}


# ── Token Refresh Safety Net ───────────────────────────────────────────────────

@shared_task(name="core.tasks.refresh_expiring_tokens")
def refresh_expiring_tokens():
    """
    Periodic task (runs every 6 hours via Celery Beat).
    Proactively refreshes OAuth tokens that are expiring within the next 3 days.

    This prevents silent post failures caused by expired access tokens.
    Platforms with non-expiring tokens (Facebook Page tokens, Twitter OAuth1) are skipped.
    """
    from datetime import timedelta

    now = timezone.now()
    refresh_window = now + timedelta(days=3)

    # Only platforms with refreshable, expiring tokens
    refreshable_platforms = {"linkedin", "youtube", "instagram"}

    expiring_accounts = SocialAccount.objects.filter(
        platform__in=refreshable_platforms,
        expires_at__isnull=False,
        expires_at__lte=refresh_window,
    ).select_related("user")

    count = expiring_accounts.count()
    if count == 0:
        logger.debug("refresh_expiring_tokens: no expiring tokens found")
        return {"refreshed": 0, "failed": 0}

    logger.info("refresh_expiring_tokens: found %d account(s) with expiring tokens", count)

    refreshed = 0
    failed = 0

    for account in expiring_accounts:
        try:
            service = get_service(account.platform, account.user, account=account)
            service.refresh_access_token()
            refreshed += 1
            logger.info(
                "refresh_expiring_tokens: refreshed %s account %s (user=%s)",
                account.platform, account.display_name, account.user_id,
            )
        except Exception as exc:
            failed += 1
            logger.warning(
                "refresh_expiring_tokens: failed to refresh %s account %s (user=%s): %s",
                account.platform, account.display_name, account.user_id, exc,
            )

    logger.info(
        "refresh_expiring_tokens: refreshed=%d failed=%d", refreshed, failed
    )
    return {"refreshed": refreshed, "failed": failed, "total": count}


# ── OAuth State Cleanup ────────────────────────────────────────────────────────

@shared_task(name="core.tasks.cleanup_expired_oauth_states")
def cleanup_expired_oauth_states():
    """
    Periodic task (runs daily) to delete expired OAuthState records.
    Keeps the database clean and prevents runaway growth.
    """
    from .models import OAuthState
    from datetime import timedelta

    cutoff = timezone.now() - timedelta(hours=1)  # 1 hour past expiry
    deleted_count, _ = OAuthState.objects.filter(expires_at__lte=cutoff).delete()
    logger.info("cleanup_expired_oauth_states: deleted %d expired record(s)", deleted_count)
    return {"deleted": deleted_count}