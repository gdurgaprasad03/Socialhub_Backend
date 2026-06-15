import json
import logging
import os
from urllib.parse import urlencode
from datetime import datetime, timedelta
from celery import current_app
from django.conf import settings
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
        status__in=[Post.Status.SCHEDULED, Post.Status.PENDING, Post.Status.PROCESSING],
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
                from billing.models import PostUsage
                sub = get_or_create_subscription(request.user)
                usage = PostUsage.get_or_create_for_user(request.user)
                if sub.plan.posts_limit != -1 and usage.posts_used >= sub.plan.posts_limit:
                    return Response(
                        {"error": "Monthly post limit reached. Please upgrade your plan."},
                        status=status.HTTP_403_FORBIDDEN
                    )
                
                # Check Expiration
                now = timezone.now()
                if sub.current_period_end < now:
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

            # ── Handle uploaded files ──────────────────────────────────────
            uploaded_files = request.FILES.getlist("media_files")
            logger.info("CreatePost: received %d uploaded file(s)", len(uploaded_files))

            extra_image_urls = []
            if uploaded_files:
                extra_image_urls = _save_uploaded_files(uploaded_files)

            # ── Parse images ───────────────────────────────────────────────
            raw_images = request.data.get("images", "")
            existing_images = []
            if isinstance(raw_images, list):
                existing_images = raw_images
            elif isinstance(raw_images, str) and raw_images.strip():
                try:
                    parsed = json.loads(raw_images)
                    if isinstance(parsed, list):
                        existing_images = parsed
                except (ValueError, TypeError):
                    existing_images = []

            all_images = existing_images + extra_image_urls

            # ── Parse target_accounts ──────────────────────────────────────
            raw_target_accounts = request.data.get("target_accounts", "")
            if isinstance(raw_target_accounts, str) and raw_target_accounts.strip():
                try:
                    raw_target_accounts = json.loads(raw_target_accounts)
                except (ValueError, TypeError):
                    raw_target_accounts = []

            # ── Build data dict ────────────────────────────────────────────
            if hasattr(request.data, 'dict'):
                data = request.data.dict()
            else:
                data = dict(request.data)

            data["images"] = all_images
            data["target_accounts"] = raw_target_accounts

            serializer = PostSerializer(data=data, context={"request": request})
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
            logger.info("CreatePost: saved post id=%d target_accounts=%s", post.id, post.target_accounts)

            message = "Draft saved successfully"
            if not is_draft:
                if scheduled_time:
                    task_result = process_post.apply_async((post.id,), eta=scheduled_time)
                    message = "Post scheduled successfully"
                else:
                    task_result = process_post.delay(post.id)
                    message = "Post queued successfully"
                post.celery_task_id = task_result.id
                post.save(update_fields=["celery_task_id", "updated_at"])

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
            serializer = PostSerializer(
                post, data=request.data, partial=True, context={"request": request})
            serializer.is_valid(raise_exception=True)
            updated_post = serializer.save()
            if post.celery_task_id and post.status in [Post.Status.SCHEDULED, Post.Status.PENDING]:
                current_app.control.revoke(post.celery_task_id, terminate=False)
            is_draft = request.data.get("is_draft", updated_post.status == Post.Status.DRAFT)
            
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
                updated_post.save(update_fields=["status", "celery_task_id", "updated_at"])
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
                updated_post.save(update_fields=["status", "celery_task_id", "updated_at"])
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
                        post_urn = result.get("post_urn") or result.get("post_id")
                        if post_urn:
                            try:
                                account = SocialAccount.objects.get(
                                    id=int(account_key), user=request.user
                                )
                                service = get_service(account.platform, request.user, account=account)
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

        post_urn = platform_result.get("post_urn") or platform_result.get("post_id")
        if not post_urn:
            return Response(
                {"error": "No post URN stored. Cannot delete remotely."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            account = SocialAccount.objects.get(id=int(account_id), user=request.user)
            service = get_service(account.platform, request.user, account=account)
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
            logger.exception("Unexpected error deleting from platform: post_id=%s account=%s", pk, account_id)
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
    """
    Unified view for managing posting slots (PostingSchedule) 
    and viewing actual scheduled posts.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        # 1. Get recurring slots
        schedules = PostingSchedule.objects.filter(user=request.user)
        slots_data = PostingScheduleSerializer(schedules, many=True).data

        # 2. Get actual upcoming posts
        posts = Post.objects.filter(
            user=request.user,
            status__in=[Post.Status.SCHEDULED, Post.Status.PENDING, Post.Status.PROCESSING],
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
            status_counts = user_posts.values("status").annotate(count=Count("status"))
            detailed_status = {item["status"]: item["count"] for item in status_counts}
            connected_accounts = SocialAccount.objects.filter(user=request.user).count()
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
                    account = SocialAccount.objects.get(pk=pk, user=request.user)
                except SocialAccount.DoesNotExist:
                    return Response({"error": "Social account not found"}, status=status.HTTP_404_NOT_FOUND)
                return Response(SocialAccountSerializer(account).data)
            accounts = SocialAccount.objects.filter(user=request.user).order_by("platform", "created_at")
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

        try:
            state_value = generate_state()
            redirect_url = request.query_params.get("next", "").strip()
            expires_at = oauth_expiry()
            code_verifier = ""
            login_method = ""

            # Instagram can be connected two ways:
            #   • Facebook-based flow (default): IG account linked to a Facebook Page.
            #   • Direct Instagram Login: user signs in with Instagram credentials only.
            # Use the direct flow for Instagram when it's configured, unless the
            # caller explicitly requests the Facebook flow with ?method=facebook.
            # Use the direct Instagram Login flow for Instagram connections by
            # default so the button opens the separate Instagram login experience.
            # The older Meta/Facebook path is still available when the frontend
            # explicitly requests it with ?method=facebook.
            use_instagram_login = (
                platform == SocialAccount.Platform.INSTAGRAM
                and request.query_params.get("method", "instagram") != "facebook"
                and bool(getattr(settings, "INSTAGRAM_APP_ID", ""))
            )

            if platform == SocialAccount.Platform.LINKEDIN:
                callback_url = settings.LINKEDIN_REDIRECT_URI
            elif use_instagram_login:
                callback_url = getattr(settings, "INSTAGRAM_REDIRECT_URI", "") or request.build_absolute_uri(
                    reverse("social-connect-callback", kwargs={"platform": platform})
                )
            elif platform == SocialAccount.Platform.TWITTER:
                callback_url = getattr(settings, "TWITTER_REDIRECT_URI", "") or request.build_absolute_uri(
                    reverse("social-connect-callback", kwargs={"platform": platform})
                )
            elif platform == SocialAccount.Platform.YOUTUBE:
                callback_url = getattr(settings, "YOUTUBE_REDIRECT_URI", "") or request.build_absolute_uri(
                    reverse("social-connect-callback", kwargs={"platform": platform})
                )
            else:
                callback_url = request.build_absolute_uri(
                    reverse("social-connect-callback", kwargs={"platform": platform})
                )

            if platform == SocialAccount.Platform.LINKEDIN:
                auth_url = build_linkedin_auth_url(callback_url, state_value)
                note = "Connect your LinkedIn personal profile."
            elif use_instagram_login:
                auth_url = build_instagram_login_auth_url(callback_url, state_value)
                login_method = "instagram"
                note = (
                    "Sign in with your Instagram professional (Business or Creator) "
                    "account — no Facebook account or Page required."
                )
            elif platform == SocialAccount.Platform.TWITTER:
                tokens = fetch_twitter_request_token(callback_url)
                auth_url = build_twitter_oauth1_auth_url(tokens["oauth_token"])
                state_value = tokens["oauth_token"]
                code_verifier = tokens["oauth_token_secret"]
                note = "Connect your Twitter/X account."
            elif platform == SocialAccount.Platform.YOUTUBE:
                auth_url = build_youtube_auth_url(callback_url, state_value)
                note = "Connect your YouTube channel to upload videos."
            else:
                auth_url = build_meta_auth_url(callback_url, state_value)
                note = (
                    "Instagram uses Meta/Facebook login. "
                    "After sign-in, choose your Facebook Page or Instagram professional account."
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
            logger.warning("OAuth config error: platform=%s user=%s: %s", platform, request.user.id, exc)
            return Response({"error": "OAuth is not configured for this platform."},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        except Exception:
            logger.exception("Unexpected error starting OAuth: platform=%s user=%s", platform, request.user.id)
            return Response({"error": "Unable to start social connect."},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class SocialConnectCallbackView(APIView):
    permission_classes = [AllowAny]

    def get(self, request, platform):
        provider_error = request.query_params.get("error") or request.query_params.get("error_message")
        if provider_error:
            return self._error_response("OAuth provider returned an error.")

        state_value = request.query_params.get("state") or request.query_params.get("oauth_token")
        code = request.query_params.get("code") or request.query_params.get("oauth_verifier")

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
                token_payload = exchange_instagram_login_code(code, callback_uri)
                profile_payload = fetch_instagram_login_profile(token_payload["access_token"])
                account_data = build_instagram_login_account_data(token_payload, profile_payload)

            elif platform == SocialAccount.Platform.LINKEDIN:
                token_payload = exchange_linkedin_code(code, callback_uri)
                profile_payload = fetch_linkedin_profile(token_payload["access_token"])

            elif platform in [SocialAccount.Platform.FACEBOOK, SocialAccount.Platform.INSTAGRAM]:
                token_payload = exchange_meta_code(code, callback_uri)
                pages = fetch_meta_accounts(token_payload["access_token"])

                if platform == SocialAccount.Platform.FACEBOOK:
                    # Connect ALL pages the user manages (up to 5)
                    if not pages:
                        raise ValueError("No Facebook Pages found for this account.")
                    # Connect first page — user can connect more by re-authorizing
                    profile_payload = pages[0]
                else:
                    profile_payload = next(
                        (p for p in pages if p.get("instagram_business_account")), None
                    )
                    if not profile_payload:
                        raise ValueError("No Instagram professional account found.")

            elif platform == SocialAccount.Platform.TWITTER:
                auth_payload = exchange_twitter_oauth1_code(state_value, code_verifier, code)
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
                profile_payload = fetch_youtube_profile(token_payload["access_token"])

            else:
                raise ValueError("Unsupported platform.")

            # The Instagram-Login branch builds account_data itself (different
            # token/profile shape); every other flow uses the shared builder.
            if account_data is None:
                account_data = build_social_account_data(platform, token_payload, profile_payload)

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
            logger.warning("OAuth error: platform=%s user=%s: %s", platform, state_user.id, exc)
            OAuthState.objects.filter(pk=oauth_state.pk).update(used_at=None)
            return self._error_response(
                str(exc) if isinstance(exc, ValueError)
                else "Social account connection failed. Please try again."
            )
        except Exception:
            logger.exception("Unexpected OAuth callback failure: platform=%s user=%s", platform, state_user.id)
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
            settings.SOCIAL_OAUTH_ERROR_URL, {"status": "error", "message": message}
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
        # TEMP DEBUG: log what the client sends (never the password) + why it fails.
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