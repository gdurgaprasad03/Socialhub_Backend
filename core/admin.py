from django.contrib import admin
from .models import Post, SocialAccount, OAuthState, PostingSchedule


@admin.register(SocialAccount)
class SocialAccountAdmin(admin.ModelAdmin):
    list_display = (
        "id", "user", "platform", "account_label", 
        "platform_username", "account_id", "expires_at", "created_at"
    )
    search_fields = ("user__username", "user__email", "account_id", "platform_username", "account_label")
    list_filter = ("platform", "created_at")
    ordering = ("-created_at",)
    readonly_fields = ("created_at", "updated_at")


@admin.register(OAuthState)
class OAuthStateAdmin(admin.ModelAdmin):
    list_display = ("id", "platform", "user", "state", "expires_at", "used_at", "created_at")
    search_fields = ("user__username", "state")
    list_filter = ("platform", "used_at")
    ordering = ("-created_at",)
    readonly_fields = ("created_at",)


@admin.register(Post)
class PostAdmin(admin.ModelAdmin):
    list_display = (
        "id", "user", "status", "scheduled_time", 
        "get_target_accounts_count", "published_at", "created_at"
    )
    search_fields = ("user__username", "content", "celery_task_id")
    list_filter = ("status", "created_at", "published_at")
    ordering = ("-created_at",)
    readonly_fields = ("created_at", "updated_at", "platform_results", "celery_task_id")

    @admin.display(description="Accounts")
    def get_target_accounts_count(self, obj):
        return len(obj.target_accounts) if obj.target_accounts else 0


@admin.register(PostingSchedule)
class PostingScheduleAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "get_day_of_week_display", "time")
    list_filter = ("day_of_week", "user")
    search_fields = ("user__username",)
    ordering = ("user", "day_of_week", "time")

