import json

from django.contrib.auth import authenticate, get_user_model
from django.utils import timezone
from rest_framework import serializers
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Post, SocialAccount, PostingSchedule

User = get_user_model()

SUPPORTED_PLATFORMS = {choice[0] for choice in SocialAccount.Platform.choices}
INSTAGRAM_POST_TYPES = {"feed", "reel", "story"}
MAX_ACCOUNTS_PER_PLATFORM = 10  # Safety cap per platform; plan total limit is enforced separately


class RegisterSerializer(serializers.ModelSerializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True, min_length=8)

    class Meta:
        model = User
        fields = ["id", "username", "email", "password"]
        read_only_fields = ["id"]

    def validate_email(self, value):
        normalized = value.strip().lower()
        if User.objects.filter(email__iexact=normalized).exists():
            raise serializers.ValidationError("Email already exists")
        return normalized

    def validate_username(self, value):
        normalized = value.strip()
        if not normalized:
            raise serializers.ValidationError("Username is required")
        if User.objects.filter(username__iexact=normalized).exists():
            raise serializers.ValidationError("Username already exists")
        return normalized

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class LoginSerializer(serializers.Serializer):
    
    identifier = serializers.CharField(required=False, allow_blank=True, write_only=True)
    email = serializers.CharField(required=False, allow_blank=True)
    username = serializers.CharField(required=False, allow_blank=True)
    password = serializers.CharField(write_only=True)

    def validate(self, data):
        username_or_email = (
            data.get("identifier") or data.get("email") or data.get("username") or ""
        ).strip()
        password = data.get("password")

        if not username_or_email or not password:
            raise serializers.ValidationError(
                "Email or username and password are required"
            )

        if "@" in username_or_email:
            user_obj = User.objects.filter(email__iexact=username_or_email).first()
        else:
            user_obj = (
                User.objects.filter(username__iexact=username_or_email).first()
                or User.objects.filter(email__iexact=username_or_email).first()
            )

        user = None
        if user_obj:
            user = authenticate(username=user_obj.username, password=password)

        if not user:
            raise serializers.ValidationError("Invalid credentials")
        if not user.is_active:
            raise serializers.ValidationError("User inactive")

        refresh = RefreshToken.for_user(user)
        return {
            "user_id": user.id,
            "username": user.username,
            "email": user.email,
            "access": str(refresh.access_token),
            "refresh": str(refresh),
        }


class SocialAccountSerializer(serializers.ModelSerializer):
    access_token = serializers.CharField(write_only=True)
    refresh_token = serializers.CharField(write_only=True, required=False, allow_blank=True)
    is_expired = serializers.BooleanField(read_only=True)
    display_name = serializers.CharField(read_only=True)

    class Meta:
        model = SocialAccount
        fields = [
            "id", "user", "platform", "account_type", "account_label", "display_name",
            "access_token", "refresh_token",
            "token_type", "account_id", "platform_username", "metadata",
            "expires_at", "is_expired", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "user", "account_type", "token_type", "account_id", "platform_username",
            "metadata", "expires_at", "is_expired", "display_name",
            "created_at", "updated_at",
        ]

    def validate_platform(self, value):
        cleaned = value.strip().lower() if isinstance(value, str) else value
        if cleaned not in SUPPORTED_PLATFORMS:
            raise serializers.ValidationError("Unsupported platform")
        return cleaned

    def validate(self, data):
        user = self.context["request"].user
        platform = data.get("platform", getattr(self.instance, "platform", None))
        account_id = data.get("account_id", getattr(self.instance, "account_id", None))

        # Check max accounts per platform (safety cap)
        existing_count = SocialAccount.objects.filter(
            user=user, platform=platform
        )
        if self.instance:
            existing_count = existing_count.exclude(pk=self.instance.pk)

        if existing_count.count() >= MAX_ACCOUNTS_PER_PLATFORM:
            raise serializers.ValidationError(
                f"Maximum {MAX_ACCOUNTS_PER_PLATFORM} {platform} accounts allowed per user."
            )

        # Check plan total account limit
        try:
            from billing.views import get_or_create_subscription
            sub = get_or_create_subscription(user)
            max_accounts = sub.plan.max_accounts
            if max_accounts != -1:
                total_count = SocialAccount.objects.filter(user=user)
                if self.instance:
                    total_count = total_count.exclude(pk=self.instance.pk)
                if total_count.count() >= max_accounts:
                    raise serializers.ValidationError(
                        f"Your {sub.plan.name} plan allows {max_accounts} connected account(s). "
                        "Please upgrade to connect more."
                    )
        except serializers.ValidationError:
            raise
        except Exception:
            pass  # Fail open — plan check is also enforced at OAuth start

        # Check duplicate account_id for same platform
        if account_id:
            duplicate = SocialAccount.objects.filter(
                user=user, platform=platform, account_id=account_id
            )
            if self.instance:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                raise serializers.ValidationError(
                    f"This {platform} account is already connected."
                )

        return data


