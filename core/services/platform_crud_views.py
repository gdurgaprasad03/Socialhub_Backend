"""
Additional CRUD views for platform-specific operations on published posts.
Add these to your existing views.py and urls.py.
"""
import logging
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import Post, SocialAccount
from .platform_crud_service import get_platform_post, update_platform_post

logger = logging.getLogger(__name__)

# Platforms that support updating posts
UPDATABLE_PLATFORMS = {"facebook", "instagram", "youtube"}

# Platforms that support reading posts
READABLE_PLATFORMS = {"linkedin", "facebook", "instagram", "twitter", "youtube"}


class GetPlatformPostView(APIView):
    """
    GET /api/posts/{pk}/account/{account_id}/read/
    Fetch the live post data from the platform.
    Useful to verify post was published and get current stats.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request, pk, account_id):
        try:
            post = Post.objects.get(pk=pk, user=request.user)
        except Post.DoesNotExist:
            return Response({"error": "Post not found"}, status=status.HTTP_404_NOT_FOUND)

        if post.status not in [Post.Status.PUBLISHED, Post.Status.PARTIAL]:
            return Response(
                {"error": "Post is not published yet."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        account_key = str(account_id)
        platform_result = post.platform_results.get(account_key)
        if not platform_result or not platform_result.get("success"):
            return Response(
                {"error": f"No successful publish record for account {account_id}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        post_urn = platform_result.get("post_urn") or platform_result.get("post_id")
        if not post_urn:
            return Response(
                {"error": "No post URN stored for this account."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            account = SocialAccount.objects.get(id=int(account_id), user=request.user)
        except SocialAccount.DoesNotExist:
            return Response({"error": "Account not found."}, status=status.HTTP_404_NOT_FOUND)

        if account.platform not in READABLE_PLATFORMS:
            return Response(
                {"error": f"{account.platform} does not support reading posts via API."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            platform_data = get_platform_post(
                account.platform, post_urn, account.access_token
            )
            return Response({
                "post_id": pk,
                "account_id": account_id,
                "platform": account.platform,
                "display_name": account.display_name,
                "post_urn": post_urn,
                "platform_data": platform_data,
            })
        except Exception as exc:
            logger.exception("Error reading post from platform: post=%s account=%s", pk, account_id)
            return Response(
                {"error": f"Failed to read from {account.platform}: {str(exc)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )


class UpdatePlatformPostView(APIView):
    """
    PATCH /api/posts/{pk}/account/{account_id}/update/
    Update a published post on the platform.

    Supported updates per platform:
    - Facebook: message (text content)
    - Instagram: caption
    - YouTube: title, description, privacy

    Not supported:
    - LinkedIn: API does not support post updates
    - Twitter: API does not support tweet updates
    """
    permission_classes = [IsAuthenticated]

    def patch(self, request, pk, account_id):
        try:
            post = Post.objects.get(pk=pk, user=request.user)
        except Post.DoesNotExist:
            return Response({"error": "Post not found"}, status=status.HTTP_404_NOT_FOUND)

        if post.status not in [Post.Status.PUBLISHED, Post.Status.PARTIAL]:
            return Response(
                {"error": "Only published posts can be updated on platforms."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        account_key = str(account_id)
        platform_result = post.platform_results.get(account_key)
        if not platform_result or not platform_result.get("success"):
            return Response(
                {"error": f"No successful publish record for account {account_id}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        post_urn = platform_result.get("post_urn") or platform_result.get("post_id")
        if not post_urn:
            return Response(
                {"error": "No post URN stored for this account."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            account = SocialAccount.objects.get(id=int(account_id), user=request.user)
        except SocialAccount.DoesNotExist:
            return Response({"error": "Account not found."}, status=status.HTTP_404_NOT_FOUND)

        if account.platform not in UPDATABLE_PLATFORMS:
            return Response(
                {
                    "error": f"{account.platform.title()} does not support updating posts via API.",
                    "supported_platforms": list(UPDATABLE_PLATFORMS),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Extract update fields from request
        update_kwargs = {}
        if "content" in request.data:
            update_kwargs["message"] = request.data["content"]
            update_kwargs["caption"] = request.data["content"]
            update_kwargs["description"] = request.data["content"]
        if "message" in request.data:
            update_kwargs["message"] = request.data["message"]
        if "caption" in request.data:
            update_kwargs["caption"] = request.data["caption"]
        if "title" in request.data:
            update_kwargs["title"] = request.data["title"]
        if "description" in request.data:
            update_kwargs["description"] = request.data["description"]
        if "privacy" in request.data:
            if request.data["privacy"] not in ("public", "private", "unlisted"):
                return Response(
                    {"error": "privacy must be one of: public, private, unlisted"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            update_kwargs["privacy"] = request.data["privacy"]

        if not update_kwargs:
            return Response(
                {"error": "No updatable fields provided. Send content, title, description, or privacy."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = update_platform_post(
                account.platform, post_urn, account.access_token, **update_kwargs
            )

            # Update local post content if content was changed
            if "content" in request.data or "message" in request.data or "caption" in request.data:
                new_content = (
                    request.data.get("content") or
                    request.data.get("message") or
                    request.data.get("caption", "")
                )
                Post.objects.filter(id=pk).update(content=new_content)

            return Response({
                "message": f"Post updated on {account.platform} successfully.",
                "account_id": account_id,
                "platform": account.platform,
                "display_name": account.display_name,
                "post_urn": post_urn,
                "platform_response": result,
            })

        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            logger.exception("Error updating post on platform: post=%s account=%s", pk, account_id)
            return Response(
                {"error": f"Failed to update on {account.platform}: {str(exc)}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )


class AutoSaveDraftView(APIView):
    """
    POST /api/posts/autosave/
    Auto-save current form state as a draft.
    Called by frontend when user navigates away or closes browser.
    Updates existing draft if draft_id provided, otherwise creates new draft.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        import json
        from django.core.files.storage import default_storage
        from django.conf import settings as django_settings

        try:
            draft_id = request.data.get("draft_id")
            content = request.data.get("content", "")
            target_accounts_raw = request.data.get("target_accounts", [])
            platform_options_raw = request.data.get("platform_options", {})

            # Parse target_accounts
            if isinstance(target_accounts_raw, str):
                try:
                    target_accounts = json.loads(target_accounts_raw)
                except Exception:
                    target_accounts = []
            else:
                target_accounts = list(target_accounts_raw) if target_accounts_raw else []

            # Parse platform_options
            if isinstance(platform_options_raw, str):
                try:
                    platform_options = json.loads(platform_options_raw)
                except Exception:
                    platform_options = {}
            else:
                platform_options = platform_options_raw or {}

            # Handle uploaded files
            uploaded_files = request.FILES.getlist("media_files")
            extra_image_urls = []
            for f in uploaded_files:
                file_path = default_storage.save(f"post_media/{f.name}", f)
                extra_image_urls.append(django_settings.MEDIA_URL + file_path)

            raw_images = request.data.get("images", "")
            existing_images = []
            if isinstance(raw_images, list):
                existing_images = raw_images
            elif isinstance(raw_images, str) and raw_images.strip():
                try:
                    parsed = json.loads(raw_images)
                    if isinstance(parsed, list):
                        existing_images = parsed
                except Exception:
                    existing_images = []

            all_images = existing_images + extra_image_urls

            # Validate target_accounts belong to user
            valid_account_ids = []
            if target_accounts:
                valid_account_ids = list(
                    SocialAccount.objects.filter(
                        user=request.user, id__in=target_accounts
                    ).values_list("id", flat=True)
                )

            # Build platforms list for legacy compatibility
            platforms = list(
                SocialAccount.objects.filter(
                    user=request.user, id__in=valid_account_ids
                ).values_list("platform", flat=True).distinct()
            ) if valid_account_ids else []

            if draft_id:
                # Update existing draft
                try:
                    draft = Post.objects.get(
                        pk=draft_id, user=request.user, status=Post.Status.DRAFT
                    )
                    draft.content = content
                    draft.target_accounts = valid_account_ids
                    draft.platforms = platforms
                    draft.platform_options = platform_options
                    if all_images:
                        draft.images = all_images
                    draft.save()
                    return Response({
                        "message": "Draft updated.",
                        "draft_id": draft.id,
                        "status": "draft",
                    })
                except Post.DoesNotExist:
                    pass  # Fall through to create new draft

            # Create new draft
            draft = Post.objects.create(
                user=request.user,
                content=content,
                target_accounts=valid_account_ids,
                platforms=platforms,
                platform_options=platform_options,
                images=all_images,
                status=Post.Status.DRAFT,
            )

            return Response({
                "message": "Draft saved.",
                "draft_id": draft.id,
                "status": "draft",
            }, status=status.HTTP_201_CREATED)

        except Exception as exc:
            logger.exception("Error auto-saving draft")
            return Response(
                {"error": f"Failed to save draft: {str(exc)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )