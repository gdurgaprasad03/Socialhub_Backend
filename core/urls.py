from django.urls import path

from .views import (
    CreatePost,
    DashboardStatsView,
    DeletePublishedPostView,
    LoginView,
    LogoutView,
    RegisterView,
    SocialAccountView,
    SocialConnectStartView,
    SocialConnectCallbackView,
    SchedulingView,
    SocialAccountHealthView,
    PostStatusView,
    PostAnalyticsView,
    BulkDeletePostsView,
    LinkedInPageSelectView,
    YouTubeChannelSelectView,
    DesignExportView,
    DesignListView,
    PolotnoStateSaveView,
)
from .services.platform_crud_views import (
    GetPlatformPostView,
    UpdatePlatformPostView,
    AutoSaveDraftView,
)


urlpatterns = [
    # ── Auth ──────────────────────────────────────────────────────────────
    path("register/", RegisterView.as_view(), name="register"),
    path("login/", LoginView.as_view(), name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),

    # ── Dashboard ─────────────────────────────────────────────────────────
    path("dashboard/", DashboardStatsView.as_view(), name="dashboard-stats"),

    # ── Social Accounts ───────────────────────────────────────────────────
    path("social-accounts/", SocialAccountView.as_view(), name="social-accounts"),
    path("social-accounts/<int:pk>/", SocialAccountView.as_view(), name="social-accounts-detail"),
    # Account health: token status, expiry, last post per account
    path("social-accounts/health/", SocialAccountHealthView.as_view(), name="social-accounts-health"),

    # ── OAuth ─────────────────────────────────────────────────────────────
    path("social-connect/<str:platform>/start/", SocialConnectStartView.as_view(), name="social-connect-start"),
    path("social-connect/<str:platform>/callback/", SocialConnectCallbackView.as_view(), name="social-connect-callback"),
    # LinkedIn Page selector (connect an org page using the personal account token)
    path("social-connect/linkedin/select-page/", LinkedInPageSelectView.as_view(), name="linkedin-select-page"),
    # YouTube channel selector (for users with multiple channels on one Google account)
    path("social-connect/youtube/select-channel/", YouTubeChannelSelectView.as_view(), name="youtube-select-channel"),

    # ── Posts ─────────────────────────────────────────────────────────────
    path("posts/", CreatePost.as_view(), name="create-post"),
    path("posts/<int:pk>/", CreatePost.as_view(), name="post-detail"),

    # Bulk delete multiple posts at once (published posts are also removed from platforms)
    path("posts/bulk-delete/", BulkDeletePostsView.as_view(), name="bulk-delete-posts"),

    # Auto-save draft (called by frontend on navigate away / window close)
    path("posts/autosave/", AutoSaveDraftView.as_view(), name="post-autosave"),

    # Real-time post status polling (lightweight — for PROCESSING state)
    path("posts/<int:pk>/status/", PostStatusView.as_view(), name="post-status"),

    # Live analytics from platform APIs (likes, comments, impressions, views)
    path("posts/<int:pk>/analytics/", PostAnalyticsView.as_view(), name="post-analytics"),

    # Per-account CRUD operations on published posts
    # Delete post from specific account
    path("posts/<int:pk>/account/<int:account_id>/", DeletePublishedPostView.as_view(), name="delete-published-post"),
    # Read post from platform (get live data)
    path("posts/<int:pk>/account/<int:account_id>/read/", GetPlatformPostView.as_view(), name="read-platform-post"),
    # Update post on platform (Facebook message, Instagram caption, YouTube title/description/privacy)
    path("posts/<int:pk>/account/<int:account_id>/update/", UpdatePlatformPostView.as_view(), name="update-platform-post"),

    # ── Scheduling ────────────────────────────────────────────────────────
    path("scheduling/", SchedulingView.as_view(), name="scheduling"),
    path("scheduling/<int:pk>/", SchedulingView.as_view(), name="scheduling-detail"),

    # ── Design Studio (Canva + Polotno) ───────────────────────────────────
    path("designs/export/", DesignExportView.as_view(), name="design-export"),
    path("designs/", DesignListView.as_view(), name="design-list"),
    path("designs/<int:pk>/", DesignListView.as_view(), name="design-detail"),
    path("designs/<int:pk>/state/", PolotnoStateSaveView.as_view(), name="design-state"),
]
