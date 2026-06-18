import base64
import json
import logging
import os
import uuid
from urllib.parse import urlencode
from datetime import datetime, timedelta
from celery import current_app
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import OperationalError
from django.db.models import Count
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.exceptions import ParseError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from .models import OAuthState, Post, SocialAccount, PostingSchedule
from .serializers import LoginSerializer, PostSerializer, RegisterSerializer, SocialAccountSerializer, PostingScheduleSerializer
from .services.oauth import (
    OAuthConfigurationError,
    SocialPlatformError,
    build_linkedin_auth_url,
    build_meta_auth_url,
    build_instagram_login_auth_url,
    build_twitter_auth_url,
    build_twitter_oauth1_auth_url,
    build_youtube_auth_url,
    build_social_account_data,
    build_instagram_login_account_data,
    exchange_linkedin_code,
    exchange_meta_code,
    exchange_instagram_login_code,
    exchange_twitter_code,
    exchange_twitter_oauth1_code,
    exchange_youtube_code,
    fetch_linkedin_profile,
    fetch_meta_accounts,
    fetch_instagram_login_profile,
    fetch_twitter_profile,
    fetch_twitter_request_token,
    fetch_youtube_profile,
    generate_state,
    generate_code_verifier,
    oauth_expiry,
)
from .services.factory import get_service
from .tasks import process_post

logger = logging.getLogger(__name__)


def _build_frontend_redirect(base_url, params):
    if not base_url:
        return None
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}{urlencode(params)}"


def _get_next_queue_slot(user):
    schedules = list(PostingSchedule.objects.filter(
        user=user).order_by("day_of_week", "time"))
    if not schedules:
        return None

    last_post = Post.objects.filter(
        user=user,
        status__in=[Post.Status.SCHEDULED,
                    Post.Status.PENDING, Post.Status.PROCESSING],
        scheduled_time__isnull=False
    ).order_by("-scheduled_time").first()

    now = timezone.now()
    reference_time = last_post.scheduled_time if last_post else now
    if reference_time < now:
        reference_time = now

    for _ in range(14):
        ref_day = reference_time.weekday()
        ref_time = reference_time.time()
        for schedule in schedules:
            if schedule.day_of_week > ref_day or (
                schedule.day_of_week == ref_day and schedule.time > ref_time
            ):
                target_date = reference_time.date() + timedelta(
                    days=(schedule.day_of_week - ref_day)
                )
                dt = datetime.combine(target_date, schedule.time)
                return timezone.make_aware(dt)
        days_ahead = 7 - reference_time.weekday()
        reference_time = (reference_time + timedelta(days=days_ahead)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    return None


def _save_uploaded_files(files):
    saved_urls = []
    for f in files:
        file_path = default_storage.save(f"post_media/{f.name}", f)
        url = settings.MEDIA_URL + file_path
        saved_urls.append(url)
    return saved_urls


# Map common image/video mime types to file extensions for base64 uploads.
_MIME_EXT = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
    "image/gif": "gif", "image/webp": "webp", "image/bmp": "bmp",
    "video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm",
}


def _save_base64_media(data_uri):
    """Decode a `data:<mime>;base64,<payload>` URI, store it under MEDIA, and
    return its /media URL. Returns None if the value isn't a decodable data URI."""
    if not isinstance(data_uri, str) or not data_uri.startswith("data:"):
        return None
    try:
        header, _, payload = data_uri.partition(",")
        if "base64" not in header or not payload:
            return None
        mime = header[len("data:"):].split(";")[0].strip().lower()
        ext = _MIME_EXT.get(mime, mime.split("/")[-1] or "bin")
        raw = base64.b64decode(payload)
    except Exception:
        logger.warning("Skipping undecodable base64 media value")
        return None
    file_path = default_storage.save(
        f"post_media/{uuid.uuid4().hex}.{ext}", ContentFile(raw)
    )
    return settings.MEDIA_URL + file_path


def _normalize_image_inputs(values):
    """Turn a mixed list of image inputs into a list of stored/usable URLs.

    Each value may be a base64 data URI (decoded + stored), an http(s)/`/media`
    URL (kept as-is), or junk (skipped). `blob:` URLs only exist in the browser
    and cannot be resolved server-side, so they're dropped with a warning."""
    urls = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        if value.startswith("data:"):
            saved = _save_base64_media(value)
            if saved:
                urls.append(saved)
        elif value.startswith(("http://", "https://", "/media/")):
            urls.append(value)
        elif value.startswith("blob:"):
            logger.warning("Dropping blob: URL — not resolvable server-side")
        # anything else is ignored
    return urls


def _collect_request_images(request):
    """Gather every image the client sent into a clean list of stored URLs.

    Looks in three places the frontend uses: real file uploads (request.FILES),
    string values appended under "media_files" (base64/URLs as form fields), and
    the JSON "images" field. Base64 data URIs are decoded and stored; URLs pass
    through. Returns (urls, media_provided) — media_provided is False only when
    the request carried no image input at all, so a partial PUT can leave the
    existing images untouched instead of clearing them."""
    urls = []
    provided = False

    uploaded_files = request.FILES.getlist("media_files")
    if uploaded_files:
        provided = True
        logger.info("CreatePost: received %d uploaded file(s)",
                    len(uploaded_files))
        urls += _save_uploaded_files(uploaded_files)

    # The frontend also appends non-file image values (base64 data URIs / URLs)
    # under "media_files" as plain form fields. Read them from request.data
    # (DRF's already-parsed payload) rather than request.POST, which can raise
    # on a multipart request whose stream DRF has already consumed. request.data
    # merges form fields and files, so filter out the file objects we handled.
    media_values = (
        request.data.getlist("media_files")
        if hasattr(request.data, "getlist") else []
    )
    string_media = [m for m in media_values if isinstance(m, str)]
    if string_media:
        provided = True
        urls += _normalize_image_inputs(string_media)

    raw_images = request.data.get("images", None)
    if raw_images not in (None, ""):
        provided = True
        existing = []
        if isinstance(raw_images, list):
            existing = raw_images
        elif isinstance(raw_images, str) and raw_images.strip():
            try:
                parsed = json.loads(raw_images)
                if isinstance(parsed, list):
                    existing = parsed
            except (ValueError, TypeError):
                existing = []
        urls += _normalize_image_inputs(existing)

    return urls, provided


