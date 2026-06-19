import logging
import os
import requests
import tempfile
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .base import BaseSocialService, SocialPlatformError

logger = logging.getLogger(__name__)

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
YOUTUBE_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
YOUTUBE_TOKEN_URL = "https://oauth2.googleapis.com/token"


class YouTubeService(BaseSocialService):
    platform = "youtube"

    def __init__(self, user, account=None):
        super().__init__(user, account=account)
        self.token = self.account.access_token

    def refresh_access_token(self):
        """Refresh expired YouTube access token using refresh token."""
        if not self.account.refresh_token:
            raise SocialPlatformError("No refresh token available for YouTube.")

        response = requests.post(
            YOUTUBE_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.account.refresh_token,
                "client_id": settings.YOUTUBE_CLIENT_ID,
                "client_secret": settings.YOUTUBE_CLIENT_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=settings.SOCIAL_REQUEST_TIMEOUT,
        )

        if not response.ok:
            raise SocialPlatformError(f"YouTube token refresh failed: {response.text[:300]}")

        payload = response.json()
        self.account.access_token = payload["access_token"]
        expires_in = payload.get("expires_in")
        if expires_in:
            self.account.expires_at = timezone.now() + timedelta(seconds=int(expires_in))
        self.account.save()
        self.token = self.account.access_token
        logger.info("YouTube token refreshed successfully.")

    def _auth_headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def _is_local_path(self, url):
        site_url = getattr(settings, "SITE_URL", "")
        return (
            url.startswith(settings.MEDIA_URL) or
            (site_url and site_url in url) or
            (not url.startswith("http://") and not url.startswith("https://"))
        )

    def _resolve_local_path(self, url):
        site_url = getattr(settings, "SITE_URL", "").rstrip("/")
        if site_url and url.startswith(site_url):
            url = url[len(site_url):]
        media_url = settings.MEDIA_URL
        if url.startswith(media_url):
            relative = url[len(media_url):]
        else:
            relative = url.lstrip("/")
        return os.path.join(settings.MEDIA_ROOT, relative)

    # ── Video upload ──────────────────────────────────────────────────────

    def _upload_video_resumable(self, file_path, title, description, privacy="public"):
       
        file_size = os.path.getsize(file_path)
        ext = os.path.splitext(file_path)[1].lower()
        content_type_map = {
            ".mp4": "video/mp4",
            ".mov": "video/quicktime",
            ".avi": "video/x-msvideo",
            ".mkv": "video/x-matroska",
            ".webm": "video/webm",
            ".m4v": "video/x-m4v",
        }
        content_type = content_type_map.get(ext, "video/mp4")

        # Step 1 — Initiate resumable upload
        metadata = {
            "snippet": {
                "title": title[:100] if title else "Untitled",
                "description": description or "",
                "categoryId": "22",  # People & Blogs
            },
            "status": {
                "privacyStatus": privacy,
                "selfDeclaredMadeForKids": False,
            },
        }

        init_response = requests.post(
            f"{YOUTUBE_UPLOAD_URL}?uploadType=resumable&part=snippet,status",
            json=metadata,
            headers={
                **self._auth_headers(),
                "Content-Type": "application/json",
                "X-Upload-Content-Type": content_type,
                "X-Upload-Content-Length": str(file_size),
            },
            timeout=settings.SOCIAL_REQUEST_TIMEOUT,
        )

        if not init_response.ok:
            raise SocialPlatformError(
                f"YouTube upload initiation failed: {init_response.text[:300]}"
            )

        upload_url = init_response.headers.get("Location")
        if not upload_url:
            raise SocialPlatformError("YouTube did not return an upload URL.")

        logger.info("YouTube resumable upload initiated: size=%d bytes", file_size)

        # Step 2 — Upload file in chunks
        chunk_size = 8 * 1024 * 1024  # 8MB chunks
        uploaded = 0

        with open(file_path, "rb") as f:
            while uploaded < file_size:
                chunk = f.read(chunk_size)
                chunk_length = len(chunk)
                end_byte = uploaded + chunk_length - 1

                upload_response = requests.put(
                    upload_url,
                    data=chunk,
                    headers={
                        "Content-Length": str(chunk_length),
                        "Content-Range": f"bytes {uploaded}-{end_byte}/{file_size}",
                        "Content-Type": content_type,
                    },
                    timeout=120,
                )

                if upload_response.status_code in (200, 201):
                    # Upload complete
                    video_data = upload_response.json()
                    video_id = video_data.get("id")
                    logger.info("YouTube video upload complete: video_id=%s", video_id)
                    return video_id
                elif upload_response.status_code == 308:
                    # Resume incomplete — continue
                    range_header = upload_response.headers.get("Range", "")
                    if range_header:
                        uploaded = int(range_header.split("-")[1]) + 1
                    else:
                        uploaded += chunk_length
                    logger.info(
                        "YouTube upload progress: %d/%d bytes (%.1f%%)",
                        uploaded, file_size, (uploaded / file_size) * 100
                    )
                else:
                    raise SocialPlatformError(
                        f"YouTube chunk upload failed: {upload_response.status_code} {upload_response.text[:300]}"
                    )

        raise SocialPlatformError("YouTube upload finished without receiving video ID.")

    def _download_video_to_temp(self, video_url):
        logger.info("Downloading video from URL for YouTube: %s", video_url)
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
                tmp_path = tmp.name

            with requests.get(video_url, stream=True, timeout=120) as resp:
                resp.raise_for_status()
                with open(tmp_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=4 * 1024 * 1024):
                        f.write(chunk)

            return tmp_path
        except Exception as exc:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise SocialPlatformError(f"Failed to download video: {exc}") from exc

    # ── Delete video ──────────────────────────────────────────────────────

    def delete_post(self, video_id):
        """Delete a YouTube video by its ID."""
        logger.info("Deleting YouTube video: id=%s", video_id)
        response = requests.delete(
            f"{YOUTUBE_API_BASE}/videos",
            params={"id": video_id},
            headers=self._auth_headers(),
            timeout=settings.SOCIAL_REQUEST_TIMEOUT,
        )
        if response.status_code in (204, 200):
            logger.info("YouTube video deleted: id=%s", video_id)
            return True
        if response.status_code == 404:
            logger.info("YouTube video already deleted/not found: id=%s", video_id)
            return True
        if not response.ok:
            try:
                error_msg = response.json().get("error", {}).get("message", "")
                if "not found" in error_msg.lower() or "notfound" in error_msg.lower():
                    logger.info("YouTube video already deleted/not found: id=%s", video_id)
                    return True
            except Exception:
                error_msg = response.text[:300]
            raise SocialPlatformError(
                f"YouTube delete failed: {error_msg}"
            )
        return True

    # ── Main entry point ─────────────────────────────────────────────────

    def create_post(self, post):
        
        if not post.has_video:
            raise SocialPlatformError(
                "YouTube only supports video posts. "
                "Please attach a video file or video URL."
            )

        # Get privacy setting from platform options
        privacy = post.get_platform_option("youtube", "privacy", "public")
        if privacy not in ("public", "private", "unlisted"):
            privacy = "public"

        title = post.content[:100] if post.content else "Untitled Video"
        description = post.content or ""

        tmp_path = None
        try:
            if post.video_file:
                # Uploaded file — use directly
                abs_path = post.video_file.path
                logger.info("Uploading YouTube video from file: %s", abs_path)
                video_id = self._upload_video_resumable(abs_path, title, description, privacy)

            elif post.video:
                video_url = post.video.strip()
                if self._is_local_path(video_url):
                    # Local media file
                    abs_path = self._resolve_local_path(video_url)
                    logger.info("Uploading YouTube video from local path: %s", abs_path)
                    video_id = self._upload_video_resumable(abs_path, title, description, privacy)
                else:
                    # Remote URL — download first
                    tmp_path = self._download_video_to_temp(video_url)
                    video_id = self._upload_video_resumable(tmp_path, title, description, privacy)
            else:
                raise SocialPlatformError("Post has no video to upload to YouTube.")

            video_url = f"https://www.youtube.com/watch?v={video_id}"
            logger.info("YouTube video published: id=%s url=%s", video_id, video_url)

            return {
                "post_id": video_id,
                "post_urn": video_id,
                "url": video_url,
                "privacy": privacy,
            }

        finally:
            # Clean up temp file if created
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)