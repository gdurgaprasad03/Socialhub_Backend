# ============================================================
# billing/serializers.py
# ============================================================
from rest_framework import serializers
from .models import Plan, UserSubscription, PostUsage


class PlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = Plan
        fields = [
            "id", "name", "slug", "interval", "price",
            "posts_limit", "max_accounts", "is_active",
        ]


class UserSubscriptionSerializer(serializers.ModelSerializer):
    plan = PlanSerializer(read_only=True)
    is_active = serializers.BooleanField(read_only=True)
    is_expired = serializers.BooleanField(read_only=True)

    class Meta:
        model = UserSubscription
        fields = [
            "id", "plan", "status", "is_active", "is_expired",
            "current_period_start", "current_period_end",
            "cancelled_at", "created_at",
        ]