# ──────────────────────────────────────────────
# POSTS
# ──────────────────────────────────────────────

class CreatePost(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk=None):
        try:
            if pk:
                try:
                    post = Post.objects.get(pk=pk, user=request.user)
                except Post.DoesNotExist:
                    return Response({"error": "Post not found"}, status=status.HTTP_404_NOT_FOUND)
                return Response(PostSerializer(post).data)
            posts = Post.objects.filter(user=request.user)
            return Response(PostSerializer(posts, many=True).data)
        except Exception as exc:
            logger.exception("Error fetching posts")
            return Response({"error": "Unable to fetch posts", "details": str(exc)},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def post(self, request, pk=None):
        if pk:
            return Response({"error": "Method not allowed with post ID"},
                            status=status.HTTP_405_METHOD_NOT_ALLOWED)
        try:
            # ── Check Plan Limits ──────────────────────────────────────────
            is_draft = request.data.get("is_draft", False)
            if not is_draft:
                from billing.views import get_or_create_subscription
                from billing.models import PostUsage, UserSubscription
                sub = get_or_create_subscription(request.user)
                usage = PostUsage.get_or_create_for_user(request.user)
                if sub.plan.posts_limit != -1 and usage.posts_used >= sub.plan.posts_limit:
                    return Response(
                        {"error": "Monthly post limit reached. Please upgrade your plan."},
                        status=status.HTTP_403_FORBIDDEN
                    )

                # Check Expiration.
                # A PAID active plan is renewed externally by Razorpay (the
                # subscription.charged webhook pushes current_period_end forward),
                # so a lagging date shouldn't lock out a valid paid subscriber.
                # Free trials and non-active plans (cancelled grace period,
                # past-due, etc.) are still gated by current_period_end.
                now = timezone.now()
                is_paid_active = (
                    sub.status == UserSubscription.Status.ACTIVE
                    and not sub.plan.is_free
                )
                if (not is_paid_active
                        and sub.current_period_end
                        and sub.current_period_end < now):
                    return Response(
                        {"error": "Your subscription or free trial has expired. Please upgrade to continue posting."},
                        status=status.HTTP_403_FORBIDDEN
                    )

                # Check Daily Limit
                is_new_day = usage.last_post_at is None or usage.last_post_at.date() < now.date()
                effective_daily_used = 0 if is_new_day else usage.daily_posts_used

                if sub.plan.posts_per_day != -1 and effective_daily_used >= sub.plan.posts_per_day:
                    return Response(
                        {"error": f"Daily limit reached ({sub.plan.posts_per_day} posts/day). Please try again tomorrow or upgrade."},
                        status=status.HTTP_403_FORBIDDEN
                    )

            # ── Handle media (file uploads, base64 data URIs, URLs) ────────
            all_images, _ = _collect_request_images(request)

            # ── Duplicate post prevention (idempotency check) ─────────────
            import hashlib
            import json as _json
            raw_ta = request.data.get("target_accounts", [])
            if isinstance(raw_ta, str):
                try:
                    raw_ta = _json.loads(raw_ta)
                except Exception:
                    raw_ta = []
            # Guard: if the frontend sent a bare integer (or any non-list scalar)
            # wrap it so sorted() doesn't raise 'int object is not iterable'.
            if not isinstance(raw_ta, (list, tuple)):
                raw_ta = [raw_ta] if raw_ta is not None else []
            _idem_src = f"{request.user.id}:{request.data.get('content', '')}:{sorted(raw_ta)}:{request.data.get('scheduled_time', '')}"
            idempotency_key = hashlib.sha256(_idem_src.encode()).hexdigest()[:64]
            from datetime import timedelta
            recent_cutoff = timezone.now() - timedelta(seconds=60)
            duplicate = Post.objects.filter(
                user=request.user,
                idempotency_key=idempotency_key,
                created_at__gte=recent_cutoff,
            ).first()
            if duplicate and not request.data.get("is_draft", False):
                logger.info("Duplicate post rejected: user=%s idem_key=%s", request.user.id, idempotency_key)
                return Response(
                    {
                        "error": "Duplicate post detected. This post was already submitted within the last 60 seconds.",
                        "duplicate_post_id": duplicate.id,
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            # ── Build data dict and parse target_accounts ──────────────────
            if hasattr(request.data, 'dict'):
                # If it's a QueryDict (multipart form), standard .get() drops list items.
                data = request.data.dict()
                raw_target_accounts = request.data.getlist("target_accounts")
            else:
                # If it's standard dict (JSON)
                data = dict(request.data)
                raw_target_accounts = request.data.get("target_accounts", [])

                if isinstance(raw_target_accounts, str) and raw_target_accounts.strip():
                    try:
                        raw_target_accounts = json.loads(raw_target_accounts)
                    except (ValueError, TypeError):
                        raw_target_accounts = []

            data["images"] = all_images
            data["target_accounts"] = raw_target_accounts

            serializer = PostSerializer(
                data=data, context={"request": request})
            serializer.is_valid(raise_exception=True)

            scheduled_time = serializer.validated_data.get("scheduled_time")
            is_draft = request.data.get("is_draft", False)
            add_to_queue = request.data.get("add_to_queue", False)

            if add_to_queue and not scheduled_time and not is_draft:
                scheduled_time = _get_next_queue_slot(request.user)
                serializer.validated_data["scheduled_time"] = scheduled_time

            if is_draft:
                initial_status = Post.Status.DRAFT
            else:
                initial_status = Post.Status.SCHEDULED if scheduled_time else Post.Status.PENDING

            post = serializer.save(user=request.user, status=initial_status)
            # Save idempotency key to prevent duplicate submissions
            if idempotency_key:
                Post.objects.filter(id=post.id).update(idempotency_key=idempotency_key)
            logger.info("CreatePost: saved post id=%d target_accounts=%s",
                        post.id, post.target_accounts)

            message = "Draft saved successfully"
            if not is_draft:
                try:
                    if scheduled_time:
                        task_result = process_post.apply_async(
                            (post.id,), eta=scheduled_time)
                        message = "Post scheduled successfully"
                    else:
                        task_result = process_post.delay(post.id)
                        message = "Post queued successfully"
                    post.celery_task_id = task_result.id
                    post.save(update_fields=["celery_task_id", "updated_at"])
                except Exception as broker_exc:
                    # Celery broker (Redis) is temporarily unavailable.
                    # The post is safely saved in the database as PENDING.
                    # It will be re-queued automatically once the worker is back,
                    # or manually via: python manage.py requeue_pending_posts
                    logger.error(
                        "Celery broker unavailable — post id=%d saved as PENDING "
                        "and will be retried: %s", post.id, broker_exc
                    )
                    message = (
                        "Post saved successfully but could not be queued immediately "
                        "due to a background worker issue. It will be published shortly."
                    )

            return Response(
                {"message": message, "post": PostSerializer(post).data},
                status=status.HTTP_201_CREATED
            )

        except (serializers.ValidationError, ParseError) as exc:
            detail = getattr(exc, 'detail', str(exc))
            return Response(detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Error creating post")
            return Response({"error": "Unable to create post", "details": str(exc)},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def put(self, request, pk=None):
        try:
            if not pk:
                return Response({"error": "Method not allowed without post ID"},
                                status=status.HTTP_405_METHOD_NOT_ALLOWED)
            try:
                post = Post.objects.get(pk=pk, user=request.user)
            except Post.DoesNotExist:
                return Response({"error": "Post not found"}, status=status.HTTP_404_NOT_FOUND)

            # Normalize media (file uploads, base64 data URIs, URLs) the same way
            # as create. Only override `images` when the request actually carried
            # media, so a text-only edit doesn't wipe existing images.
            if hasattr(request.data, 'dict'):
                data = request.data.dict()
            else:
                data = dict(request.data)
            all_images, media_provided = _collect_request_images(request)
            if media_provided:
                data["images"] = all_images
            else:
                data.pop("images", None)

            serializer = PostSerializer(
                post, data=data, partial=True, context={"request": request})
            serializer.is_valid(raise_exception=True)
            updated_post = serializer.save()
            if post.celery_task_id and post.status in [Post.Status.SCHEDULED, Post.Status.PENDING]:
                current_app.control.revoke(
                    post.celery_task_id, terminate=False)
            is_draft = request.data.get(
                "is_draft", updated_post.status == Post.Status.DRAFT)

            if not is_draft and updated_post.status == Post.Status.DRAFT:
                # User is trying to publish a draft — check limits
                from billing.views import get_or_create_subscription
                from billing.models import PostUsage
                sub = get_or_create_subscription(request.user)
                usage = PostUsage.get_or_create_for_user(request.user)
                if sub.plan.posts_limit != -1 and usage.posts_used >= sub.plan.posts_limit:
                    return Response(
                        {"error": "Monthly post limit reached. Please upgrade your plan."},
                        status=status.HTTP_403_FORBIDDEN
                    )

                # Check Daily Limit
                from django.utils import timezone
                now = timezone.now()
                is_new_day = usage.last_post_at is None or usage.last_post_at.date() < now.date()
                effective_daily_used = 0 if is_new_day else usage.daily_posts_used

                if sub.plan.posts_per_day != -1 and effective_daily_used >= sub.plan.posts_per_day:
                    return Response(
                        {"error": f"Daily limit reached ({sub.plan.posts_per_day} posts/day). Please try again tomorrow or upgrade."},
                        status=status.HTTP_403_FORBIDDEN
                    )

            if is_draft:
                updated_post.status = Post.Status.DRAFT
                updated_post.celery_task_id = None
                updated_post.save(
                    update_fields=["status", "celery_task_id", "updated_at"])
            elif updated_post.status in [Post.Status.SCHEDULED, Post.Status.PENDING,
                                         Post.Status.FAILED, Post.Status.DRAFT]:
                if updated_post.scheduled_time and updated_post.scheduled_time > timezone.now():
                    task_result = process_post.apply_async(
                        (updated_post.id,), eta=updated_post.scheduled_time)
                    updated_post.status = Post.Status.SCHEDULED
                else:
                    task_result = process_post.delay(updated_post.id)
                    updated_post.status = Post.Status.PENDING
                updated_post.celery_task_id = task_result.id
                updated_post.save(
                    update_fields=["status", "celery_task_id", "updated_at"])
            return Response(PostSerializer(updated_post).data)
        except (serializers.ValidationError, ParseError) as exc:
            detail = getattr(exc, 'detail', str(exc))
            return Response(detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Error updating post")
            return Response({"error": "Unable to update post", "details": str(exc)},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def delete(self, request, pk=None):
        try:
            if not pk:
                return Response({"error": "Method not allowed without post ID"},
                                status=status.HTTP_405_METHOD_NOT_ALLOWED)
            try:
                post = Post.objects.get(pk=pk, user=request.user)
            except Post.DoesNotExist:
                return Response({"error": "Post not found"}, status=status.HTTP_404_NOT_FOUND)

            if post.status in [Post.Status.PUBLISHED, Post.Status.PARTIAL]:
                for account_key, result in (post.platform_results or {}).items():
                    if result.get("success") and not result.get("deleted"):
                        post_urn = result.get(
                            "post_urn") or result.get("post_id")
                        if post_urn:
                            try:
                                account = SocialAccount.objects.get(
                                    id=int(account_key), user=request.user
                                )
                                service = get_service(
                                    account.platform, request.user, account=account)
                                service.account = account
                                service.delete_post(post_urn)
                            except Exception as exc:
                                logger.warning(
                                    "Optional remote delete failed: post_id=%s account=%s error=%s",
                                    pk, account_key, exc
                                )
            post.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except Exception as exc:
            logger.exception("Error deleting post")
            return Response({"error": "Unable to delete post", "details": str(exc)},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class DeletePublishedPostView(APIView):
    permission_classes = [IsAuthenticated]

    def delete(self, request, pk, account_id):
        try:
            post = Post.objects.get(pk=pk, user=request.user)
        except Post.DoesNotExist:
            return Response({"error": "Post not found"}, status=status.HTTP_404_NOT_FOUND)

        if post.status not in [Post.Status.PUBLISHED, Post.Status.PARTIAL]:
            return Response(
                {"error": "Only published or partially published posts can be deleted."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        account_key = str(account_id)
        platform_result = post.platform_results.get(account_key)
        if not platform_result or not platform_result.get("success"):
            return Response(
                {"error": f"No successful publish record found for account: {account_id}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        post_urn = platform_result.get(
            "post_urn") or platform_result.get("post_id")
        if not post_urn:
            return Response(
                {"error": "No post URN stored. Cannot delete remotely."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            account = SocialAccount.objects.get(
                id=int(account_id), user=request.user)
            service = get_service(
                account.platform, request.user, account=account)
            service.account = account
            service.delete_post(post_urn)
        except SocialAccount.DoesNotExist:
            return Response({"error": "Account not found"}, status=status.HTTP_404_NOT_FOUND)
        except SocialPlatformError as exc:
            return Response(
                {"error": f"Failed to delete from platform: {str(exc)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except Exception:
            logger.exception(
                "Unexpected error deleting from platform: post_id=%s account=%s", pk, account_id)
            return Response(
                {"error": "Unexpected error deleting from platform."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        updated_results = post.platform_results.copy()
        updated_results[account_key]["deleted"] = True
        post.platform_results = updated_results

        all_deleted = all(
            v.get("deleted") for v in updated_results.values() if v.get("success")
        )
        if all_deleted:
            post.delete()
            return Response({"message": "Post deleted from platform and removed locally."})

        post.save(update_fields=["platform_results", "updated_at"])
        return Response({
            "message": "Post deleted from platform.",
            "platform_results": updated_results
        })


class SchedulingView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # 1. Get recurring slots
        schedules = PostingSchedule.objects.filter(user=request.user)
        slots_data = PostingScheduleSerializer(schedules, many=True).data

        # 2. Get actual upcoming posts
        posts = Post.objects.filter(
            user=request.user,
            status__in=[Post.Status.SCHEDULED,
                        Post.Status.PENDING, Post.Status.PROCESSING],
        ).order_by("scheduled_time")
        posts_data = PostSerializer(posts, many=True).data

        return Response({
            "slots": slots_data,
            "scheduled_posts": posts_data
        })

    def post(self, request):
        """Add a new recurring posting slot."""
        serializer = PostingScheduleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(user=request.user)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def delete(self, request, pk=None):
        """Delete one or all recurring posting slots."""
        if not pk:
            PostingSchedule.objects.filter(user=request.user).delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        PostingSchedule.objects.filter(user=request.user, pk=pk).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class DashboardStatsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            user_posts = Post.objects.filter(user=request.user)
            total_posts = user_posts.count()
            status_counts = user_posts.values(
                "status").annotate(count=Count("status"))
            detailed_status = {item["status"]: item["count"]
                               for item in status_counts}
            connected_accounts = SocialAccount.objects.filter(
                user=request.user).count()
            return Response({
                "total_posts": total_posts,
                "scheduled_posts": (
                    detailed_status.get(Post.Status.SCHEDULED, 0)
                    + detailed_status.get(Post.Status.PENDING, 0)
                    + detailed_status.get(Post.Status.PROCESSING, 0)
                ),
                "published_posts": detailed_status.get(Post.Status.PUBLISHED, 0),
                "partial_posts": detailed_status.get(Post.Status.PARTIAL, 0),
                "failed_posts": detailed_status.get(Post.Status.FAILED, 0),
                "detailed_status": detailed_status,
                "connected_accounts": connected_accounts,
            })
        except Exception as exc:
            logger.exception("Error fetching dashboard stats")
            return Response({"error": "Unable to fetch dashboard stats", "details": str(exc)},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ──────────────────────────────────────────────
# SOCIAL ACCOUNTS
# ──────────────────────────────────────────────

class SocialAccountView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, pk=None):
        try:
            if pk:
                try:
                    account = SocialAccount.objects.get(
                        pk=pk, user=request.user)
                except SocialAccount.DoesNotExist:
                    return Response({"error": "Social account not found"}, status=status.HTTP_404_NOT_FOUND)
                return Response(SocialAccountSerializer(account).data)
            accounts = SocialAccount.objects.filter(
                user=request.user).order_by("platform", "created_at")
            return Response(SocialAccountSerializer(accounts, many=True).data)
        except Exception as exc:
            logger.exception("Error fetching social accounts")
            return Response({"error": "Unable to fetch social accounts", "details": str(exc)},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def delete(self, request, pk=None):
        try:
            if not pk:
                return Response({"error": "Method not allowed without ID"},
                                status=status.HTTP_405_METHOD_NOT_ALLOWED)
            try:
                account = SocialAccount.objects.get(pk=pk, user=request.user)
            except SocialAccount.DoesNotExist:
                return Response({"error": "Social account not found"}, status=status.HTTP_404_NOT_FOUND)
            account.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        except Exception as exc:
            logger.exception("Error deleting social account")
            return Response({"error": "Unable to delete social account", "details": str(exc)},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ──────────────────────────────────────────────
# OAUTH CONNECT
# ──────────────────────────────────────────────

class SocialConnectStartView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, platform):
        if platform not in {choice[0] for choice in SocialAccount.Platform.choices}:
            return Response({"error": "Unsupported platform"}, status=status.HTTP_400_BAD_REQUEST)

        # ── Enforce max_accounts plan limit ───────────────────────────────
        try:
            from billing.views import get_or_create_subscription
            sub = get_or_create_subscription(request.user)
            max_accounts = sub.plan.max_accounts
            if max_accounts != -1:
                current_count = SocialAccount.objects.filter(user=request.user).count()
                if current_count >= max_accounts:
                    return Response(
                        {
                            "error": (
                                f"You have reached your plan limit of {max_accounts} connected account(s). "
                                f"Please upgrade your plan to connect more accounts."
                            ),
                            "limit_reached": True,
                            "max_accounts": max_accounts,
                            "current_accounts": current_count,
                        },
                        status=status.HTTP_403_FORBIDDEN
                    )
        except Exception as exc:
            logger.warning("Could not check account limit for user=%s: %s", request.user.id, exc)

        try:
            state_value = generate_state()
            redirect_url = request.query_params.get("next", "").strip()
            expires_at = oauth_expiry()
            code_verifier = ""
            login_method = ""

            use_instagram_login = (
                platform == SocialAccount.Platform.INSTAGRAM
                and request.query_params.get("method", "instagram") != "facebook"
                and bool(getattr(settings, "INSTAGRAM_APP_ID", ""))
            )

            if platform == SocialAccount.Platform.LINKEDIN:
                callback_url = settings.LINKEDIN_REDIRECT_URI
            elif use_instagram_login:
                callback_url = getattr(settings, "INSTAGRAM_REDIRECT_URI", "") or request.build_absolute_uri(
                    reverse("social-connect-callback",
                            kwargs={"platform": platform})
                )
            elif platform == SocialAccount.Platform.TWITTER:
                callback_url = getattr(settings, "TWITTER_REDIRECT_URI", "") or request.build_absolute_uri(
                    reverse("social-connect-callback",
                            kwargs={"platform": platform})
                )
            elif platform == SocialAccount.Platform.YOUTUBE:
                callback_url = getattr(settings, "YOUTUBE_REDIRECT_URI", "") or request.build_absolute_uri(
                    reverse("social-connect-callback",
                            kwargs={"platform": platform})
                )
            else:
                callback_url = request.build_absolute_uri(
                    reverse("social-connect-callback",
                            kwargs={"platform": platform})
                )

            if platform == SocialAccount.Platform.LINKEDIN:
                auth_url = build_linkedin_auth_url(callback_url, state_value)
                note = "Connect your LinkedIn personal profile."
            elif use_instagram_login:
                auth_url = build_instagram_login_auth_url(
                    callback_url, state_value)
                login_method = "instagram"
                note = (
                    "Sign in with your Instagram professional (Business or Creator) account — "
                    "no Facebook account or Page required. "
                    "⚠️ Personal Instagram accounts are NOT supported by Instagram's API."
                )
            elif platform == SocialAccount.Platform.TWITTER:
                tokens = fetch_twitter_request_token(callback_url)
                auth_url = build_twitter_oauth1_auth_url(tokens["oauth_token"])
                state_value = tokens["oauth_token"]
                code_verifier = tokens["oauth_token_secret"]
                note = "Connect your Twitter/X account."
            elif platform == SocialAccount.Platform.YOUTUBE:
                auth_url = build_youtube_auth_url(callback_url, state_value)
                note = "Connect your YouTube channel to upload videos. If you have multiple channels, you can select the correct one after connecting."
            else:
                auth_url = build_meta_auth_url(callback_url, state_value)
                note = (
                    "Instagram uses Meta/Facebook login. "
                    "After sign-in, choose your Facebook Page or Instagram professional account. "
                    "⚠️ Personal Instagram accounts are NOT supported — a Business or Creator account is required."
                )

            OAuthState.objects.create(
                user=request.user,
                platform=platform,
                state=state_value,
                callback_uri=callback_url,
                code_verifier=code_verifier,
                login_method=login_method,
                redirect_url=redirect_url,
                expires_at=expires_at,
            )

            return Response({
                "platform": platform,
                "auth_url": auth_url,
                "state": state_value,
                "expires_at": expires_at,
                "callback_url": callback_url,
                "note": note,
            })

        except OAuthConfigurationError as exc:
            logger.warning(
                "OAuth config error: platform=%s user=%s: %s", platform, request.user.id, exc)
            return Response({"error": "OAuth is not configured for this platform."},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception:
            logger.exception(
                "Unexpected error starting OAuth: platform=%s user=%s", platform, request.user.id)
            return Response({"error": "Unable to start social connect."},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class SocialConnectCallbackView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, platform):
        provider_error = request.query_params.get(
            "error") or request.query_params.get("error_message")
        if provider_error:
            return self._error_response("OAuth provider returned an error.")

        state_value = request.query_params.get(
            "state") or request.query_params.get("oauth_token")
        code = request.query_params.get(
            "code") or request.query_params.get("oauth_verifier")

        if not state_value or not code:
            return self._error_response("Missing authorization response.")

        try:
            oauth_state = OAuthState.objects.select_related("user").get(
                platform=platform, state=state_value
            )
        except OAuthState.DoesNotExist:
            return self._error_response("Invalid or expired session. Please restart the connection process.")

        if oauth_state.is_expired:
            OAuthState.objects.filter(pk=oauth_state.pk).delete()
            return self._error_response("OAuth session expired. Please try again.")

        if oauth_state.is_used:
            return self._redirect_success(oauth_state.redirect_url, platform)

        now = timezone.now()
        try:
            claimed = OAuthState.objects.filter(
                pk=oauth_state.pk, used_at__isnull=True, expires_at__gt=now,
            ).update(used_at=now)
        except OperationalError:
            return self._error_response("Server busy. Please try again.")

        if claimed == 0:
            latest = OAuthState.objects.filter(pk=oauth_state.pk).first()
            if latest and latest.is_used:
                return self._redirect_success(latest.redirect_url, platform)
            return self._error_response("Invalid or expired session.")

        state_user = oauth_state.user
        callback_uri = oauth_state.callback_uri
        success_redirect_url = oauth_state.redirect_url
        code_verifier = oauth_state.code_verifier
        login_method = oauth_state.login_method
        account_data = None

        try:
            if platform == SocialAccount.Platform.INSTAGRAM and login_method == "instagram":
                # Direct Instagram Login — no Facebook Page involved.
                token_payload = exchange_instagram_login_code(
                    code, callback_uri)
                profile_payload = fetch_instagram_login_profile(
                    token_payload["access_token"])
                account_data = build_instagram_login_account_data(
                    token_payload, profile_payload)

            elif platform == SocialAccount.Platform.LINKEDIN:
                token_payload = exchange_linkedin_code(code, callback_uri)
                profile_payload = fetch_linkedin_profile(
                    token_payload["access_token"])

            elif platform in [SocialAccount.Platform.FACEBOOK, SocialAccount.Platform.INSTAGRAM]:
                token_payload = exchange_meta_code(code, callback_uri)
                pages = fetch_meta_accounts(token_payload["access_token"])

                if platform == SocialAccount.Platform.FACEBOOK:
                    # Connect ALL pages the user manages (up to 5)
                    if not pages:
                        raise ValueError(
                            "No Facebook Pages found for this account.")
                    # Connect first page — user can connect more by re-authorizing
                    profile_payload = pages[0]
                else:
                    profile_payload = next(
                        (p for p in pages if p.get(
                            "instagram_business_account")), None
                    )
                    if not profile_payload:
                        raise ValueError(
                            "No Instagram professional account found.")

            elif platform == SocialAccount.Platform.TWITTER:
                auth_payload = exchange_twitter_oauth1_code(
                    state_value, code_verifier, code)
                token_payload = {
                    "access_token": auth_payload["access_token"],
                    "oauth1_access_token": auth_payload["access_token"],
                    "oauth1_access_token_secret": auth_payload["access_token_secret"],
                }
                profile_payload = {
                    "id": auth_payload["user_id"],
                    "username": auth_payload["screen_name"],
                    "name": auth_payload["screen_name"],
                }

            elif platform == SocialAccount.Platform.YOUTUBE:
                token_payload = exchange_youtube_code(code, callback_uri)
                profile_payload = fetch_youtube_profile(
                    token_payload["access_token"])

            else:
                raise ValueError("Unsupported platform.")

            if account_data is None:
                account_data = build_social_account_data(
                    platform, token_payload, profile_payload)

            # Set account_label from the profile
            account_label = (
                profile_payload.get("name") or
                profile_payload.get("username") or
                profile_payload.get("email") or
                account_data.get("platform_username", "")
            )
            account_data["account_label"] = account_label

            # Allow multiple accounts — update if same account_id exists, else create
            account = SocialAccount.objects.filter(
                user=state_user,
                platform=platform,
                account_id=account_data["account_id"]
            ).first()

            if account:
                for field, value in account_data.items():
                    setattr(account, field, value)
                account.save()
            else:
                account = SocialAccount.objects.create(
                    user=state_user, platform=platform, **account_data
                )

            success_redirect = _build_frontend_redirect(
                success_redirect_url or settings.SOCIAL_OAUTH_SUCCESS_URL,
                {"status": "success", "platform": platform},
            )
            if success_redirect:
                return HttpResponseRedirect(success_redirect)

            return Response({
                "message": "Social account connected successfully",
                "platform": platform,
                "account": SocialAccountSerializer(account).data,
            }, status=status.HTTP_200_OK)

        except (OAuthConfigurationError, SocialPlatformError, ValueError) as exc:
            logger.warning("OAuth error: platform=%s user=%s: %s",
                           platform, state_user.id, exc)
            OAuthState.objects.filter(pk=oauth_state.pk).update(used_at=None)
            return self._error_response(
                str(exc) if isinstance(exc, ValueError)
                else "Social account connection failed. Please try again."
            )
        except Exception:
            logger.exception(
                "Unexpected OAuth callback failure: platform=%s user=%s", platform, state_user.id)
            OAuthState.objects.filter(pk=oauth_state.pk).update(used_at=None)
            return self._error_response("Social account connection failed. Please try again.")

    def _redirect_success(self, redirect_url, platform):
        url = _build_frontend_redirect(
            redirect_url or settings.SOCIAL_OAUTH_SUCCESS_URL,
            {"status": "success", "platform": platform},
        )
        if url:
            return HttpResponseRedirect(url)
        return Response({"message": "Social account already connected", "platform": platform})

    def _error_response(self, message):
        url = _build_frontend_redirect(
            settings.SOCIAL_OAUTH_ERROR_URL, {
                "status": "error", "message": message}
        )
        if url:
            return HttpResponseRedirect(url)
        return Response({"error": message}, status=status.HTTP_400_BAD_REQUEST)


# ──────────────────────────────────────────────
# AUTH
# ──────────────────────────────────────────────

class RegisterView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        try:
            serializer = RegisterSerializer(data=request.data)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            return Response({"message": "User registered successfully"}, status=status.HTTP_201_CREATED)
        except serializers.ValidationError as exc:
            return Response(exc.detail, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Error registering user")
            return Response({"error": "Unable to register user", "details": str(exc)},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):

        try:
            keys = list(request.data.keys())
        except Exception:
            keys = "unparseable-body"
        logger.warning(
            "LOGIN DEBUG: keys=%s email=%r username=%r identifier=%r has_password=%s",
            keys,
            request.data.get("email"),
            request.data.get("username"),
            request.data.get("identifier"),
            bool(request.data.get("password")),
        )
        serializer = LoginSerializer(data=request.data)
        if not serializer.is_valid():
            logger.warning("LOGIN DEBUG: rejected -> %s", serializer.errors)
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        return Response(serializer.validated_data, status=status.HTTP_200_OK)


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        refresh_token = request.data.get("refresh")
        if not refresh_token:
            return Response({"error": "Refresh token is required to logout"},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            token = RefreshToken(refresh_token)
            token.blacklist()
        except Exception as exc:
            logger.warning("Logout token blacklist issue: %s", exc)
        return Response({"message": "Successfully logged out"}, status=status.HTTP_205_RESET_CONTENT)


# ──────────────────────────────────────────────
# ACCOUNT HEALTH
# ──────────────────────────────────────────────

class SocialAccountHealthView(APIView):
    """
    GET /api/social-accounts/health/
    Returns health status of all connected social accounts for the user.
    Token status: 'active', 'expiring_soon' (within 7 days), 'expired', 'no_expiry'
    Also shows last post time per account from platform_results.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            from datetime import timedelta
            now = timezone.now()
            warn_threshold = now + timedelta(days=7)

            accounts = SocialAccount.objects.filter(
                user=request.user
            ).order_by("platform", "created_at")

            health_data = []
            for account in accounts:
                if account.expires_at is None:
                    token_status = "no_expiry"
                elif account.expires_at <= now:
                    token_status = "expired"
                elif account.expires_at <= warn_threshold:
                    token_status = "expiring_soon"
                else:
                    token_status = "active"

                # Find last successful post for this account
                last_post_info = Post.objects.filter(
                    user=request.user,
                    status__in=[Post.Status.PUBLISHED, Post.Status.PARTIAL],
                ).order_by("-published_at").values(
                    "id", "published_at", f"platform_results"
                ).first()

                last_post_at = None
                last_post_id = None
                if last_post_info:
                    results = last_post_info.get("platform_results") or {}
                    account_result = results.get(str(account.id), {})
                    if account_result.get("success"):
                        last_post_at = last_post_info.get("published_at")
                        last_post_id = last_post_info.get("id")

                days_until_expiry = None
                if account.expires_at:
                    delta = account.expires_at - now
                    days_until_expiry = max(0, delta.days)

                health_data.append({
                    "id": account.id,
                    "platform": account.platform,
                    "display_name": account.display_name,
                    "platform_username": account.platform_username,
                    "account_label": account.account_label,
                    "token_status": token_status,
                    "expires_at": account.expires_at,
                    "days_until_expiry": days_until_expiry,
                    "last_post_at": last_post_at,
                    "last_post_id": last_post_id,
                    "created_at": account.created_at,
                    "updated_at": account.updated_at,
                })

            return Response({"accounts": health_data})
        except Exception as exc:
            logger.exception("Error fetching account health")
            return Response(
                {"error": "Unable to fetch account health", "details": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


# ──────────────────────────────────────────────
# POST STATUS (real-time polling)
# ──────────────────────────────────────────────

class PostStatusView(APIView):
    """
    GET /api/posts/<pk>/status/
    Returns the current status of a post for real-time polling.
    Frontend can poll every 3-5 seconds while post is in PROCESSING state.
    Returns minimal payload for efficiency.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            post = Post.objects.get(pk=pk, user=request.user)
        except Post.DoesNotExist:
            return Response({"error": "Post not found"}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            "id": post.id,
            "status": post.status,
            "platform_results": post.platform_results,
            "published_at": post.published_at,
            "updated_at": post.updated_at,
            "celery_task_id": post.celery_task_id,
        })


# ──────────────────────────────────────────────
# POST ANALYTICS
# ──────────────────────────────────────────────

class PostAnalyticsView(APIView):
    """
    GET /api/posts/<pk>/analytics/
    Fetches live engagement analytics from each platform for a published post.
    Returns metrics keyed by social account ID.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            post = Post.objects.get(pk=pk, user=request.user)
        except Post.DoesNotExist:
            return Response({"error": "Post not found"}, status=status.HTTP_404_NOT_FOUND)

        if post.status not in [Post.Status.PUBLISHED, Post.Status.PARTIAL]:
            return Response(
                {"error": "Analytics are only available for published posts."},
                status=status.HTTP_400_BAD_REQUEST
            )

        analytics = {}
        for account_key, result in (post.platform_results or {}).items():
            if not result.get("success"):
                analytics[account_key] = {"error": "Post was not published to this account."}
                continue

            post_urn = result.get("post_urn") or result.get("post_id")
            platform = result.get("platform")

            if not post_urn or not platform:
                analytics[account_key] = {"error": "Missing post identifier."}
                continue

            try:
                account = SocialAccount.objects.get(
                    id=int(account_key), user=request.user
                )
                metrics = self._fetch_metrics(platform, account, post_urn)
                analytics[account_key] = {
                    "platform": platform,
                    "display_name": result.get("display_name", ""),
                    "metrics": metrics,
                    "fetched_at": timezone.now().isoformat(),
                }
            except SocialAccount.DoesNotExist:
                analytics[account_key] = {"error": "Account not found."}
            except Exception as exc:
                logger.warning(
                    "Analytics fetch failed: post_id=%s account=%s error=%s",
                    pk, account_key, exc
                )
                analytics[account_key] = {"error": str(exc)}

        return Response({
            "post_id": post.id,
            "post_status": post.status,
            "published_at": post.published_at,
            "analytics": analytics,
        })

    def _fetch_metrics(self, platform, account, post_urn):
        """Fetch engagement metrics from platform API."""
        import requests as req_lib
        token = account.access_token

        if platform == "linkedin":
            encoded = req_lib.utils.quote(post_urn, safe="")
            url = f"https://api.linkedin.com/rest/socialMetadata/{encoded}"
            headers = {
                "Authorization": f"Bearer {token}",
                "LinkedIn-Version": "202602",
                "X-Restli-Protocol-Version": "2.0.0",
            }
            resp = req_lib.get(url, headers=headers, timeout=15)
            if not resp.ok:
                raise Exception(f"LinkedIn API error: {resp.status_code}")
            data = resp.json()
            return {
                "likes": data.get("totalSocialActivityCounts", {}).get("numLikes", 0),
                "comments": data.get("totalSocialActivityCounts", {}).get("numComments", 0),
                "shares": data.get("totalSocialActivityCounts", {}).get("numShares", 0),
                "impressions": data.get("totalSocialActivityCounts", {}).get("numViews", 0),
            }

        elif platform == "facebook":
            from django.conf import settings as dj_settings
            graph_version = getattr(dj_settings, "META_GRAPH_API_VERSION", "v23.0")
            url = f"https://graph.facebook.com/{graph_version}/{post_urn}"
            params = {
                "fields": "likes.summary(true),comments.summary(true),shares",
                "access_token": token,
            }
            resp = req_lib.get(url, params=params, timeout=15)
            if not resp.ok:
                raise Exception(f"Facebook API error: {resp.status_code}")
            data = resp.json()
            return {
                "likes": data.get("likes", {}).get("summary", {}).get("total_count", 0),
                "comments": data.get("comments", {}).get("summary", {}).get("total_count", 0),
                "shares": data.get("shares", {}).get("count", 0),
            }

        elif platform == "instagram":
            from django.conf import settings as dj_settings
            graph_version = getattr(dj_settings, "META_GRAPH_API_VERSION", "v23.0")
            login_type = (account.metadata or {}).get("login_type")
            if login_type == "instagram":
                base = f"https://graph.instagram.com/{graph_version}"
            else:
                base = f"https://graph.facebook.com/{graph_version}"
            url = f"{base}/{post_urn}"
            params = {
                "fields": "like_count,comments_count,media_type,timestamp",
                "access_token": token,
            }
            resp = req_lib.get(url, params=params, timeout=15)
            if not resp.ok:
                raise Exception(f"Instagram API error: {resp.status_code}")
            data = resp.json()
            return {
                "likes": data.get("like_count", 0),
                "comments": data.get("comments_count", 0),
                "media_type": data.get("media_type", ""),
            }

        elif platform == "twitter":
            # Twitter API v2 — requires OAuth1 user context
            from requests_oauthlib import OAuth1
            from django.conf import settings as dj_settings
            oauth = OAuth1(
                dj_settings.TWITTER_CONSUMER_KEY,
                dj_settings.TWITTER_CONSUMER_SECRET,
                account.metadata.get("oauth1_access_token", ""),
                account.metadata.get("oauth1_access_token_secret", ""),
            )
            url = f"https://api.twitter.com/2/tweets/{post_urn}"
            params = {"tweet.fields": "public_metrics"}
            resp = req_lib.get(url, params=params, auth=oauth, timeout=15)
            if not resp.ok:
                raise Exception(f"Twitter API error: {resp.status_code}")
            data = resp.json().get("data", {})
            metrics = data.get("public_metrics", {})
            return {
                "likes": metrics.get("like_count", 0),
                "retweets": metrics.get("retweet_count", 0),
                "replies": metrics.get("reply_count", 0),
                "impressions": metrics.get("impression_count", 0),
            }

        elif platform == "youtube":
            from django.conf import settings as dj_settings
            url = "https://www.googleapis.com/youtube/v3/videos"
            params = {
                "id": post_urn,
                "part": "statistics",
            }
            headers = {"Authorization": f"Bearer {token}"}
            resp = req_lib.get(url, params=params, headers=headers, timeout=15)
            if not resp.ok:
                raise Exception(f"YouTube API error: {resp.status_code}")
            items = resp.json().get("items", [])
            if not items:
                return {"error": "Video not found or analytics not available."}
            stats = items[0].get("statistics", {})
            return {
                "views": int(stats.get("viewCount", 0)),
                "likes": int(stats.get("likeCount", 0)),
                "comments": int(stats.get("commentCount", 0)),
                "favorites": int(stats.get("favoriteCount", 0)),
            }

        return {"error": f"Analytics not supported for platform: {platform}"}


# ──────────────────────────────────────────────
# BULK DELETE POSTS
# ──────────────────────────────────────────────

class BulkDeletePostsView(APIView):
    """
    DELETE /api/posts/bulk-delete/
    Body: {"post_ids": [1, 2, 3]}
    Deletes multiple posts. For published posts, also deletes from platforms.
    Returns summary of deleted and failed post IDs.
    """
    permission_classes = [IsAuthenticated]

    def delete(self, request):
        post_ids = request.data.get("post_ids", [])
        if not isinstance(post_ids, list) or not post_ids:
            return Response(
                {"error": "post_ids must be a non-empty list of post IDs."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate all are integers
        try:
            post_ids = [int(pid) for pid in post_ids]
        except (ValueError, TypeError):
            return Response(
                {"error": "All post_ids must be integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        deleted = []
        failed = []

        posts = Post.objects.filter(id__in=post_ids, user=request.user)
        found_ids = {p.id for p in posts}
        not_found = [pid for pid in post_ids if pid not in found_ids]

        for pid in not_found:
            failed.append({"id": pid, "error": "Post not found."})

        for post in posts:
            try:
                # Delete from platforms if published
                if post.status in [Post.Status.PUBLISHED, Post.Status.PARTIAL]:
                    for account_key, result in (post.platform_results or {}).items():
                        if result.get("success") and not result.get("deleted"):
                            post_urn = result.get("post_urn") or result.get("post_id")
                            if post_urn:
                                try:
                                    account = SocialAccount.objects.get(
                                        id=int(account_key), user=request.user
                                    )
                                    service = get_service(
                                        account.platform, request.user, account=account
                                    )
                                    service.delete_post(post_urn)
                                except Exception as exc:
                                    logger.warning(
                                        "Bulk delete: platform delete failed for post_id=%s account=%s: %s",
                                        post.id, account_key, exc
                                    )

                # Revoke pending Celery task if any
                if post.celery_task_id and post.status in [
                    Post.Status.SCHEDULED, Post.Status.PENDING
                ]:
                    try:
                        from celery import current_app
                        current_app.control.revoke(post.celery_task_id, terminate=False)
                    except Exception:
                        pass

                post.delete()
                deleted.append(post.id)
            except Exception as exc:
                logger.exception("Bulk delete failed for post_id=%s", post.id)
                failed.append({"id": post.id, "error": str(exc)})

        return Response({
            "message": f"Bulk delete complete: {len(deleted)} deleted, {len(failed)} failed.",
            "deleted": deleted,
            "failed": failed,
        })


# ──────────────────────────────────────────────
# YOUTUBE CHANNEL SELECTOR
# ──────────────────────────────────────────────

class YouTubeChannelSelectView(APIView):
    """
    POST /api/social-connect/youtube/select-channel/
    Body: {"social_account_id": 5, "channel_id": "UCxxxxx", "channel_title": "My Channel"}
    Updates a YouTube social account to use a specific channel from the list
    stored in metadata["available_channels"]. This allows users with multiple
    YouTube channels on one Google account to pick which channel to use.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        account_id = request.data.get("social_account_id")
        channel_id = request.data.get("channel_id", "").strip()
        channel_title = request.data.get("channel_title", "").strip()

        if not account_id or not channel_id:
            return Response(
                {"error": "social_account_id and channel_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            account = SocialAccount.objects.get(
                id=int(account_id), user=request.user, platform="youtube"
            )
        except SocialAccount.DoesNotExist:
            return Response(
                {"error": "YouTube account not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Validate channel_id exists in available_channels
        available = (account.metadata or {}).get("available_channels", [])
        if available:
            valid_ids = [ch.get("id") for ch in available]
            if channel_id not in valid_ids:
                return Response(
                    {"error": f"Channel ID '{channel_id}' is not in available channels for this account."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Update account_id to the selected channel
        account.account_id = channel_id
        if channel_title:
            account.account_label = channel_title
            account.platform_username = channel_title
        metadata = dict(account.metadata or {})
        metadata["selected_channel_id"] = channel_id
        metadata["selected_channel_title"] = channel_title
        account.metadata = metadata
        account.save(update_fields=["account_id", "account_label", "platform_username", "metadata", "updated_at"])

        logger.info(
            "YouTube channel selected: user=%s account_id=%s channel=%s",
            request.user.id, account.id, channel_id
        )

        return Response({
            "message": f"YouTube channel '{channel_title or channel_id}' selected successfully.",
            "account": SocialAccountSerializer(account).data,
        })