class PostSerializer(serializers.ModelSerializer):
    # Read-only field showing account details for each target account
    target_account_details = serializers.SerializerMethodField()

    class Meta:
        model = Post
        fields = [
            "id", "user", "content",
            "image", "images", "media_file",
            "video", "video_file",
            "platform_options",
            "target_accounts", "target_account_details",
            "platforms",  # kept for legacy compatibility
            "status", "scheduled_time",
            "celery_task_id", "platform_results",
            "published_at", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "user", "status", "celery_task_id",
            "platform_results", "published_at", "created_at", "updated_at",
            "target_account_details",
        ]

    def get_target_account_details(self, obj):
        """Return basic info about each target account for frontend display."""
        if not obj.target_accounts:
            return []
        accounts = SocialAccount.objects.filter(
            id__in=obj.target_accounts, user=obj.user
        )
        return [
            {
                "id": acc.id,
                "platform": acc.platform,
                "display_name": acc.display_name,
                "platform_username": acc.platform_username,
            }
            for acc in accounts
        ]

    def validate_target_accounts(self, value):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                raise serializers.ValidationError(
                    "target_accounts must be a valid JSON array of account IDs"
                )

        if not isinstance(value, list):
            raise serializers.ValidationError("target_accounts must be a list")

        if not value:
            raise serializers.ValidationError(
                "At least one target account is required"
            )

        # Validate all IDs are integers
        validated = []
        for item in value:
            try:
                validated.append(int(item))
            except (ValueError, TypeError):
                raise serializers.ValidationError(
                    f"Invalid account ID: {item}. Must be an integer."
                )

        return list(dict.fromkeys(validated))  # deduplicate preserving order

    def validate_images(self, value):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                raise serializers.ValidationError(
                    "images must be a valid JSON array of URLs"
                )

        if not isinstance(value, list):
            raise serializers.ValidationError("images must be a list")

        validated = []
        for url in value:
            if not isinstance(url, str) or not url.strip():
                continue
            url = url.strip()
            if not url.startswith(("http://", "https://", "/media/")):
                raise serializers.ValidationError(f"Invalid URL in images list: {url}")
            validated.append(url)
        return validated

    def validate_platform_options(self, value):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                raise serializers.ValidationError(
                    "platform_options must be a valid JSON object"
                )

        if not isinstance(value, dict):
            raise serializers.ValidationError("platform_options must be an object")

        return value

    def validate_scheduled_time(self, value):
        if value and value <= timezone.now():
            raise serializers.ValidationError("scheduled_time must be in the future")
        return value

    def validate(self, data):
        user = self.context["request"].user

        content = (
            data.get("content") or getattr(self.instance, "content", "") or ""
        ).strip()

        image = data.get("image", getattr(self.instance, "image", None))
        images = data.get("images", getattr(self.instance, "images", []))
        media_file = data.get("media_file", getattr(self.instance, "media_file", None))
        video = data.get("video", getattr(self.instance, "video", None))
        video_file = data.get("video_file", getattr(self.instance, "video_file", None))

        has_image = bool(image or images or media_file)
        has_video = bool(video or video_file)

        if has_image and has_video:
            raise serializers.ValidationError(
                "A post cannot have both images and video. Choose one."
            )

        if not content and not has_image and not has_video:
            raise serializers.ValidationError(
                "Post must have content, at least one image, or a video."
            )

        # Validate target accounts belong to this user
        target_accounts = data.get(
            "target_accounts",
            getattr(self.instance, "target_accounts", [])
        ) or []

        if target_accounts:
            valid_ids = set(
                SocialAccount.objects.filter(
                    user=user, id__in=target_accounts
                ).values_list("id", flat=True)
            )
            invalid = [aid for aid in target_accounts if aid not in valid_ids]
            if invalid:
                raise serializers.ValidationError(
                    f"Invalid or unconnected account IDs: {invalid}"
                )

            # Build platforms list from target accounts for legacy compatibility
            platform_list = list(
                SocialAccount.objects.filter(
                    user=user, id__in=target_accounts
                ).values_list("platform", flat=True).distinct()
            )
            data["platforms"] = platform_list

            # Instagram-specific validation
            instagram_accounts = SocialAccount.objects.filter(
                user=user, id__in=target_accounts, platform="instagram"
            )
            platform_options = data.get(
                "platform_options",
                getattr(self.instance, "platform_options", {})
            ) or {}

            for ig_account in instagram_accounts:
                account_options = platform_options.get(str(ig_account.id), {})
                ig_post_type = account_options.get("post_type", "feed")

                if ig_post_type == "reel" and not has_video:
                    raise serializers.ValidationError(
                        f"Instagram Reels require a video URL (account: {ig_account.display_name})."
                    )
                elif ig_post_type == "story" and not has_image and not has_video:
                    raise serializers.ValidationError(
                        f"Instagram Stories require an image or video (account: {ig_account.display_name})."
                    )
                elif ig_post_type == "feed" and not has_image and not has_video:
                    raise serializers.ValidationError(
                        f"Instagram feed posts require an image (account: {ig_account.display_name})."
                    )

        return data


class PostingScheduleSerializer(serializers.ModelSerializer):
    day_of_week_display = serializers.CharField(
        source="get_day_of_week_display", read_only=True)

    class Meta:
        model = PostingSchedule
        fields = ["id", "day_of_week", "day_of_week_display", "time"]
        read_only_fields = ["id"]