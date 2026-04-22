import json
import logging

from django.http import JsonResponse
from django.utils import timezone

from .models import Plan, PostUsage, UserSubscription

logger = logging.getLogger(__name__)

# URLs where post limit check applies
POST_CREATE_PATH = "/api/posts/"


class PostLimitMiddleware:
    """
    Middleware that checks if user has exceeded their monthly post limit
    before allowing POST requests to /api/posts/.

    Runs BEFORE the view — returns 402 Payment Required if limit exceeded.
    Does not count draft saves or autosave requests.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._should_check(request):
            error = self._check_limit(request)
            if error:
                return error

        response = self.get_response(request)

        # After successful post creation, increment usage
        if (
            self._should_check(request) and
            response.status_code == 201 and
            not self._is_draft(request)
        ):
            self._increment_usage(request)

        return response

    def _should_check(self, request):
        """Only check on POST to /api/posts/ — not drafts, not autosave."""
        if request.method != "POST":
            return False
        if not request.path.startswith(POST_CREATE_PATH):
            return False
        if request.path.rstrip("/") == "/api/posts/autosave":
            return False
        if not hasattr(request, "user") or not request.user.is_authenticated:
            return False
        return True

    def _is_draft(self, request):
        """Check if the request is saving a draft."""
        try:
            body = json.loads(request.body.decode("utf-8"))
            return body.get("is_draft", False)
        except Exception:
            # Multipart form
            return request.POST.get("is_draft") in ("true", "True", "1", True)

    def _check_limit(self, request):
        """Return error response if limit exceeded, None if ok."""
        try:
            # Get subscription
            try:
                subscription = request.user.subscription
            except UserSubscription.DoesNotExist:
                # No subscription — treat as free plan
                free_plan = Plan.objects.filter(slug="free", is_active=True).first()
                if not free_plan:
                    return None  # No plans configured yet — allow
                limit = free_plan.posts_per_month
            else:
                if subscription.status not in [
                    UserSubscription.Status.ACTIVE,
                    UserSubscription.Status.TRIALING,
                ]:
                    return JsonResponse({
                        "error": "Your subscription is inactive. Please renew to continue posting.",
                        "code": "subscription_inactive",
                        "upgrade_url": "/dashboard/billing/",
                    }, status=402)

                plan = subscription.plan
                if plan.is_unlimited:
                    return None  # Agency plan — no limit

                limit = plan.posts_per_month

                # Check account limit
                from core.models import SocialAccount
                account_count = SocialAccount.objects.filter(user=request.user).count()
                if plan.max_accounts != -1 and account_count > plan.max_accounts:
                    return JsonResponse({
                        "error": f"Your {plan.name} plan allows {plan.max_accounts} connected accounts. You have {account_count}.",
                        "code": "account_limit_exceeded",
                        "upgrade_url": "/dashboard/billing/",
                    }, status=402)

            # Check post usage
            if limit == -1:
                return None

            usage = PostUsage.get_or_create_for_user(request.user)
            if usage.posts_used >= limit:
                return JsonResponse({
                    "error": f"You've used all {limit} posts for this month. Upgrade your plan to continue.",
                    "code": "post_limit_exceeded",
                    "posts_used": usage.posts_used,
                    "posts_limit": limit,
                    "upgrade_url": "/dashboard/billing/",
                }, status=402)

        except Exception as exc:
            logger.exception("PostLimitMiddleware error: %s", exc)
            # On error — allow the request through (fail open)

        return None

    def _increment_usage(self, request):
        """Increment post usage after successful creation."""
        try:
            usage = PostUsage.get_or_create_for_user(request.user)
            usage.increment()
            logger.info(
                "Post usage incremented: user=%s used=%s",
                request.user.id, usage.posts_used
            )
        except Exception as exc:
            logger.exception("Failed to increment post usage: %s", exc)