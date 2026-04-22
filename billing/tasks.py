import logging
from celery import shared_task
from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task
def reset_monthly_usage():
    """
    Reset post usage for all users at the start of each month.
    Schedule this task to run on the 1st of every month at midnight.

    Add to settings.py:
    from celery.schedules import crontab

    CELERY_BEAT_SCHEDULE = {
        "reset-monthly-usage": {
            "task": "billing.tasks.reset_monthly_usage",
            "schedule": crontab(day_of_month=1, hour=0, minute=0),
        },
    }
    """
    from .models import PostUsage, BillingEvent

    now = timezone.now()
    updated_count = PostUsage.objects.update(
        posts_used=0,
        period_start=now.date(),
        last_reset_at=now,
    )

    BillingEvent.objects.create(
        event_type=BillingEvent.EventType.USAGE_RESET,
        payload={"reset_count": updated_count, "reset_at": now.isoformat()},
    )

    logger.info("Monthly usage reset complete: %d users reset", updated_count)
    return {"reset_count": updated_count}


@shared_task
def expire_past_due_subscriptions():
    """
    Expire subscriptions that are past due and past their period end.
    Run daily.

    Add to CELERY_BEAT_SCHEDULE:
    "expire-past-due": {
        "task": "billing.tasks.expire_past_due_subscriptions",
        "schedule": crontab(hour=1, minute=0),
    },
    """
    from .models import UserSubscription, Plan

    now = timezone.now()
    expired = UserSubscription.objects.filter(
        status=UserSubscription.Status.PAST_DUE,
        current_period_end__lt=now,
    )

    free_plan = Plan.objects.filter(slug="free", is_active=True).first()
    count = 0

    for sub in expired:
        if free_plan:
            sub.plan = free_plan
        sub.status = UserSubscription.Status.EXPIRED
        sub.save()
        count += 1
        logger.info("Subscription expired: user=%s", sub.user_id)

    logger.info("Expired %d past-due subscriptions", count)
    return {"expired_count": count}