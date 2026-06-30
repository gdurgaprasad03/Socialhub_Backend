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

THREADS_GRAPH_BASE = "https://graph.threads.net"
THREADS_API_VERSION = "v1.0"

THREADS_CONTAINER_FINISHED = "FINISHED"
THREADS_CONTAINER_FAILED_STATUSES = {"ERROR", "EXPIRED"}


class ThreadsService(BaseSocialService):
    platform = "threads"

    def __init__(self, user, account=None):
        super().__init__(user, account=account)
        self.base_url = f"{THREADS_GRAPH_BASE}/{THREADS_API_VERSION}"
        self.user_id = self.account.account_id
        self.token = self.account.access_token

    # ── Token refresh ─────────────────────────────────────────────────────

    def refresh_access_token(self):
        from django.utils import timezone
        from datetime import timedelta
        from .oauth import refresh_threads_token

        payload = refresh_threads_token(self.account.access_token)
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
        resp = requests.post(url, params=params, timeout=settings.SOCIAL_REQUEST_TIMEOUT)
        try:
            result = resp.json()
        except (ValueError, TypeError):
            result = {}

        if not resp.ok:
            error_data = result.get("error", {})
            msg = error_data.get("message") or error_data.get("error_user_msg") or resp.text[:500]
            code = error_data.get("code")
            full_msg = f"Threads Error: {msg}"
            if code:
                full_msg += f" (code: {code})"
            raise SocialPlatformError(full_msg)

        return result

    def _graph_get(self, endpoint, params=None):
        url = f"{self.base_url}/{endpoint}"
        params = params or {}
        params["access_token"] = self.account.access_token
        resp = requests.get(url, params=params, timeout=settings.SOCIAL_REQUEST_TIMEOUT)
        try:
            result = resp.json()
        except (ValueError, TypeError):
            result = {}

        if not resp.ok:
            error_data = result.get("error", {})
            msg = error_data.get("message") or resp.text[:500]
            raise SocialPlatformError(f"Threads Error: {msg}")

        return result

    def _graph_delete(self, endpoint):
        url = f"{self.base_url}/{endpoint}"
        resp = requests.delete(
            url,
            params={"access_token": self.account.access_token},
            timeout=settings.SOCIAL_REQUEST_TIMEOUT,
        )
        if not resp.ok:
            try:
                error_data = resp.json().get("error", {})
                error = error_data.get("message", "")
                code = error_data.get("code")
                error_lower = error.lower()
                if code in (100, 21, 33) or "does not exist" in error_lower or "not found" in error_lower:
                    logger.info("Threads post already deleted or not found: id=%s", endpoint)
                    return {"success": True, "already_deleted": True}
            except Exception:
                error = resp.text[:500]
            raise SocialPlatformError(f"Threads Delete Error: {error}")
        return resp.json() if resp.content else {"success": True}

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

    def _ensure_public_image_url(self, url):
        if not url:
            raise SocialPlatformError("No image URL provided.")
        if self._is_local_path(url):
            abs_path = self._resolve_local_path(url)
            public_url, _ = upload_image_to_cloudinary(abs_path)
        elif "res.cloudinary.com" not in url:
            public_url, _ = upload_image_to_cloudinary(url)
        else:
            public_url = url
        return public_url

    def _ensure_public_video_url(self, url):
        if not url:
            raise SocialPlatformError("No video URL provided.")
        if self._is_local_path(url):
            abs_path = self._resolve_local_path(url)
            return upload_video_to_cloudinary(abs_path)
        elif "res.cloudinary.com" not in url:
            return upload_video_to_cloudinary(url)
        return url

    # ── Container creation ────────────────────────────────────────────────

    def _create_text_container(self, text):
        result = self._graph_post(
            f"{self.user_id}/threads",
            {"media_type": "TEXT", "text": text},
        )
        container_id = result.get("id")
        if not container_id:
            raise SocialPlatformError("Threads text container creation failed.")
        logger.info("Threads text container created: id=%s", container_id)
        return container_id

    def _create_image_container(self, image_url, text="", is_carousel_item=False):
        public_url = self._ensure_public_image_url(image_url)
        params = {"media_type": "IMAGE", "image_url": public_url}
        if is_carousel_item:
            params["is_carousel_item"] = "true"
        else:
            params["text"] = text
        result = self._graph_post(f"{self.user_id}/threads", params)
        container_id = result.get("id")
        if not container_id:
            raise SocialPlatformError("Threads image container creation failed.")
        logger.info("Threads image container created: id=%s carousel=%s", container_id, is_carousel_item)
        return container_id

    def _create_video_container(self, video_url, text="", is_carousel_item=False):
        public_url = self._ensure_public_video_url(video_url)
        params = {"media_type": "VIDEO", "video_url": public_url}
        if is_carousel_item:
            params["is_carousel_item"] = "true"
        else:
            params["text"] = text
        result = self._graph_post(f"{self.user_id}/threads", params)
        container_id = result.get("id")
        if not container_id:
            raise SocialPlatformError("Threads video container creation failed.")
        logger.info("Threads video container created: id=%s", container_id)
        return container_id

    def _create_carousel_container(self, children_ids, text=""):
        result = self._graph_post(
            f"{self.user_id}/threads",
            {
                "media_type": "CAROUSEL",
                "children": ",".join(children_ids),
                "text": text,
            },
        )
        container_id = result.get("id")
        if not container_id:
            raise SocialPlatformError("Threads carousel container creation failed.")
        logger.info("Threads carousel container created: id=%s children=%d", container_id, len(children_ids))
        return container_id

    def _publish_container(self, container_id):
        result = self._graph_post(
            f"{self.user_id}/threads_publish",
            {"creation_id": container_id},
        )
        media_id = result.get("id")
        if not media_id:
            raise SocialPlatformError("Threads publish did not return a media ID.")
        logger.info("Threads container published: media_id=%s", media_id)
        return media_id

    # ── Async video support ───────────────────────────────────────────────

    def upload_video(self, post):
        video_url = None

        if post.video_file:
            abs_path = post.video_file.path
            logger.info("Uploading video file to Cloudinary for Threads: %s", abs_path)
            video_url = upload_video_to_cloudinary(abs_path)
        elif post.video:
            raw_url = post.video.strip()
            if self._is_local_path(raw_url):
                abs_path = self._resolve_local_path(raw_url)
                video_url = upload_video_to_cloudinary(abs_path)
            else:
                video_url = raw_url
        else:
            raise SocialPlatformError("Post has no video for Threads.")

        container_id = self._create_video_container(video_url, post.content)
        logger.info("Threads video container created: id=%s", container_id)
        return container_id

    def get_video_asset_status(self, container_id):
        result = self._graph_get(container_id, {"fields": "status,error_message"})
        status_val = result.get("status", "IN_PROGRESS")
        logger.info("Threads container %s status=%s", container_id, status_val)
        if status_val == THREADS_CONTAINER_FINISHED:
            return "AVAILABLE"
        elif status_val in THREADS_CONTAINER_FAILED_STATUSES:
            return "FAILED"
        return "PROCESSING"

    def publish_video_post(self, post, container_id):
        media_id = self._publish_container(container_id)
        logger.info("Threads video post published: media_id=%s", media_id)
        return {"post_urn": media_id, "post_id": media_id}

    # ── Main entry point ─────────────────────────────────────────────────

    def create_post(self, post):
        all_images = post.all_images

        logger.info(
            "Threads create_post: images=%d has_video=%s",
            len(all_images), post.has_video,
        )

        # Video post (async processing)
        if post.has_video:
            raise SocialPlatformError(
                "Video posts are processed asynchronously. "
                "The post will be published once the video is ready."
            )

        # Carousel (2–20 images)
        if len(all_images) > 1:
            if len(all_images) > 20:
                raise SocialPlatformError("Threads carousel supports a maximum of 20 items.")
            children_ids = [
                self._create_image_container(img_url, is_carousel_item=True)
                for img_url in all_images
            ]
            carousel_id = self._create_carousel_container(children_ids, post.content)
            media_id = self._publish_container(carousel_id)
            logger.info("Threads carousel published: media_id=%s", media_id)
            return {"post_id": media_id}

        # Single image
        if len(all_images) == 1:
            container_id = self._create_image_container(all_images[0], post.content)
            media_id = self._publish_container(container_id)
            logger.info("Threads single image published: media_id=%s", media_id)
            return {"post_id": media_id}

        # Text-only post
        if not post.content:
            raise SocialPlatformError("Threads requires either text content or at least one image/video.")
        container_id = self._create_text_container(post.content)
        media_id = self._publish_container(container_id)
        logger.info("Threads text post published: media_id=%s", media_id)
        return {"post_id": media_id}

    def delete_post(self, post_id):
        logger.info("Deleting Threads post: post_id=%s", post_id)
        result = self._graph_delete(post_id)
        logger.info("Threads post deleted: result=%s", result)
        return result
