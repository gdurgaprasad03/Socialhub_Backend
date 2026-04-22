from django.core.management.base import BaseCommand
from billing.models import Plan

class Command(BaseCommand):
    help = "Seeds the initial subscription plans"

    def handle(self, *args, **options):
        plans = [
            {
                "name": "Free",
                "slug": "free",
                "interval": "monthly",
                "price": 0,
                "posts_per_month": 21, # 3 posts per day * 7 days
                "posts_per_day": 3,
                "max_accounts": 2,
                "razorpay_plan_id": "",
            },
            {
                "name": "Basic",
                "slug": "basic",
                "interval": "monthly",
                "price": 499,
                "posts_per_month": 100,
                "posts_per_day": -1,
                "max_accounts": 3,
                "razorpay_plan_id": "",
            },
            {
                "name": "Pro",
                "slug": "pro",
                "interval": "monthly",
                "price": 1499,
                "posts_per_month": 500,
                "posts_per_day": -1,
                "max_accounts": 6,
                "razorpay_plan_id": "",
            },
            {
                "name": "Agency",
                "slug": "agency",
                "interval": "monthly",
                "price": 3999,
                "posts_per_month": -1,
                "posts_per_day": -1,
                "max_accounts": 10,
                "razorpay_plan_id": "",
            },
        ]

        # 1. Update or create the plans in the list
        seeded_slugs = [p["slug"] for p in plans]
        for plan_data in plans:
            plan, created = Plan.objects.update_or_create(
                slug=plan_data["slug"],
                defaults=plan_data
            )
            status = "created" if created else "updated"
            self.stdout.write(self.style.SUCCESS(f"Successfully {status} plan: {plan.name}"))

        # 2. Deactivate any existing plans that are NOT in our list (like the old 'starter')
        deactivated_count = Plan.objects.exclude(slug__in=seeded_slugs).update(is_active=False)
        if deactivated_count:
            self.stdout.write(self.style.WARNING(f"Deactivated {deactivated_count} old plan(s)."))
