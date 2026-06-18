import logging
import os
import requests
from django.conf import settings
from .base import BaseSocialService, SocialPlatformError
from ..cloudinary_utils import (
    upload_image_to_cloudinary,
    upload_video_to_cloudinary,
    get_transformed_url,
)

logger = logging.getLogger(__name__)

GRAPH_VERSION = getattr(settings, "META_GRAPH_API_VERSION", "v23.0")

IG_CONTAINER_FINISHED = "FINISHED"
IG_CONTAINER_FAILED_STATUSES = {"ERROR", "EXPIRED"}

POST_TYPE_FEED = "feed"
POST_TYPE_REEL = "reel"
POST_TYPE_STORY = "story"


class InstagramService(BaseSocialService):
    platform = "instagram"

    def __init__(self, user, account=None):
        super().__init__(user, account=account)
        # Accounts connected via direct "Instagram Login" use the Instagram Graph
        # host with an IG user token; Facebook-Page-linked accounts use the
        # Facebook Graph host with a Page token. Same endpoints/params otherwise.
        if (self.account.metadata or {}).get("login_type") == "instagram":
            self.base_url = f"https://graph.instagram.com/{GRAPH_VERSION}"
        else:
            self.base_url = f"https://graph.facebook.com/{GRAPH_VERSION}"
        self.ig_user_id = self.account.account_id
        self.token = self.account.access_token

    # ── Token refresh ─────────────────────────────────────────────────────

    def refresh_access_token(self):
        """Refresh a direct Instagram-Login token (long-lived, 60-day, refreshable).

        Facebook-Page-linked accounts use non-expiring Page tokens, so there is
        nothing to refresh for them — fall back to the base behaviour.
        """
        from django.utils import timezone
        from datetime import timedelta
        from .oauth import refresh_instagram_login_token

        if (self.account.metadata or {}).get("login_type") != "instagram":
            return super().refresh_access_token()

        payload = refresh_instagram_login_token(self.account.access_token)
        self.account.access_token = payload["access_token"]
        self.token = payload["access_token"]
        expires_in = payload.get("expires_in")
        if expires_in:
            self.account.expires_at = timezone.now() + timedelta(seconds=int(expires_in))
        self.account.save(update_fields=["access_token", "expires_at", "updated_at"])

    # ── Helpers ───────────────────────────────────────────────────────────

    def _graph_post(self, endpoint, params):
        url = f"{self.base_url}/{endpoint}"
        params["access_token"] = self.account.access_token
        resp = requests.post(url, params=params,
                             timeout=settings.SOCIAL_REQUEST_TIMEOUT)
        try:
            result = resp.json()
        except (ValueError, TypeError):
            result = {}

        if not resp.ok:
            error_data = result.get("error", {})
            msg = error_data.get("message") or error_data.get("error_user_msg") or resp.text[:500]
            code = error_data.get("code")
            subcode = error_data.get("error_subcode")
            full_msg = f"Instagram Error: {msg}"
            if code:
                full_msg += f" (code: {code})"
            if subcode:
                full_msg += f" (subcode: {subcode})"
            raise SocialPlatformError(full_msg)

        return result

    def _graph_get(self, endpoint, params=None):
        url = f"{self.base_url}/{endpoint}"
        params = params or {}
        params["access_token"] = self.account.access_token
        resp = requests.get(url, params=params,
                            timeout=settings.SOCIAL_REQUEST_TIMEOUT)
        try:
            result = resp.json()
        except (ValueError, TypeError):
            result = {}

        if not resp.ok:
            error_data = result.get("error", {})
            msg = error_data.get("message") or error_data.get("error_user_msg") or resp.text[:500]
            raise SocialPlatformError(f"Instagram Error: {msg}")

        return result

    def _graph_delete(self, endpoint):
        url = f"{self.base_url}/{endpoint}"
        resp = requests.delete(url, params={"access_token": self.account.access_token},
                               timeout=settings.SOCIAL_REQUEST_TIMEOUT)
        if not resp.ok:
            try:
                error_data = resp.json().get("error", {})
                error = error_data.get("message", "")
                code = error_data.get("code")
                error_lower = error.lower()
                # Code 100/21/33 or specific text indicates post doesn't exist or is already deleted
                if code in (100, 21, 33) or "does not exist" in error_lower or "not found" in error_lower or "unsupported delete request" in error_lower:
                    logger.info("Instagram post already deleted or not found: id=%s", endpoint)
                    return {"success": True, "already_deleted": True}
            except Exception:
                error = resp.text[:500]
            raise SocialPlatformError(f"Instagram Delete Error: {error}")
        return resp.json()

    def _get_post_type(self, post):
        return post.get_platform_option("instagram", "post_type", POST_TYPE_FEED)

    def _is_local_path(self, url):
        if url.startswith(settings.MEDIA_URL):
            return True
        site_url = getattr(settings, "SITE_URL", "")
        if site_url and url.startswith(site_url):
            return True
        return not url.startswith("http://") and not url.startswith("https://")

    def _resolve_local_path(self, url):
        media_url = settings.MEDIA_URL
        site_url = getattr(settings, "SITE_URL", "")
        if site_url and url.startswith(site_url):
            url = url[len(site_url):]

        if url.startswith(media_url):
            relative = url[len(media_url):]
        else:
            relative = url.lstrip("/")
        return os.path.join(settings.MEDIA_ROOT, relative)

    def _ensure_public_image_url(self, url, post_type=POST_TYPE_FEED):
        """
        Ensure image URL is publicly accessible and has a valid aspect ratio for Instagram.
        """
        if not url:
            raise SocialPlatformError("No image URL provided.")

        public_url = url
        if self._is_local_path(url):
            abs_path = self._resolve_local_path(url)
            logger.info("Uploading local image to Cloudinary: %s", abs_path)
            public_url = upload_image_to_cloudinary(abs_path)
        elif "res.cloudinary.com" not in url:
            # For non-Cloudinary remote URLs, we upload them to Cloudinary
            # so we can apply aspect ratio transformations.
            logger.info("Uploading remote image to Cloudinary for transformation: %s", url)
            public_url = upload_image_to_cloudinary(url)

        # Apply aspect ratio transformation
        if post_type == POST_TYPE_STORY:
            transformation = "c_pad,ar_9:16,b_auto"
        else:
            transformation = "c_pad,ar_1:1,b_auto"

        transformed_url = get_transformed_url(public_url, transformation)
        logger.info("Final IG image URL (transformed): %s", transformed_url)
        return transformed_url

    def _ensure_public_video_url(self, url, post_type=POST_TYPE_REEL):
        """
        Ensure video URL is publicly accessible and has a valid aspect ratio (9:16 for Reels/Stories).
        """
        if not url:
            raise SocialPlatformError("No video URL provided.")

        public_url = url
        if self._is_local_path(url):
            abs_path = self._resolve_local_path(url)
            logger.info("Uploading local video to Cloudinary: %s", abs_path)
            public_url = upload_video_to_cloudinary(abs_path)
        elif "res.cloudinary.com" not in url:
            logger.info("Uploading remote video to Cloudinary for transformation: %s", url)
            public_url = upload_video_to_cloudinary(url)

        # Reels and Stories both prefer 9:16
        transformation = "c_pad,ar_9:16,b_auto"
        transformed_url = get_transformed_url(public_url, transformation)
        logger.info("Final IG video URL (transformed): %s", transformed_url)
        return transformed_url

    def _publish_container(self, container_id):
        result = self._graph_post(
            f"{self.ig_user_id}/media_publish",
            {"creation_id": container_id},
        )
        media_id = result.get("id")
        if not media_id:
            raise SocialPlatformError("Instagram publish did not return a media ID.")
        logger.info("IG container published: media_id=%s", media_id)
        return media_id

    # ── Image containers ──────────────────────────────────────────────────

    def _create_image_container(self, image_url, caption="", is_carousel_item=False, post_type=POST_TYPE_FEED):
        public_url = self._ensure_public_image_url(image_url, post_type=post_type)
        logger.info("IG sending image_url to API: %s", public_url)  
        params = {"image_url": public_url}
        if is_carousel_item:
            params["is_carousel_item"] = "true"
        else:
            params["caption"] = caption
        result = self._graph_post(f"{self.ig_user_id}/media", params)
        container_id = result.get("id")
        if not container_id:
            raise SocialPlatformError("Instagram image container creation failed.")
        logger.info("IG image container created: id=%s carousel=%s",
                    container_id, is_carousel_item)
        return container_id

    def _create_story_image_container(self, image_url):
        public_url = self._ensure_public_image_url(image_url, post_type=POST_TYPE_STORY)
        result = self._graph_post(
            f"{self.ig_user_id}/media",
            {"media_type": "STORIES", "image_url": public_url},
        )
        container_id = result.get("id")
        if not container_id:
            raise SocialPlatformError("Instagram Story image container creation failed.")
        logger.info("IG story image container created: id=%s", container_id)
        return container_id

    def _create_carousel_container(self, children_ids, caption=""):
        result = self._graph_post(
            f"{self.ig_user_id}/media",
            {
                "media_type": "CAROUSEL",
                "children": ",".join(children_ids),
                "caption": caption,
            },
        )
        container_id = result.get("id")
        if not container_id:
            raise SocialPlatformError("Instagram carousel container creation failed.")
        logger.info("IG carousel container created: id=%s children=%d",
                    container_id, len(children_ids))
        return container_id

    # ── Video containers ──────────────────────────────────────────────────

    def _create_reel_container(self, video_url, caption=""):
        public_url = self._ensure_public_video_url(video_url, post_type=POST_TYPE_REEL)
        result = self._graph_post(
            f"{self.ig_user_id}/media",
            {
                "media_type": "REELS",
                "video_url": public_url,
                "caption": caption,
                "share_to_feed": "true",
            },
        )
        container_id = result.get("id")
        if not container_id:
            raise SocialPlatformError("Instagram Reel container creation failed.")
        logger.info("IG Reel container created: id=%s", container_id)
        return container_id

    def _create_story_video_container(self, video_url):
        public_url = self._ensure_public_video_url(video_url, post_type=POST_TYPE_STORY)
        result = self._graph_post(
            f"{self.ig_user_id}/media",
            {
                "media_type": "STORIES",
                "video_url": public_url,
            },
        )
        container_id = result.get("id")
        if not container_id:
            raise SocialPlatformError("Instagram Story video container creation failed.")
        logger.info("IG story video container created: id=%s", container_id)
        return container_id

    # ── Video upload (async entry point from tasks) ───────────────────────

    def upload_video(self, post):
        """
        Called by tasks._process_video_post.
        Handles uploaded files, local paths, and public URLs.
        Local files are uploaded to Cloudinary automatically.
        """
        post_type = self._get_post_type(post)
        video_url = None

        if post.video_file:
            abs_path = post.video_file.path
            logger.info("Uploading video file to Cloudinary for Instagram: %s", abs_path)
            video_url = upload_video_to_cloudinary(abs_path)
        elif post.video:
            raw_url = post.video.strip()
            if self._is_local_path(raw_url):
                abs_path = self._resolve_local_path(raw_url)
                logger.info("Uploading local video to Cloudinary for Instagram: %s", abs_path)
                video_url = upload_video_to_cloudinary(abs_path)
            else:
                video_url = raw_url
        else:
            raise SocialPlatformError("Post has no video for Instagram.")

        if post_type == POST_TYPE_STORY:
            container_id = self._create_story_video_container(video_url)
        else:
            container_id = self._create_reel_container(video_url, post.content)

        logger.info("IG video container created: type=%s id=%s", post_type, container_id)
        return container_id

    def get_video_asset_status(self, container_id):
        result = self._graph_get(container_id, {"fields": "status_code,status"})
        status_code = result.get("status_code", "IN_PROGRESS")
        logger.info("IG container %s status_code=%s", container_id, status_code)
        if status_code == IG_CONTAINER_FINISHED:
            return "AVAILABLE"
        elif status_code in IG_CONTAINER_FAILED_STATUSES:
            return "FAILED"
        else:
            return "PROCESSING"

    def publish_video_post(self, post, container_id):
        media_id = self._publish_container(container_id)
        post_type = self._get_post_type(post)
        logger.info("IG video post published: type=%s media_id=%s", post_type, media_id)
        return {"post_urn": media_id, "post_id": media_id}

    # ── Main entry point ─────────────────────────────────────────────────

    def create_post(self, post):
        post_type = self._get_post_type(post)
        all_images = post.all_images

        logger.info("IG create_post: type=%s images=%d has_video=%s",
                    post_type, len(all_images), post.has_video)

        # Story
        if post_type == POST_TYPE_STORY:
            if post.has_video:
                raise SocialPlatformError(
                    "Video stories are processed asynchronously. "
                    "The post will be published once the video is ready."
                )
            if not all_images:
                raise SocialPlatformError("Instagram Stories require an image or video.")
            container_id = self._create_story_image_container(all_images[0])
            media_id = self._publish_container(container_id)
            logger.info("IG image story published: media_id=%s", media_id)
            return {"post_id": media_id}

        # Feed
        if not all_images:
            raise SocialPlatformError(
                "Instagram feed posts require at least one image. "
                "For video posts, select 'Reel' as the post type."
            )

        # Carousel
        if len(all_images) > 1:
            if len(all_images) > 10:
                raise SocialPlatformError("Instagram carousel supports a maximum of 10 images.")
            children_ids = [
                self._create_image_container(img_url, is_carousel_item=True, post_type=post_type)
                for img_url in all_images
            ]
            carousel_id = self._create_carousel_container(children_ids, post.content)
            media_id = self._publish_container(carousel_id)
            logger.info("IG carousel published: media_id=%s", media_id)
            return {"post_id": media_id}

        # Single image
        container_id = self._create_image_container(all_images[0], post.content, post_type=post_type)
        media_id = self._publish_container(container_id)
        logger.info("IG single image published: media_id=%s", media_id)
        return {"post_id": media_id}

    def delete_post(self, post_id):
        logger.info("Deleting IG post: post_id=%s", post_id)
        result = self._graph_delete(post_id)
        logger.info("IG post deleted: result=%s", result)
        return result