import hashlib
import hmac
import json
import logging

import razorpay
from django.conf import settings
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import BillingEvent, Plan, PostUsage, UserSubscription
from .serializers import PlanSerializer, UserSubscriptionSerializer

logger = logging.getLogger(__name__)


def get_razorpay_client():
    return razorpay.Client(
        auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
    )


def get_or_create_subscription(user):
    """Get user subscription or auto-assign Free plan safely."""
    free_plan = Plan.objects.filter(slug="free", is_active=True).first()
    if not free_plan:
        # Create a default Free plan if it doesn't exist
        free_plan = Plan.objects.create(
            name="Free Trial",
            slug="free",
            price=0,
            posts_limit=21, # 3 per day * 7 days
            posts_per_day=3,
            max_accounts=2
        )

    subscription, created = UserSubscription.objects.get_or_create(
        user=user,
        defaults={
            "plan": free_plan,
            "status": UserSubscription.Status.TRIALING,
            "current_period_start": timezone.now(),
            "current_period_end": timezone.now() + timezone.timedelta(days=7),
        }
    )
    return subscription


class PlanListView(APIView):
    """
    GET /api/billing/plans/
    List all active plans. Public endpoint — no auth required.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        plans = Plan.objects.filter(is_active=True).order_by("price")
        return Response(PlanSerializer(plans, many=True).data)


class CurrentSubscriptionView(APIView):
    """
    GET /api/billing/subscription/
    Get the current user's subscription and usage.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            subscription = get_or_create_subscription(request.user)
            usage = PostUsage.get_or_create_for_user(request.user)

            plan = subscription.plan
            posts_limit = plan.posts_limit
            posts_used = usage.posts_used
            posts_remaining = (
                -1 if plan.is_unlimited
                else max(0, posts_limit - posts_used)
            )
            usage_pct = (
                0 if plan.is_unlimited
                else min(100, round((posts_used / posts_limit) * 100, 1)) if posts_limit > 0 else 100
            )

            # Calculate days remaining
            days_left = (subscription.current_period_end - timezone.now()).days
            days_left = max(0, days_left)

            # Daily usage
            daily_limit = plan.posts_per_day
            daily_used = usage.daily_posts_used
            # Reset if it's a new day
            if usage.last_post_at and usage.last_post_at.date() < timezone.now().date():
                daily_used = 0

            return Response({
                "subscription": UserSubscriptionSerializer(subscription).data,
                "days_left": days_left,
                "usage": {
                    "posts_used": posts_used,
                    "posts_limit": posts_limit,
                    "posts_remaining": posts_remaining,
                    "daily_used": daily_used,
                    "daily_limit": daily_limit,
                    "usage_percentage": usage_pct,
                    "is_unlimited": plan.is_unlimited,
                    "period_start": usage.period_start,
                },
            })
        except Exception as exc:
            logger.exception("Error fetching subscription")
            return Response(
                {"error": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class CreateSubscriptionView(APIView):
    """
    POST /api/billing/subscribe/
    Create a Razorpay subscription for a paid plan.
    Body: {"plan_slug": "starter"}
    Returns Razorpay subscription ID for frontend to complete payment.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        plan_slug = request.data.get("plan_slug", "").strip()
        if not plan_slug:
            return Response(
                {"error": "plan_slug is required"},
                status=status.HTTP_400_BAD_REQUEST
            )

        try:
            plan = Plan.objects.get(slug=plan_slug, is_active=True)
        except Plan.DoesNotExist:
            return Response(
                {"error": f"Plan '{plan_slug}' not found"},
                status=status.HTTP_404_NOT_FOUND
            )

        if plan.is_free:
            # Downgrade to free — cancel existing Razorpay subscription
            return self._downgrade_to_free(request.user)

        if not plan.razorpay_plan_id:
            return Response(
                {"error": f"Plan '{plan_slug}' is not configured for payments yet."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Plan change / upgrade handling.
        # If the user already has an active paid plan, block a no-op and cancel
        # the existing Razorpay subscription before creating the new one so the
        # user is never billed for two subscriptions at once.
        current = get_or_create_subscription(request.user)
        if (
            current.plan_id == plan.id
            and current.status == UserSubscription.Status.ACTIVE
        ):
            return Response(
                {"error": f"You are already subscribed to the {plan.name} plan."},
                status=status.HTTP_400_BAD_REQUEST
            )

        # Allow upgrade from any status (TRIALING, CANCELLED, EXPIRED, PAST_DUE)
        # as long as it's not a duplicate ACTIVE subscription for the same plan.
        is_plan_change = bool(
            current.razorpay_subscription_id
            and not current.plan.is_free
            and current.status == UserSubscription.Status.ACTIVE
        )
        if is_plan_change:
            self._cancel_existing_razorpay_subscription(current)

        try:
            client = get_razorpay_client()

            # Create Razorpay subscription
            rp_subscription = client.subscription.create({
                "plan_id": plan.razorpay_plan_id,
                "total_count": 12,  # 12 billing cycles (1 year)
                "quantity": 1,
                "customer_notify": 1,
                "notes": {
                    "user_id": str(request.user.id),
                    "user_email": request.user.email,
                    "plan_slug": plan_slug,
                }
            })

            if is_plan_change:
                BillingEvent.objects.create(
                    user=request.user,
                    event_type=BillingEvent.EventType.PLAN_CHANGED,
                    payload={
                        "from_plan": current.plan.slug,
                        "to_plan": plan_slug,
                        "new_razorpay_subscription_id": rp_subscription["id"],
                    },
                )

            return Response({
                "razorpay_subscription_id": rp_subscription["id"],
                "razorpay_key_id": settings.RAZORPAY_KEY_ID,
                "plan": PlanSerializer(plan).data,
                "amount": int(plan.price * 100),  # in paise
                "currency": "INR",
                "is_plan_change": is_plan_change,
            })

        except Exception as exc:
            logger.exception("Error creating Razorpay subscription")
            return Response(
                {"error": f"Payment setup failed: {str(exc)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

    def _cancel_existing_razorpay_subscription(self, subscription):
        """
        Cancel the user's current Razorpay subscription immediately so that
        switching to another paid plan never results in double billing.
        Failures are logged but not fatal — the user still gets the new plan.
        """
        if not subscription.razorpay_subscription_id:
            return
        old_id = subscription.razorpay_subscription_id
        try:
            client = get_razorpay_client()
            client.subscription.cancel(
                old_id,
                {"cancel_at_cycle_end": 0},  # cancel now, not at period end
            )
            # Detach the old id immediately so the resulting
            # `subscription.cancelled` webhook can't downgrade this user before
            # the new subscription's `subscription.activated` webhook lands.
            subscription.razorpay_subscription_id = ""
            subscription.save(update_fields=["razorpay_subscription_id", "updated_at"])
            logger.info(
                "Cancelled old subscription %s for plan change (user=%s)",
                old_id, subscription.user_id,
            )
        except Exception as exc:
            logger.warning(
                "Failed to cancel old Razorpay subscription %s during plan change: %s",
                old_id, exc,
            )

    def _downgrade_to_free(self, user):
        try:
            subscription = get_or_create_subscription(user)

            # Already on free plan and not an active paid subscription — nothing to do
            if subscription.plan.is_free and subscription.status not in [
                UserSubscription.Status.ACTIVE,
                UserSubscription.Status.TRIALING,
            ]:
                free_plan = subscription.plan
                return Response({
                    "message": "You are already on the Free plan.",
                    "plan": PlanSerializer(free_plan).data,
                })

            if subscription.razorpay_subscription_id:
                try:
                    client = get_razorpay_client()
                    client.subscription.cancel(
                        subscription.razorpay_subscription_id,
                        {"cancel_at_cycle_end": 1}
                    )
                except Exception as exc:
                    logger.warning("Failed to cancel Razorpay subscription: %s", exc)

            free_plan = Plan.objects.get(slug="free", is_active=True)
            subscription.plan = free_plan
            subscription.status = UserSubscription.Status.CANCELLED
            subscription.razorpay_subscription_id = ""
            subscription.cancelled_at = timezone.now()
            subscription.save()

            return Response({
                "message": "Downgraded to Free plan. You can upgrade again at any time.",
                "plan": PlanSerializer(free_plan).data,
            })
        except Exception as exc:
            logger.exception("Error downgrading to free")
            return Response(
                {"error": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class VerifySubscriptionView(APIView):
    """
    POST /api/subscribe/verify/
    Called by the frontend in Razorpay checkout's success handler, right after
    payment. Verifies the payment signature and activates the plan immediately
    so the user doesn't have to wait for the (async, sometimes-delayed) webhook.
    Body: {
        "razorpay_payment_id": "...",
        "razorpay_subscription_id": "...",
        "razorpay_signature": "..."
    }
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        payment_id = request.data.get("razorpay_payment_id", "").strip()
        subscription_id = request.data.get("razorpay_subscription_id", "").strip()
        signature = request.data.get("razorpay_signature", "").strip()

        if not (payment_id and subscription_id and signature):
            return Response(
                {"error": "razorpay_payment_id, razorpay_subscription_id and "
                          "razorpay_signature are all required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        client = get_razorpay_client()

        # 1. Verify the payment actually came from Razorpay (anti-tamper).
        try:
            client.utility.verify_subscription_payment_signature({
                "razorpay_payment_id": payment_id,
                "razorpay_subscription_id": subscription_id,
                "razorpay_signature": signature,
            })
        except Exception:
            logger.warning("Invalid subscription payment signature for sub %s", subscription_id)
            return Response(
                {"error": "Payment verification failed. Signature mismatch."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 2. Resolve which plan was actually paid for (trust Razorpay, not the
        #    client) by reading the plan_id off the subscription.
        try:
            rp_sub = client.subscription.fetch(subscription_id)
            rp_plan_id = rp_sub.get("plan_id", "")
        except Exception as exc:
            logger.exception("Could not fetch Razorpay subscription %s", subscription_id)
            return Response(
                {"error": f"Could not verify subscription: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        plan = Plan.objects.filter(razorpay_plan_id=rp_plan_id, is_active=True).first()
        if not plan:
            logger.error("No local plan matches Razorpay plan_id %s", rp_plan_id)
            return Response(
                {"error": "Paid plan is not recognised on the server."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 3. Activate immediately.
        now = timezone.now()
        subscription, _ = UserSubscription.objects.update_or_create(
            user=request.user,
            defaults={
                "plan": plan,
                "status": UserSubscription.Status.ACTIVE,
                "razorpay_subscription_id": subscription_id,
                "current_period_start": now,
                "current_period_end": now + timezone.timedelta(days=30),
                "cancelled_at": None,
            },
        )

        BillingEvent.objects.create(
            user=request.user,
            event_type=BillingEvent.EventType.PAYMENT_SUCCESS,
            payload={
                "plan": plan.slug,
                "razorpay_payment_id": payment_id,
                "razorpay_subscription_id": subscription_id,
                "source": "verify",
            },
        )
        logger.info("Subscription activated via verify: user=%s plan=%s",
                    request.user.id, plan.slug)

        return Response({
            "message": f"You are now on the {plan.name} plan.",
            "subscription": UserSubscriptionSerializer(subscription).data,
        })


class CancelSubscriptionView(APIView):
    """
    POST /api/billing/cancel/
    Cancel the current subscription at end of billing period.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            subscription = get_or_create_subscription(request.user)

            # Block only if already cancelled or expired — not free trial users
            if subscription.status in [
                UserSubscription.Status.CANCELLED,
                UserSubscription.Status.EXPIRED,
            ]:
                return Response(
                    {"error": "Your subscription is already cancelled."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            # Block if on free plan and NOT trialing (i.e., a plain free account, not a trial)
            if subscription.plan.is_free and subscription.status != UserSubscription.Status.TRIALING:
                return Response(
                    {"error": "You are already on the Free plan."},
                    status=status.HTTP_400_BAD_REQUEST
                )

            if subscription.razorpay_subscription_id:
                try:
                    client = get_razorpay_client()
                    client.subscription.cancel(
                        subscription.razorpay_subscription_id,
                        {"cancel_at_cycle_end": 1}
                    )
                except Exception as exc:
                    logger.warning("Razorpay cancel failed: %s", exc)

            # Downgrade to free plan on cancellation and mark cancelled
            try:
                free_plan = Plan.objects.get(slug="free", is_active=True)
            except Plan.DoesNotExist:
                free_plan = subscription.plan  # fallback: keep current plan

            subscription.plan = free_plan
            subscription.razorpay_subscription_id = ""
            subscription.cancelled_at = timezone.now()
            subscription.status = UserSubscription.Status.CANCELLED
            subscription.save()

            BillingEvent.objects.create(
                user=request.user,
                event_type=BillingEvent.EventType.SUBSCRIPTION_CANCELLED,
                payload={"plan": subscription.plan.slug},
            )

            return Response({
                "message": "Subscription cancelled. You can upgrade to a paid plan at any time.",
                "current_period_end": subscription.current_period_end,
            })
        except Exception as exc:
            logger.exception("Error cancelling subscription")
            return Response(
                {"error": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class RazorpayWebhookView(APIView):
    """
    POST /api/billing/webhook/
    Handle Razorpay webhook events.
    Add this URL to Razorpay Dashboard → Webhooks.
    """
    permission_classes = [AllowAny]

    def post(self, request):
        # Verify webhook signature
        webhook_secret = getattr(settings, "RAZORPAY_WEBHOOK_SECRET", "")
        if webhook_secret:
            signature = request.headers.get("X-Razorpay-Signature", "")
            try:
                client = get_razorpay_client()
                client.utility.verify_webhook_signature(
                    request.body.decode("utf-8"),
                    signature,
                    webhook_secret,
                )
            except Exception:
                logger.warning("Invalid Razorpay webhook signature")
                return Response({"error": "Invalid signature"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            return Response({"error": "Invalid JSON"}, status=status.HTTP_400_BAD_REQUEST)

        event_type = payload.get("event", "")
        event_id = payload.get("id", "")

        # Deduplicate events
        if event_id and BillingEvent.objects.filter(razorpay_event_id=event_id).exists():
            logger.info("Duplicate webhook event ignored: %s", event_id)
            return Response({"status": "ok"})

        logger.info("Razorpay webhook received: %s", event_type)

        try:
            if event_type == "subscription.activated":
                self._handle_subscription_activated(payload)
            elif event_type == "subscription.charged":
                self._handle_subscription_charged(payload)
            elif event_type == "subscription.cancelled":
                self._handle_subscription_cancelled(payload)
            elif event_type == "subscription.completed":
                self._handle_subscription_cancelled(payload)
            elif event_type == "payment.failed":
                self._handle_payment_failed(payload)

            # Log event
            BillingEvent.objects.create(
                razorpay_event_id=event_id or None,
                event_type=event_type,
                payload=payload,
            )

        except Exception as exc:
            logger.exception("Error processing webhook: %s", event_type)
            return Response(
                {"error": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        return Response({"status": "ok"})

    def _get_user_from_subscription(self, rp_subscription_id):
        try:
            sub = UserSubscription.objects.select_related("user").get(
                razorpay_subscription_id=rp_subscription_id
            )
            return sub.user, sub
        except UserSubscription.DoesNotExist:
            return None, None

    def _handle_subscription_activated(self, payload):
        """Payment succeeded — activate subscription."""
        subscription_data = payload.get("payload", {}).get("subscription", {}).get("entity", {})
        rp_subscription_id = subscription_data.get("id")
        notes = subscription_data.get("notes", {})
        user_id = notes.get("user_id")
        plan_slug = notes.get("plan_slug")

        if not user_id or not plan_slug:
            logger.warning("Missing user_id or plan_slug in webhook notes")
            return

        from django.contrib.auth import get_user_model
        User = get_user_model()

        try:
            user = User.objects.get(id=user_id)
            plan = Plan.objects.get(slug=plan_slug, is_active=True)
        except (User.DoesNotExist, Plan.DoesNotExist) as exc:
            logger.error("User or plan not found: %s", exc)
            return

        subscription, _ = UserSubscription.objects.update_or_create(
            user=user,
            defaults={
                "plan": plan,
                "status": UserSubscription.Status.ACTIVE,
                "razorpay_subscription_id": rp_subscription_id,
                "current_period_start": timezone.now(),
                "current_period_end": timezone.now() + timezone.timedelta(days=30),
                "cancelled_at": None,
            }
        )

        BillingEvent.objects.create(
            user=user,
            event_type=BillingEvent.EventType.SUBSCRIPTION_CREATED,
            payload={"plan": plan_slug, "razorpay_subscription_id": rp_subscription_id},
        )
        logger.info("Subscription activated: user=%s plan=%s", user.id, plan_slug)

    def _handle_subscription_charged(self, payload):
        """Recurring payment succeeded — renew period."""
        subscription_data = payload.get("payload", {}).get("subscription", {}).get("entity", {})
        rp_subscription_id = subscription_data.get("id")

        user, subscription = self._get_user_from_subscription(rp_subscription_id)
        if not user:
            logger.warning("No subscription found for renewal: %s", rp_subscription_id)
            return

        subscription.status = UserSubscription.Status.ACTIVE
        subscription.current_period_start = timezone.now()
        subscription.current_period_end = timezone.now() + timezone.timedelta(days=30)
        subscription.save()

        BillingEvent.objects.create(
            user=user,
            event_type=BillingEvent.EventType.SUBSCRIPTION_RENEWED,
            payload={"razorpay_subscription_id": rp_subscription_id},
        )
        logger.info("Subscription renewed: user=%s", user.id)

    def _handle_subscription_cancelled(self, payload):
        """Subscription cancelled or completed."""
        subscription_data = payload.get("payload", {}).get("subscription", {}).get("entity", {})
        rp_subscription_id = subscription_data.get("id")

        user, subscription = self._get_user_from_subscription(rp_subscription_id)
        if not user:
            return

        # Downgrade to free plan
        try:
            free_plan = Plan.objects.get(slug="free", is_active=True)
            subscription.plan = free_plan
            subscription.status = UserSubscription.Status.EXPIRED
            subscription.cancelled_at = timezone.now()
            subscription.save()
        except Plan.DoesNotExist:
            subscription.status = UserSubscription.Status.EXPIRED
            subscription.save()

        logger.info("Subscription expired/cancelled: user=%s", user.id)

    def _handle_payment_failed(self, payload):
        """Payment failed — mark as past due."""
        subscription_data = payload.get("payload", {}).get("subscription", {}).get("entity", {})
        rp_subscription_id = subscription_data.get("id", "")

        if not rp_subscription_id:
            return

        user, subscription = self._get_user_from_subscription(rp_subscription_id)
        if not user:
            return

        subscription.status = UserSubscription.Status.PAST_DUE
        subscription.save()

        BillingEvent.objects.create(
            user=user,
            event_type=BillingEvent.EventType.PAYMENT_FAILED,
            payload=payload,
        )
        logger.warning("Payment failed: user=%s", user.id)


class UsageView(APIView):
    """
    GET /api/billing/usage/
    Get current month usage stats for the user.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        try:
            subscription = get_or_create_subscription(request.user)
            usage = PostUsage.get_or_create_for_user(request.user)
            plan = subscription.plan

            # Calculate days remaining
            days_left = (subscription.current_period_end - timezone.now()).days
            days_left = max(0, days_left)

            # Daily usage
            daily_limit = plan.posts_per_day
            daily_used = usage.daily_posts_used
            # Reset if it's a new day
            if usage.last_post_at and usage.last_post_at.date() < timezone.now().date():
                daily_used = 0

            posts_remaining = (
                -1 if plan.is_unlimited
                else max(0, plan.posts_limit - usage.posts_used)
            )

            return Response({
                "plan_name": plan.name,
                "posts_used": usage.posts_used,
                "posts_limit": plan.posts_limit,
                "posts_remaining": posts_remaining,
                "is_unlimited": plan.is_unlimited,
                "period_start": usage.period_start,
                "max_accounts": plan.max_accounts,
                "days_left": days_left,
                "daily_used": daily_used,
                "daily_limit": daily_limit
            })
        except Exception as exc:
            logger.exception("Error fetching usage")
            return Response(
                {"error": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )