from django.contrib import admin
from .models import Plan, UserSubscription, BillingEvent, PostUsage

@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "price", "posts_limit", "is_active", "created_at")
    list_filter = ("is_active", "created_at")
    search_fields = ("name", "slug")
    ordering = ("price",)

@admin.register(UserSubscription)
class UserSubscriptionAdmin(admin.ModelAdmin):
    list_display = ("user", "plan", "status", "current_period_end", "razorpay_subscription_id")
    list_filter = ("status", "plan")
    search_fields = ("user__username", "user__email", "razorpay_subscription_id")
    ordering = ("-current_period_end",)

@admin.register(PostUsage)
class PostUsageAdmin(admin.ModelAdmin):
    list_display = ("user", "posts_used", "period_start", "last_reset_at", "updated_at")
    search_fields = ("user__username", "user__email")
    list_filter = ("period_start",)
    ordering = ("-updated_at",)

@admin.register(BillingEvent)
class BillingEventAdmin(admin.ModelAdmin):
    list_display = ("event_type", "user", "razorpay_event_id", "created_at")
    list_filter = ("event_type", "created_at")
    search_fields = ("user__username", "razorpay_event_id")
    readonly_fields = ("payload", "created_at")
