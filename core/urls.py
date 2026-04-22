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

    # ── OAuth ─────────────────────────────────────────────────────────────
    path("social-connect/<str:platform>/start/", SocialConnectStartView.as_view(), name="social-connect-start"),
    path("social-connect/<str:platform>/callback/", SocialConnectCallbackView.as_view(), name="social-connect-callback"),

    # ── Posts ─────────────────────────────────────────────────────────────
    path("posts/", CreatePost.as_view(), name="create-post"),
    path("posts/<int:pk>/", CreatePost.as_view(), name="post-detail"),

    # Auto-save draft (called by frontend on navigate away / window close)
    path("posts/autosave/", AutoSaveDraftView.as_view(), name="post-autosave"),

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
]



