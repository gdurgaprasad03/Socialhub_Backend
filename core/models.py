from django.conf import settings
from django.db import models
from django.utils import timezone


class SocialAccount(models.Model):
    class Platform(models.TextChoices):
        LINKEDIN = "linkedin", "LinkedIn"
        FACEBOOK = "facebook", "Facebook"
        INSTAGRAM = "instagram", "Instagram"
        TWITTER = "twitter", "Twitter"
        YOUTUBE = "youtube", "YouTube"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="social_accounts")
    platform = models.CharField(max_length=20, choices=Platform.choices)
    account_label = models.CharField(
        max_length=255, blank=True,
        help_text="Display name shown in UI (pulled from platform during OAuth)"
    )
    access_token = models.TextField()
    refresh_token = models.TextField(blank=True)
    token_type = models.CharField(max_length=50, blank=True)
    account_id = models.CharField(max_length=255)
    platform_username = models.CharField(max_length=255, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        # Removed unique constraint on (user, platform)
        # Now allows multiple accounts per platform per user
        # Unique on (user, platform, account_id) to prevent duplicate connections
        constraints = [
            models.UniqueConstraint(
                fields=["user", "platform", "account_id"],
                name="unique_user_platform_account_id"
            ),
        ]
        indexes = [
            models.Index(fields=["user", "platform"]),
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self):
        label = self.account_label or self.platform_username or self.account_id
        return f"{self.user_id} - {self.platform} - {label}"

    @property
    def is_expired(self):
        return bool(self.expires_at and self.expires_at <= timezone.now())

    @property
    def display_name(self):
        """Best display name for UI."""
        return self.account_label or self.platform_username or self.account_id


class OAuthState(models.Model):
    class Platform(models.TextChoices):
        LINKEDIN = "linkedin", "LinkedIn"
        FACEBOOK = "facebook", "Facebook"
        INSTAGRAM = "instagram", "Instagram"
        TWITTER = "twitter", "Twitter"
        YOUTUBE = "youtube", "YouTube"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="oauth_states")
    platform = models.CharField(max_length=20, choices=Platform.choices)
    state = models.CharField(max_length=255, unique=True)
    callback_uri = models.URLField(
        max_length=500, blank=True, help_text="Exact callback URI sent to provider")
    code_verifier = models.CharField(max_length=255, blank=True)
    login_method = models.CharField(
        max_length=20, blank=True,
        help_text="Connection variant for a platform, e.g. 'instagram' for direct "
                  "Instagram Login vs the default Facebook-based flow")
    redirect_url = models.URLField(
        blank=True, help_text="Frontend URL to redirect to after success/error")
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["platform", "state"]),
            models.Index(fields=["expires_at"]),
            models.Index(fields=["used_at"]),
        ]

    def __str__(self):
        return f"{self.platform} OAuth state for {self.user_id}"

    @property
    def is_expired(self):
        return self.expires_at <= timezone.now()

    @property
    def is_used(self):
        return self.used_at is not None


class Post(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING = "pending", "Pending"
        SCHEDULED = "scheduled", "Scheduled"
        PROCESSING = "processing", "Processing"
        PUBLISHED = "published", "Published"
        PARTIAL = "partial", "Partial"
        FAILED = "failed", "Failed"

    user = models.ForeignKey(settings.AUTH_USER_MODEL,
                             on_delete=models.CASCADE, related_name="posts")
    content = models.TextField(blank=True)

    # ── Image fields ──────────────────────────────────────────────────────
    image = models.URLField(null=True, blank=True, help_text="Single image URL (legacy)")
    images = models.JSONField(default=list, blank=True,
                              help_text="List of image URLs for multi-image posts")
    media_file = models.FileField(upload_to="post_media/", null=True, blank=True,
                                  help_text="Single uploaded image file (legacy)")

    # ── Video fields ──────────────────────────────────────────────────────
    video = models.URLField(null=True, blank=True, help_text="External video URL")
    video_file = models.FileField(upload_to="post_videos/", null=True, blank=True,
                                  help_text="Uploaded video file")

    # ── Per-platform options ──────────────────────────────────────────────
    platform_options = models.JSONField(
        default=dict, blank=True,
        help_text="Per-account posting options keyed by account_id e.g. {'instagram_post_type': 'reel'}"
    )

    content_overrides = models.JSONField(
        default=dict, blank=True,
        help_text="Per-account content overrides keyed by social_account.id"
    )

    # ── Target accounts ───────────────────────────────────────────────────
    # Stores list of SocialAccount IDs to post to
    # e.g. [1, 3, 7] meaning post to accounts with those IDs
    target_accounts = models.JSONField(
        default=list,
        help_text="List of SocialAccount IDs to post to"
    )

    # Legacy field kept for backwards compatibility
    platforms = models.JSONField(
        default=list,
        help_text="Legacy: list of platform names. Use target_accounts instead."
    )

    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.PENDING)
    scheduled_time = models.DateTimeField(null=True, blank=True)
    celery_task_id = models.CharField(max_length=255, null=True, blank=True)
    idempotency_key = models.CharField(
        max_length=64, blank=True, db_index=True,
        help_text="Hash-based key to prevent duplicate post submissions (user_id+content+accounts+time)"
    )
    platform_results = models.JSONField(
        default=dict, blank=True,
        help_text="Results keyed by social_account.id (str)"
    )
    published_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["scheduled_time"]),
        ]

    def __str__(self):
        return f"Post #{self.pk} ({self.status})"

    @property
    def has_video(self):
        return bool(self.video or self.video_file)

    @property
    def all_images(self):
        """Unified list of all image URLs including legacy fields, always absolute."""
        result = list(self.images or [])
        if self.image and self.image not in result:
            result.append(self.image)
        if self.media_file:
            result.append(self.media_file.url)
        return [self._make_absolute(url) for url in result if url]

    @property
    def all_videos(self):
        """Unified list of all video URLs, always absolute."""
        result = []
        if self.video:
            result.append(self.video)
        if self.video_file:
            result.append(self.video_file.url)
        return [self._make_absolute(url) for url in result if url]

    def _make_absolute(self, url):
        if not url:
            return url
        if url.startswith(("http://", "https://")):
            return url
        site_url = getattr(settings, "SITE_URL", "").rstrip("/")
        if not site_url:
            return url
        if not url.startswith("/"):
            url = "/" + url
        return site_url + url

    def get_platform_option(self, platform, key, default=None):
        
        return (self.platform_options or {}).get(platform, {}).get(key, default)

    def get_account_option(self, account_id, key, default=None):
        
        return (self.platform_options or {}).get(str(account_id), {}).get(key, default)


class PostingSchedule(models.Model):
    class Day(models.IntegerChoices):
        MONDAY = 0, "Monday"
        TUESDAY = 1, "Tuesday"
        WEDNESDAY = 2, "Wednesday"
        THURSDAY = 3, "Thursday"
        FRIDAY = 4, "Friday"
        SATURDAY = 5, "Saturday"
        SUNDAY = 6, "Sunday"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="posting_schedules")
    day_of_week = models.IntegerField(choices=Day.choices)
    time = models.TimeField()

    class Meta:
        ordering = ["day_of_week", "time"]
        unique_together = ("user", "day_of_week", "time")

    def __str__(self):
        return f"{self.user} - {self.get_day_of_week_display()} at {self.time}"