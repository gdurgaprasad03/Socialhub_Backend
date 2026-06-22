import logging
import os
from datetime import timedelta
import requests
from django.conf import settings
from django.utils import timezone
from .base import BaseSocialService, SocialPlatformError

logger = logging.getLogger(__name__)

VIDEO_CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".m4v": "video/x-m4v",
}

CHUNK_SIZE = 4 * 1024 * 1024  # 4MB per LinkedIn recommendation


class LinkedInService(BaseSocialService):
    platform = "linkedin"

    def __init__(self, user, account=None):
        super().__init__(user, account=account)
        self.base_url = "https://api.linkedin.com/v2"

    def refresh_access_token(self):
        from .oauth import refresh_linkedin_token
        if not self.account.refresh_token:
            raise SocialPlatformError("No refresh token available for LinkedIn")

        logger.info("Refreshing LinkedIn access token...")
        payload = refresh_linkedin_token(self.account.refresh_token)

        self.account.access_token = payload["access_token"]
        if "refresh_token" in payload:
            self.account.refresh_token = payload["refresh_token"]

        expires_in = payload.get("expires_in")
        if expires_in:
            self.account.expires_at = timezone.now() + timedelta(seconds=int(expires_in))

        self.account.save()
        logger.info("LinkedIn token refreshed successfully.")

    def _auth_headers(self, use_rest_v3=True):
        headers = {
            "Authorization": f"Bearer {self.account.access_token}",
            "Content-Type": "application/json",
        }
        if use_rest_v3:
            headers["LinkedIn-Version"] = "202602"
            headers["X-Restli-Protocol-Version"] = "2.0.0"
        return headers

    # ── Helpers ───────────────────────────────────────────────────────────

    def _resolve_local_path(self, media_url_path):
        media_url = settings.MEDIA_URL
        site_url = getattr(settings, "SITE_URL", "")
        
        # If it's an absolute URL pointing to our SITE_URL, strip the domain
        if site_url and media_url_path.startswith(site_url):
            media_url_path = media_url_path[len(site_url):]

        if media_url_path.startswith(media_url):
            relative = media_url_path[len(media_url):]
        else:
            relative = media_url_path.lstrip("/")
        return os.path.join(settings.MEDIA_ROOT, relative)

    def _is_local_path(self, url):
        if url.startswith(settings.MEDIA_URL):
            return True
        site_url = getattr(settings, "SITE_URL", "")
        if site_url and url.startswith(site_url + settings.MEDIA_URL):
            return True
        return not url.startswith("http://") and not url.startswith("https://")

    # ── Image upload ──────────────────────────────────────────────────────

    def _author_urn(self):
      
        if getattr(self.account, "account_type", "personal") == "page":
            return f"urn:li:organization:{self.account.account_id}"
        return f"urn:li:person:{self.account.account_id}"


    def _register_image_upload(self):
        url = "https://api.linkedin.com/rest/images?action=initializeUpload"
        payload = {
            "initializeUploadRequest": {
                "owner": self._author_urn(),
            }
        }
        result = self.post_json(url, headers=self._auth_headers(), payload=payload)
        data = result["body"]
        upload_url = data["value"]["uploadUrl"]
        asset_urn = data["value"]["image"]
        logger.info("Registered image upload — asset_urn=%s", asset_urn)
        return upload_url, asset_urn

    def _upload_binary(self, upload_url, content, content_type="image/jpeg"):
        headers = self._auth_headers(use_rest_v3=False)
        headers["Content-Type"] = content_type
        self.put_request(upload_url, data=content, headers=headers)
        logger.info("Binary upload done")

    def _upload_image_from_url(self, image_url):
        upload_url, asset_urn = self._register_image_upload()
        resp = requests.get(image_url, timeout=30)
        resp.raise_for_status()
        self._upload_binary(upload_url, resp.content, resp.headers.get("Content-Type", "image/jpeg"))
        logger.info("Image URL upload done → urn=%s", asset_urn)
        return asset_urn

    def _upload_image_from_local_path(self, media_url_path):
        abs_path = self._resolve_local_path(media_url_path)
        if not os.path.exists(abs_path):
            raise SocialPlatformError(f"Local media file not found: {abs_path}")
        ext = os.path.splitext(abs_path)[1].lower()
        content_type = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                        ".png": "image/png", ".gif": "image/gif",
                        ".webp": "image/webp"}.get(ext, "image/jpeg")
        upload_url, asset_urn = self._register_image_upload()
        with open(abs_path, "rb") as f:
            self._upload_binary(upload_url, f.read(), content_type)
        logger.info("Local image upload done → urn=%s", asset_urn)
        return asset_urn

    def _upload_image_from_file(self, file_obj):
        upload_url, asset_urn = self._register_image_upload()
        file_obj.seek(0)
        self._upload_binary(upload_url, file_obj.read(), "image/jpeg")
        logger.info("Image file object upload done → urn=%s", asset_urn)
        return asset_urn

    def _upload_all_images(self, post):
        """Unified upload of all images from the Post object."""
        asset_urns = []
        seen = set()
        all_images = post.all_images
        
        logger.info("Found %d images to upload for LinkedIn", len(all_images))

        for url in all_images:
            if not url or url in seen:
                continue
            seen.add(url)
            
            # If it's a local path in the absolute URL, we can still try to resolve it
            # Otherwise just upload from URL
            if self._is_local_path(url):
                urn = self._upload_image_from_local_path(url)
            else:
                urn = self._upload_image_from_url(url)
            asset_urns.append(urn)

        logger.info("Final LinkedIn image asset_urns=%s", asset_urns)
        return asset_urns

    # ── Video upload ──────────────────────────────────────────────────────

    def _register_video_upload(self, file_size_bytes):
        url = "https://api.linkedin.com/rest/videos?action=initializeUpload"
        payload = {
            "initializeUploadRequest": {
                "owner": self._author_urn(),
                "fileSizeBytes": file_size_bytes,
                "uploadCaptions": False,
                "uploadThumbnail": False,
            }
        }
        result = self.post_json(url, headers=self._auth_headers(), payload=payload)
        data = result["body"]
        value = data["value"]
        video_urn = value["video"]
        upload_instructions = value["uploadInstructions"]
        logger.info("Registered video upload — video_urn=%s chunks=%d", video_urn, len(upload_instructions))
        return upload_instructions, video_urn

    def _upload_video_chunks(self, upload_instructions, file_obj, content_type="video/mp4"):
        etags = []
        total = len(upload_instructions)
        for i, instruction in enumerate(upload_instructions):
            upload_url = instruction["uploadUrl"]
            first_byte = instruction["firstByte"]
            last_byte = instruction["lastByte"]
            chunk_size = last_byte - first_byte + 1
            file_obj.seek(first_byte)
            chunk_data = file_obj.read(chunk_size)
            logger.info("Uploading chunk %d/%d bytes=%d-%d size=%d",
                        i + 1, total, first_byte, last_byte, len(chunk_data))
            headers = self._auth_headers(use_rest_v3=True)
            headers["Content-Type"] = "application/octet-stream"
            resp = self.put_request(
                upload_url, data=chunk_data,
                headers=headers
            )
            etag = resp.headers.get("ETag") or resp.headers.get("etag", "")
            etags.append(etag)
            logger.info("Chunk %d/%d done — ETag=%s", i + 1, total, etag[:30] if etag else "")
        return etags

    def _finalize_video_upload(self, video_urn, etags):
        url = "https://api.linkedin.com/rest/videos?action=finalizeUpload"
        payload = {
            "finalizeUploadRequest": {
                "video": video_urn,
                "uploadToken": "",
                "uploadedPartIds": etags,
            }
        }
        self.post_json(url, headers=self._auth_headers(), payload=payload)
        logger.info("Video upload finalized — video_urn=%s", video_urn)

    def upload_video(self, post):
        """Upload video in chunks. Returns video URN. Does NOT create the post."""
        if post.video_file:
            abs_path = post.video_file.path
            ext = os.path.splitext(abs_path)[1].lower()
            content_type = VIDEO_CONTENT_TYPES.get(ext, "video/mp4")
            file_size = os.path.getsize(abs_path)
            logger.info("Uploading video file: path=%s size=%d bytes", abs_path, file_size)
            upload_instructions, video_urn = self._register_video_upload(file_size)
            with open(abs_path, "rb") as f:
                etags = self._upload_video_chunks(upload_instructions, f, content_type)
            self._finalize_video_upload(video_urn, etags)

        elif post.video:
            video_url = post.video
            if self._is_local_path(video_url):
                abs_path = self._resolve_local_path(video_url)
                ext = os.path.splitext(abs_path)[1].lower()
                content_type = VIDEO_CONTENT_TYPES.get(ext, "video/mp4")
                file_size = os.path.getsize(abs_path)
                upload_instructions, video_urn = self._register_video_upload(file_size)
                with open(abs_path, "rb") as f:
                    etags = self._upload_video_chunks(upload_instructions, f, content_type)
                self._finalize_video_upload(video_urn, etags)
            else:
                import tempfile
                logger.info("Downloading external video: %s", video_url)
                tmp_path = None
                try:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
                        tmp_path = tmp.name
                    with requests.get(video_url, stream=True, timeout=120) as resp:
                        resp.raise_for_status()
                        content_type = resp.headers.get("Content-Type", "video/mp4")
                        with open(tmp_path, "wb") as f:
                            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                                f.write(chunk)
                    file_size = os.path.getsize(tmp_path)
                    upload_instructions, video_urn = self._register_video_upload(file_size)
                    with open(tmp_path, "rb") as f:
                        etags = self._upload_video_chunks(upload_instructions, f, content_type)
                    self._finalize_video_upload(video_urn, etags)
                finally:
                    if tmp_path and os.path.exists(tmp_path):
                        os.unlink(tmp_path)
        else:
            raise SocialPlatformError("Post has no video to upload.")

        logger.info("Video upload complete — video_urn=%s", video_urn)
        return video_urn

    def get_video_asset_status(self, video_urn):
        """Poll video status. Returns 'AVAILABLE', 'PROCESSING', or 'FAILED'."""
        encoded = requests.utils.quote(video_urn, safe="")
        url = f"https://api.linkedin.com/rest/videos/{encoded}"
        resp = self.get_request(url, headers=self._auth_headers())
        data = resp.json()
        status = data.get("status", "PROCESSING")
        logger.info("Video %s status=%s", video_urn, status)
        return status

    def publish_video_post(self, post, video_urn):
        """
        Publish a video post using LinkedIn's /rest/posts API.
        """
        url = "https://api.linkedin.com/rest/posts"
        author = self._author_urn()
        payload = {
            "author": author,
            "commentary": post.content,
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
            },
            "content": {
                "media": {
                    "title": post.content[:200] if post.content else "Video",
                    "id": video_urn,
                }
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }

        logger.info("Publishing video post via /rest/posts — video_urn=%s author=%s", video_urn, author)

        resp = self.post_json(url, payload=payload, headers=self._auth_headers())
        post_urn = resp["headers"].get("x-restli-id") or resp["headers"].get("X-RestLi-Id")
        if not post_urn:
            post_urn = resp["body"].get("id")
        if not post_urn:
            raise SocialPlatformError("LinkedIn did not return a post URN for video post.")
        logger.info("Video post published — post_urn=%s", post_urn)
        return {"post_urn": post_urn, "body": resp["body"]}

    # ── Image / text post ─────────────────────────────────────────────────

    def create_post(self, post):
        """Create image or text post via /rest/posts. Replacing deprecated /v2/ugcPosts."""
        url = "https://api.linkedin.com/rest/posts"
        author = self._author_urn()
        asset_urns = self._upload_all_images(post)

        payload = {
            "author": author,
            "commentary": post.content,
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
            },
            "lifecycleState": "PUBLISHED",
            "isReshareDisabledByAuthor": False,
        }

        if asset_urns:
            if len(asset_urns) == 1:
                payload["content"] = {
                    "media": {
                        "id": asset_urns[0]
                    }
                }
            else:
                payload["content"] = {
                    "multiImage": {
                        "images": [{"id": urn} for urn in asset_urns]
                    }
                }
        elif post.has_video:
    
             raise SocialPlatformError("Video posts should use publish_video_post")

        logger.info("Creating LinkedIn post — author=%s images=%d", author, len(asset_urns))
        result = self.post_json(url, payload=payload, headers=self._auth_headers())
        resp_headers = result["headers"]
        post_urn = resp_headers.get("x-restli-id") or resp_headers.get("X-RestLi-Id")
        if not post_urn:
            post_urn = result["body"].get("id")

        if not post_urn:
            raise SocialPlatformError("LinkedIn did not return a post URN.")

        logger.info("Post created — post_urn=%s author=%s", post_urn, author)
        return {"post_urn": post_urn, "body": result["body"]}

    def delete_post(self, post_urn):
        encoded_urn = requests.utils.quote(post_urn, safe="")

    
        if post_urn.startswith("urn:li:ugcPost:"):
            url = f"https://api.linkedin.com/v2/ugcPosts/{encoded_urn}"
            headers = self._auth_headers(use_rest_v3=False)
        else:
            # Covers urn:li:share: and all REST v3 post URNs
            url = f"https://api.linkedin.com/rest/posts/{encoded_urn}"
            headers = self._auth_headers(use_rest_v3=True)

        logger.info("Deleting LinkedIn post: urn=%s url=%s", post_urn, url)
        return self.delete_request(url, headers=headers)