import logging
import os
import requests
from django.conf import settings
from .base import BaseSocialService, SocialPlatformError

logger = logging.getLogger(__name__)

GRAPH_VERSION = getattr(settings, "META_GRAPH_API_VERSION", "v23.0")


class FacebookService(BaseSocialService):
    platform = "facebook"

    def __init__(self, user, account=None):
        super().__init__(user, account=account)
        self.base_url = f"https://graph.facebook.com/{GRAPH_VERSION}"
        self.page_id = self.account.account_id
        self.token = self.account.access_token

    # ── Helpers ───────────────────────────────────────────────────────────

    def _is_local_path(self, url):
        if url.startswith(settings.MEDIA_URL):
            return True
        site_url = getattr(settings, "SITE_URL", "")
        if site_url and url.startswith(site_url + settings.MEDIA_URL):
            return True
        return not url.startswith("http://") and not url.startswith("https://")

    def _resolve_local_path(self, url):
        media_url = settings.MEDIA_URL
        site_url = getattr(settings, "SITE_URL", "")
        
        # If it's an absolute URL pointing to our SITE_URL, strip the domain
        if site_url and url.startswith(site_url):
            url = url[len(site_url):]

        relative = url[len(media_url):] if url.startswith(media_url) else url.lstrip("/")
        return os.path.join(settings.MEDIA_ROOT, relative)

    def _graph_post(self, endpoint, data=None, files=None):
        """POST to Graph API endpoint. Returns parsed JSON."""
        url = f"{self.base_url}/{endpoint}"
        if files:
            resp = requests.post(url, data=data, files=files,
                                 timeout=settings.SOCIAL_REQUEST_TIMEOUT)
        else:
            resp = requests.post(url, data=data,
                                 timeout=settings.SOCIAL_REQUEST_TIMEOUT)
        resp.raise_for_status()
        result = resp.json()
        if "error" in result:
            raise SocialPlatformError(str(result["error"]))
        return result

    def _graph_delete(self, endpoint):
        url = f"{self.base_url}/{endpoint}"
        resp = requests.delete(url, params={"access_token": self.token},
                               timeout=settings.SOCIAL_REQUEST_TIMEOUT)
        if not resp.ok:
            try:
                error = resp.json().get("error", {})
                msg = error.get("message") or error.get("error_user_msg") or resp.text[:500]
                code = error.get("code", "")
                full_msg = f"Facebook Delete Error: {msg}"
                if code:
                    full_msg += f" (code: {code})"
                raise SocialPlatformError(full_msg)
            except SocialPlatformError:
                raise
            except Exception:
                raise SocialPlatformError(f"Facebook Delete Error: {resp.text[:500]}")
        result = resp.json()
        if isinstance(result, dict) and "error" in result:
            raise SocialPlatformError(str(result["error"]))
        return result

    # ── Image upload helpers ──────────────────────────────────────────────

    def _upload_photo_unpublished(self, image_source):
        """
        Upload a single photo unpublished (for use in multi-photo posts).
        image_source: URL string or local path string.
        Returns photo ID.
        """
        if self._is_local_path(image_source):
            abs_path = self._resolve_local_path(image_source)
            if not os.path.exists(abs_path):
                raise SocialPlatformError(f"Local image file not found: {abs_path}")
            with open(abs_path, "rb") as f:
                result = self._graph_post(
                    f"{self.page_id}/photos",
                    data={"published": "false", "access_token": self.token},
                    files={"source": f},
                )
        else:
            result = self._graph_post(
                f"{self.page_id}/photos",
                data={
                    "url": image_source,
                    "published": "false",
                    "access_token": self.token,
                },
            )
        photo_id = result.get("id")
        if not photo_id:
            raise SocialPlatformError("Facebook photo upload did not return an ID.")
        logger.info("Uploaded unpublished FB photo: id=%s", photo_id)
        return photo_id

    def _upload_single_photo_published(self, image_source, caption=""):
        """Publish a single photo post directly. Returns post ID."""
        if self._is_local_path(image_source):
            abs_path = self._resolve_local_path(image_source)
            if not os.path.exists(abs_path):
                raise SocialPlatformError(f"Local image file not found: {abs_path}")
            with open(abs_path, "rb") as f:
                result = self._graph_post(
                    f"{self.page_id}/photos",
                    data={"caption": caption, "access_token": self.token},
                    files={"source": f},
                )
        else:
            result = self._graph_post(
                f"{self.page_id}/photos",
                data={
                    "url": image_source,
                    "caption": caption,
                    "access_token": self.token,
                },
            )
        post_id = result.get("post_id") or result.get("id")
        logger.info("Published single FB photo post: post_id=%s", post_id)
        return post_id

    # ── Video upload ──────────────────────────────────────────────────────

    def _upload_video(self, post):
        """
        Upload video to Facebook Page.
        Facebook video upload is synchronous for most files.
        Returns video post ID.
        """
        all_videos = post.all_videos
        description = post.content or ""

        if not all_videos:
            raise SocialPlatformError("Post has no video to upload.")

        video_url = all_videos[0]

        if self._is_local_path(video_url):
            abs_path = self._resolve_local_path(video_url)
            logger.info("Uploading FB video from local path: %s", abs_path)
            if not os.path.exists(abs_path):
                raise SocialPlatformError(f"Local video file not found: {abs_path}")
            with open(abs_path, "rb") as f:
                result = self._graph_post(
                    f"{self.page_id}/videos",
                    data={"description": description, "access_token": self.token},
                    files={"source": f},
                )
        else:
            logger.info("Uploading FB video from URL: %s", video_url)
            result = self._graph_post(
                f"{self.page_id}/videos",
                data={
                    "file_url": video_url,
                    "description": description,
                    "access_token": self.token,
                },
            )

        post_id = result.get("id")
        logger.info("FB video uploaded: post_id=%s", post_id)
        return post_id

    # ── Main entry point ─────────────────────────────────────────────────

    def create_post(self, post):
        """
        Create a Facebook Page post.
        Handles: text only, single image, multiple images, video.
        """
        all_images = post.all_images  # uses the property from Post model
        has_video = post.has_video

        # ── Video post ────────────────────────────────────────────────────
        if has_video:
            post_id = self._upload_video(post)
            return {"post_id": post_id}

        # ── Multiple images → photo album ─────────────────────────────────
        if len(all_images) > 1:
            logger.info("Creating FB multi-photo post with %d images", len(all_images))
            photo_ids = []
            for img in all_images:
                pid = self._upload_photo_unpublished(img)
                photo_ids.append(pid)

            attached_media = [{"media_fbid": pid} for pid in photo_ids]
            result = self._graph_post(
                f"{self.page_id}/feed",
                data={
                    "message": post.content,
                    "attached_media": str(attached_media).replace("'", '"'),
                    "access_token": self.token,
                },
            )
            post_id = result.get("id")
            logger.info("FB multi-photo post created: post_id=%s", post_id)
            return {"post_id": post_id}

        # ── Single image ──────────────────────────────────────────────────
        if len(all_images) == 1:
            logger.info("Creating FB single image post")
            post_id = self._upload_single_photo_published(all_images[0], post.content)
            return {"post_id": post_id}

        # ── Text only ─────────────────────────────────────────────────────
        logger.info("Creating FB text-only post")
        result = self._graph_post(
            f"{self.page_id}/feed",
            data={
                "message": post.content,
                "access_token": self.token,
            },
        )
        post_id = result.get("id")
        logger.info("FB text post created: post_id=%s", post_id)
        return {"post_id": post_id}

    def delete_post(self, post_id):
        """Delete a Facebook post by its ID."""
        logger.info("Deleting FB post: post_id=%s", post_id)
        result = self._graph_delete(post_id)
        logger.info("FB post deleted: result=%s", result)
        return result