from django.conf import settings
from django.db import models
from django.utils import timezone


class Plan(models.Model):
    """
    Defines available subscription plans.
    Seeded via management command — not created by users.
    """
    class Interval(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        ANNUAL = "annual", "Annual"

    name = models.CharField(max_length=50, unique=True)  # Free, Starter, Pro, Agency
    slug = models.SlugField(unique=True)                  # free, starter, pro, agency
    interval = models.CharField(max_length=10, choices=Interval.choices, default=Interval.MONTHLY)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    posts_limit = models.IntegerField(
        default=30,
        help_text="Total posts allowed in the period (e.g., 7-day trial or 1 month). -1 = unlimited."
    )
    posts_per_day = models.IntegerField(
        default=3,
        help_text="Max posts per day. -1 = unlimited."
    )
    max_accounts = models.IntegerField(
        default=2,
        help_text="Max connected social accounts. -1 = unlimited."
    )
    is_active = models.BooleanField(default=True)
    razorpay_plan_id = models.CharField(
        max_length=255, blank=True,
        help_text="Razorpay plan ID for subscription billing"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["price"]

    def __str__(self):
        return f"{self.name} — ₹{self.price}/{self.interval}"

    @property
    def is_free(self):
        return self.price == 0

    @property
    def is_unlimited(self):
        return self.posts_limit == -1


class UserSubscription(models.Model):
    """
    Tracks each user's current active plan and Razorpay subscription.
    One active subscription per user at a time.
    """
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"
        PAST_DUE = "past_due", "Past Due"
        TRIALING = "trialing", "Trialing"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="subscription"
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    razorpay_subscription_id = models.CharField(max_length=255, blank=True)
    razorpay_customer_id = models.CharField(max_length=255, blank=True)
    current_period_start = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "status"]),
        ]

    def __str__(self):
        return f"{self.user} — {self.plan.name} ({self.status})"

    @property
    def is_active(self):
        return self.status == self.Status.ACTIVE

    @property
    def is_expired(self):
        if self.current_period_end and self.current_period_end < timezone.now():
            return True
        return False


class PostUsage(models.Model):
    """
    Tracks how many posts a user has made in the current billing period.
    Resets on the 1st of every month via Celery task.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="post_usage"
    )
    posts_used = models.IntegerField(default=0)
    daily_posts_used = models.IntegerField(default=0)
    last_post_at = models.DateTimeField(null=True, blank=True)
    period_start = models.DateField(default=timezone.now)
    last_reset_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["user"]),
        ]

    def __str__(self):
        return f"{self.user} — {self.posts_used} posts used"

    def increment(self, count=1):
        """Increment post usage count with daily reset logic."""
        now = timezone.now()
        
        # Reset daily count if it's a new day
        if self.last_post_at and self.last_post_at.date() < now.date():
            PostUsage.objects.filter(user=self.user).update(
                daily_posts_used=count,
                posts_used=models.F("posts_used") + count,
                last_post_at=now
            )
        else:
            PostUsage.objects.filter(user=self.user).update(
                daily_posts_used=models.F("daily_posts_used") + count,
                posts_used=models.F("posts_used") + count,
                last_post_at=now
            )
        self.refresh_from_db()

    def decrement(self, count=1):
        """Decrement post usage (e.g. on failed post)."""
        PostUsage.objects.filter(user=self.user).update(
            posts_used=models.F("posts_used") - count
        )
        self.refresh_from_db()

    @classmethod
    def get_or_create_for_user(cls, user):
        usage, _ = cls.objects.get_or_create(
            user=user,
            defaults={"period_start": timezone.now().date()}
        )
        return usage


class BillingEvent(models.Model):
    """
    Audit log for all billing events (payments, cancellations, etc.)
    """
    class EventType(models.TextChoices):
        SUBSCRIPTION_CREATED = "subscription.created", "Subscription Created"
        SUBSCRIPTION_RENEWED = "subscription.renewed", "Subscription Renewed"
        SUBSCRIPTION_CANCELLED = "subscription.cancelled", "Subscription Cancelled"
        PAYMENT_SUCCESS = "payment.success", "Payment Success"
        PAYMENT_FAILED = "payment.failed", "Payment Failed"
        PLAN_CHANGED = "plan.changed", "Plan Changed"
        USAGE_RESET = "usage.reset", "Usage Reset"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="billing_events",
        null=True, blank=True
    )
    event_type = models.CharField(max_length=50, choices=EventType.choices)
    razorpay_event_id = models.CharField(max_length=255, blank=True, unique=True, null=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.event_type} — {self.user} — {self.created_at}"