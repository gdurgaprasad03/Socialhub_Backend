import base64
import copy
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
from .models import OAuthState, Post, SocialAccount, PostingSchedule, Design
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
    build_linkedin_page_account_data,
    exchange_linkedin_code,
    exchange_meta_code,
    exchange_instagram_login_code,
    exchange_twitter_code,
    exchange_twitter_oauth1_code,
    exchange_youtube_code,
    fetch_linkedin_profile,
    fetch_linkedin_pages,
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
from .cloudinary_utils import upload_image_to_cloudinary, upload_video_to_cloudinary

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
        
        local_ref = timezone.localtime(reference_time)
        ref_day = local_ref.weekday()
        ref_time = local_ref.time()
        for schedule in schedules:
            if schedule.day_of_week > ref_day or (
                schedule.day_of_week == ref_day and schedule.time > ref_time
            ):
                target_date = local_ref.date() + timedelta(
                    days=(schedule.day_of_week - ref_day)
                )
                dt = datetime.combine(target_date, schedule.time)
                return timezone.make_aware(dt)
        days_ahead = 7 - local_ref.weekday()
        next_local = (local_ref + timedelta(days=days_ahead)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        reference_time = timezone.make_aware(
            datetime(next_local.year, next_local.month, next_local.day),
            timezone.get_current_timezone(),
        )
    return None


def _save_uploaded_files(files):
    saved_urls = []
    for f in files:
        content_type = getattr(f, "content_type", "")
        is_video = content_type.startswith("video/") or f.name.lower().endswith(
            (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v")
        )
        try:
            if is_video:
                url = upload_video_to_cloudinary(f)
            else:
                url, _ = upload_image_to_cloudinary(f)
            saved_urls.append(url)
        except Exception as e:
            logger.exception("Failed to upload file to Cloudinary: %s", f.name)
            raise
    return saved_urls


# Map common image/video mime types to file extensions for base64 uploads.
_MIME_EXT = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
    "image/gif": "gif", "image/webp": "webp", "image/bmp": "bmp",
    "video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm",
}


def _save_base64_media(data_uri):
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

    is_video = mime.startswith("video/")
    try:
        file_obj = ContentFile(raw, name=f"{uuid.uuid4().hex}.{ext}")
        if is_video:
            url = upload_video_to_cloudinary(file_obj)
        else:
            url, _ = upload_image_to_cloudinary(file_obj)
        return url
    except Exception as exc:
        logger.exception("Failed to upload base64 media to Cloudinary: %s", exc)
        return None


def _normalize_image_inputs(values):
   
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
    
    urls = []
    provided = False

    uploaded_files = request.FILES.getlist("media_files")
    if uploaded_files:
        provided = True
        logger.info("CreatePost: received %d uploaded file(s)",
                    len(uploaded_files))
        urls += _save_uploaded_files(uploaded_files)

    
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

            # ── Intercept video_file and media_file direct uploads ─────────
            video_url = None
            image_url = None
            if "video_file" in request.FILES:
                try:
                    vf = request.FILES.pop("video_file")[0]
                except (KeyError, IndexError, TypeError):
                    vf = request.FILES.pop("video_file", None)
                if vf:
                    video_url = upload_video_to_cloudinary(vf)

            if "media_file" in request.FILES:
                try:
                    mf = request.FILES.pop("media_file")[0]
                except (KeyError, IndexError, TypeError):
                    mf = request.FILES.pop("media_file", None)
                if mf:
                    image_url, _ = upload_image_to_cloudinary(mf)

            # ── Handle media (file uploads, base64 data URIs, URLs) ────────
            all_images, _ = _collect_request_images(request)
            if image_url:
                all_images.append(image_url)

            # ── Duplicate post prevention (idempotency check) ─────────────
            import hashlib
            import json as _json
            raw_ta = request.data.get("target_accounts", [])
            if isinstance(raw_ta, str):
                try:
                    raw_ta = _json.loads(raw_ta)
                except Exception:
                    raw_ta = []
            
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

            if video_url:
                data["video"] = video_url
                data.pop("video_file", None)
            if image_url:
                data["image"] = image_url
                data.pop("media_file", None)

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
                if not scheduled_time:
                    return Response(
                        {"error": "No posting schedule slots configured. Please add a time slot in your scheduling settings first."},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                serializer.validated_data["scheduled_time"] = scheduled_time

            if is_draft:
                initial_status = Post.Status.DRAFT
            else:
                initial_status = Post.Status.SCHEDULED if scheduled_time else Post.Status.PENDING

            post = serializer.save(user=request.user, status=initial_status, scheduled_time=scheduled_time)
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

            video_url = None
            image_url = None
            if "video_file" in request.FILES:
                try:
                    vf = request.FILES.pop("video_file")[0]
                except (KeyError, IndexError, TypeError):
                    vf = request.FILES.pop("video_file", None)
                if vf:
                    video_url = upload_video_to_cloudinary(vf)

            if "media_file" in request.FILES:
                try:
                    mf = request.FILES.pop("media_file")[0]
                except (KeyError, IndexError, TypeError):
                    mf = request.FILES.pop("media_file", None)
                if mf:
                    image_url, _ = upload_image_to_cloudinary(mf)

            if hasattr(request.data, 'dict'):
                data = request.data.dict()
            else:
                data = dict(request.data)

            all_images, media_provided = _collect_request_images(request)
            if image_url:
                all_images.append(image_url)
                media_provided = True

            if video_url:
                data["video"] = video_url
                data.pop("video_file", None)
            if image_url:
                data["image"] = image_url
                data.pop("media_file", None)

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

            # Check if we should force local delete bypass.
            # Default is false: if the remote platform delete fails the post is
            # kept locally so the user can retry. Pass ?force=true to remove
            # locally regardless of remote delete outcome.
            force = request.query_params.get("force", "false").lower() == "true"

            # Block deletion while the Celery task is actively publishing and
            # has not yet written any results. At this moment the task has
            # already started making platform API calls; if we delete the local
            # record now the publish will finish and leave orphaned content on
            # the platform with no way to track or remove it. The PROCESSING
            # window is typically a few seconds for image posts.
            if post.status == Post.Status.PROCESSING and not any(
                isinstance(r, dict) and r.get("success")
                for r in (post.platform_results or {}).values()
            ):
                return Response(
                    {
                        "error": (
                            "This post is currently being published. "
                            "Please wait a few seconds for publishing to finish, then try again."
                        )
                    },
                    status=status.HTTP_409_CONFLICT,
                )

            errors = {}
            updated_results = copy.deepcopy(post.platform_results or {})

            # Attempt platform deletion for any account that has a successful
            # publish record, regardless of the post's overall status.
            # This covers PUBLISHED, PARTIAL, and PROCESSING (e.g. LinkedIn
            # video posts where the video upload already succeeded but the post
            # status hasn't flipped to PUBLISHED yet).
            has_published_content = any(
                isinstance(r, dict) and r.get("success") and not r.get("deleted")
                for r in (post.platform_results or {}).values()
            )

            if has_published_content:
                for account_key, result in (post.platform_results or {}).items():
                    if not (result.get("success") and not result.get("deleted")):
                        continue
                    post_urn = result.get("post_urn") or result.get("post_id")
                    if not post_urn:
                        errors[account_key] = (
                            "No platform post ID recorded for this account; "
                            "cannot delete remotely. Use ?force=true to remove locally."
                        )
                        logger.warning(
                            "Cannot delete remotely — no post_urn/post_id stored: "
                            "post_id=%s account=%s", pk, account_key
                        )
                        continue
                    try:
                        account = SocialAccount.objects.get(
                            id=int(account_key), user=request.user
                        )
                        service = get_service(
                            account.platform, request.user, account=account)
                        service.account = account
                        service.delete_post(post_urn)
                        updated_results[account_key]["deleted"] = True
                        logger.info(
                            "Platform delete succeeded: post_id=%s account=%s platform=%s",
                            pk, account_key, account.platform,
                        )
                    except SocialAccount.DoesNotExist:
                        # Account was disconnected after publishing — can't reach platform.
                        # Allow local delete so the orphaned DB record can be cleaned up.
                        logger.warning(
                            "Account no longer exists, skipping remote delete: "
                            "post_id=%s account=%s", pk, account_key
                        )
                        updated_results[account_key]["deleted"] = True
                    except Exception as exc:
                        logger.exception(
                            "Remote delete failed: post_id=%s account=%s error=%s",
                            pk, account_key, exc
                        )
                        errors[account_key] = str(exc)

                post.platform_results = updated_results
                post.save(update_fields=["platform_results", "updated_at"])

            # If there are any failed remote deletes, do NOT delete locally, unless force=true is passed
            if errors and not force:
                return Response(
                    {
                        "error": "Failed to delete post from some social media platforms. The post was not removed locally so you can retry. Use ?force=true to delete locally anyway.",
                        "details": errors,
                        "platform_results": updated_results
                    },
                    status=status.HTTP_502_BAD_GATEWAY
                )

            # Revoke pending Celery task if any (covers SCHEDULED, PENDING, and
            # the narrow PROCESSING window where terminate=False prevents retries)
            if post.celery_task_id and post.status in [
                Post.Status.SCHEDULED, Post.Status.PENDING, Post.Status.PROCESSING
            ]:
                try:
                    from celery import current_app
                    current_app.control.revoke(post.celery_task_id, terminate=False)
                except Exception:
                    pass

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

        account_key = str(account_id)
        platform_result = (post.platform_results or {}).get(account_key)
        if not platform_result or not platform_result.get("success"):
            return Response(
                {"error": f"No successful publish record found for account: {account_id}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        post_urn = platform_result.get("post_urn") or platform_result.get("post_id")
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

        updated_results = copy.deepcopy(post.platform_results)
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

        # 2. Get actual upcoming and past scheduled posts
        posts = Post.objects.filter(
            user=request.user,
            scheduled_time__isnull=False,
        ).exclude(status=Post.Status.DRAFT).order_by("scheduled_time")
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
        pk = self.kwargs.get('pk')
        if not pk:
            PostingSchedule.objects.filter(user=request.user).delete()
            return Response(status=status.HTTP_204_NO_CONTENT)
        deleted, _ = PostingSchedule.objects.filter(user=request.user, pk=pk).delete()
        if not deleted:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
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
                current_count = SocialAccount.objects.filter(user=request.user, platform=platform).count()
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
            error_description = (
                request.query_params.get("error_description")
                or request.query_params.get("error_reason")
                or ""
            )
            logger.warning(
                "OAuth provider error: platform=%s error=%s description=%s",
                platform, provider_error, error_description,
            )
            detail = f"{provider_error}: {error_description}" if error_description else provider_error
            return self._error_response(f"OAuth provider returned an error: {detail}")

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
                try:
                    pages = fetch_linkedin_pages(token_payload["access_token"])
                except Exception as exc:
                    logger.warning("Could not fetch LinkedIn pages for user=%s: %s", state_user.id, exc)
                    pages = []
                profile_payload["available_pages"] = pages

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
   
    permission_classes = [IsAuthenticated]

    def delete(self, request):
        post_ids = request.data.get("post_ids", [])
        # Default false: keep posts locally if remote delete fails so the user can retry.
        # Pass ?force=true to remove locally regardless of remote outcome.
        force = request.query_params.get("force", "false").lower() == "true"
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
                post_errors = {}
                updated_results = copy.deepcopy(post.platform_results or {})

                if post.status in [Post.Status.PUBLISHED, Post.Status.PARTIAL]:
                    for account_key, result in (post.platform_results or {}).items():
                        if result.get("success") and not result.get("deleted"):
                            post_urn = result.get("post_urn") or result.get("post_id")
                            if not post_urn:
                                post_errors[account_key] = (
                                    "No platform post ID recorded for this account; "
                                    "cannot delete remotely. Use ?force=true to remove locally."
                                )
                                logger.warning(
                                    "Bulk delete: no post_urn/post_id stored: "
                                    "post_id=%s account=%s", post.id, account_key
                                )
                            else:
                                try:
                                    account = SocialAccount.objects.get(
                                        id=int(account_key), user=request.user
                                    )
                                    if account.platform == "instagram":
                                        # Instagram Graph API does not support deleting media; skip and mark as deleted locally
                                        if account_key not in updated_results:
                                            updated_results[account_key] = result.copy()
                                        updated_results[account_key]["deleted"] = True
                                        continue

                                    service = get_service(
                                        account.platform, request.user, account=account
                                    )
                                    service.delete_post(post_urn)
                                    if account_key not in updated_results:
                                        updated_results[account_key] = result.copy()
                                    updated_results[account_key]["deleted"] = True
                                except Exception as exc:
                                    logger.exception(
                                        "Bulk delete: platform delete failed for post_id=%s account=%s",
                                        post.id, account_key
                                    )
                                    post_errors[account_key] = str(exc)

                    post.platform_results = updated_results
                    post.save(update_fields=["platform_results", "updated_at"])

                if post_errors and not force:
                    failed.append({
                        "id": post.id,
                        "error": "Failed to delete from some platforms.",
                        "details": post_errors
                    })
                else:
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
# LINKEDIN PAGE SELECTOR
# ──────────────────────────────────────────────

class LinkedInPageSelectView(APIView):
 
    permission_classes = [IsAuthenticated]

    def get(self, request):
        personal_accounts = SocialAccount.objects.filter(
            user=request.user,
            platform="linkedin",
            account_type="personal",
        )
        pages = []
        for acc in personal_accounts:
            available = (acc.metadata or {}).get("available_pages", [])
            for page in available:
                pages.append({
                    "social_account_id": acc.id,
                    "personal_account_label": acc.account_label or acc.platform_username,
                    "page_id": page.get("id"),
                    "page_name": page.get("name"),
                })
        return Response({"pages": pages})

    def post(self, request):
        social_account_id = request.data.get("social_account_id")
        page_id = str(request.data.get("page_id", "")).strip()
        page_name = str(request.data.get("page_name", "")).strip()

        if not social_account_id or not page_id:
            return Response(
                {"error": "social_account_id and page_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Enforce plan max_accounts limit (skip if page already exists for this user)
        page_exists = SocialAccount.objects.filter(
            user=request.user, platform="linkedin", account_id=page_id
        ).exists()
        if not page_exists:
            try:
                from billing.views import get_or_create_subscription
                sub = get_or_create_subscription(request.user)
                max_accounts = sub.plan.max_accounts
                if max_accounts != -1:
                    current_count = SocialAccount.objects.filter(user=request.user, platform="linkedin").count()
                    if current_count >= max_accounts:
                        return Response(
                            {
                                "error": (
                                    f"You have reached your plan limit of {max_accounts} connected account(s). "
                                    "Please upgrade your plan to connect more accounts."
                                ),
                                "limit_reached": True,
                                "max_accounts": max_accounts,
                                "current_accounts": current_count,
                            },
                            status=status.HTTP_403_FORBIDDEN,
                        )
            except Exception as exc:
                logger.warning("Could not check account limit for user=%s: %s", request.user.id, exc)

        try:
            personal_account = SocialAccount.objects.get(
                id=int(social_account_id),
                user=request.user,
                platform="linkedin",
                account_type="personal",
            )
        except SocialAccount.DoesNotExist:
            return Response(
                {"error": "LinkedIn personal account not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        available = (personal_account.metadata or {}).get("available_pages", [])
        if available:
            valid_ids = [p.get("id") for p in available]
            if page_id not in valid_ids:
                return Response(
                    {"error": f"Page ID '{page_id}' is not in your available LinkedIn pages."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not page_name:
                page_name = next(
                    (p.get("name", page_id) for p in available if p.get("id") == page_id),
                    page_id,
                )

        token_payload = {
            "access_token": personal_account.access_token,
            "refresh_token": personal_account.refresh_token,
            "token_type": personal_account.token_type,
            "expires_in": None,
        }
        admin_profile = {
            "sub": personal_account.account_id,
            "name": personal_account.account_label or personal_account.platform_username,
        }
        page_data = {"id": page_id, "name": page_name}
        account_data = build_linkedin_page_account_data(token_payload, page_data, admin_profile)
        account_data["expires_at"] = personal_account.expires_at

        page_account, created = SocialAccount.objects.update_or_create(
            user=request.user,
            platform="linkedin",
            account_id=page_id,
            defaults=account_data,
        )

        logger.info(
            "LinkedIn Page connected: user=%s page_id=%s page_name=%s created=%s",
            request.user.id, page_id, page_name, created,
        )

        return Response({
            "message": f"LinkedIn Page '{page_name}' connected successfully.",
            "account": SocialAccountSerializer(page_account).data,
            "created": created,
        }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


# ──────────────────────────────────────────────
# YOUTUBE CHANNEL SELECTOR
# ──────────────────────────────────────────────

class YouTubeChannelSelectView(APIView):
    
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


# ──────────────────────────────────────────────
# DESIGN STUDIO (Canva + Polotno)
# ──────────────────────────────────────────────

class DesignExportView(APIView):

    permission_classes = [IsAuthenticated]

    def post(self, request):
        source = request.data.get("source", "polotno")
        if source not in {Design.Source.CANVA, Design.Source.POLOTNO}:
            return Response(
                {"error": "source must be 'canva' or 'polotno'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        image_data = request.data.get("image_data", "")   # base64 from Polotno
        image_url = request.data.get("image_url", "")     # export URL from Canva
        title = str(request.data.get("title", "")).strip()
        canva_design_id = str(request.data.get("canva_design_id", "")).strip()
        polotno_state = request.data.get("polotno_state", {})
        width = int(request.data.get("width", 0) or 0)
        height = int(request.data.get("height", 0) or 0)

        if not image_data and not image_url:
            return Response(
                {"error": "Provide image_data (base64) for Polotno or image_url for Canva."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            cdn_url, public_id = upload_image_to_cloudinary(
                image_data or image_url,
                folder="socialmedia/designs",
            )
        except Exception as exc:
            logger.exception("Design Cloudinary upload failed: user=%s", request.user.id)
            return Response(
                {"error": f"Image upload failed: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        design = Design.objects.create(
            user=request.user,
            source=source,
            title=title or f"{source.title()} Design",
            image_url=cdn_url,
            cloudinary_public_id=public_id,
            canva_design_id=canva_design_id,
            polotno_state=polotno_state if isinstance(polotno_state, dict) else {},
            width=width,
            height=height,
        )

        logger.info("Design saved: user=%s id=%s source=%s", request.user.id, design.id, source)

        return Response({
            "id": design.id,
            "image_url": design.image_url,
            "title": design.title,
            "source": design.source,
            "width": design.width,
            "height": design.height,
            "created_at": design.created_at.isoformat(),
        }, status=status.HTTP_201_CREATED)


class DesignListView(APIView):
    
    permission_classes = [IsAuthenticated]

    def get(self, request):
        source = request.query_params.get("source", "")
        qs = Design.objects.filter(user=request.user)
        if source in {Design.Source.CANVA, Design.Source.POLOTNO}:
            qs = qs.filter(source=source)
        data = [
            {
                "id": d.id,
                "title": d.title,
                "source": d.source,
                "image_url": d.image_url,
                "has_polotno_state": bool(d.polotno_state),
                "canva_design_id": d.canva_design_id,
                "width": d.width,
                "height": d.height,
                "created_at": d.created_at.isoformat(),
            }
            for d in qs[:100]
        ]
        return Response({"designs": data, "count": len(data)})

    def delete(self, request, pk):
        try:
            design = Design.objects.get(pk=pk, user=request.user)
        except Design.DoesNotExist:
            return Response({"error": "Design not found."}, status=status.HTTP_404_NOT_FOUND)

        if design.cloudinary_public_id:
            try:
                import cloudinary.uploader as _cu
                _cu.destroy(design.cloudinary_public_id)
            except Exception:
                pass

        design.delete()
        return Response({"message": "Design deleted."})


class PolotnoStateSaveView(APIView):
    

    permission_classes = [IsAuthenticated]

    def patch(self, request, pk):
        try:
            design = Design.objects.get(pk=pk, user=request.user, source=Design.Source.POLOTNO)
        except Design.DoesNotExist:
            return Response({"error": "Polotno design not found."}, status=status.HTTP_404_NOT_FOUND)

        polotno_state = request.data.get("polotno_state")
        if not isinstance(polotno_state, dict):
            return Response(
                {"error": "polotno_state must be a JSON object."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        design.polotno_state = polotno_state
        design.save(update_fields=["polotno_state", "updated_at"])
        return Response({"message": "Design state saved.", "id": design.id})