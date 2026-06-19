import logging
import os
import requests
from django.conf import settings
from .base import BaseSocialService, SocialPlatformError
from ..cloudinary_utils import upload_image_to_cloudinary

logger = logging.getLogger(__name__)

TWITTER_API_BASE = "https://api.twitter.com/2"
TWITTER_UPLOAD_URL = "https://upload.twitter.com/1.1/media/upload.json"


class TwitterService(BaseSocialService):
    platform = "twitter"

    def __init__(self, user, account=None):
        super().__init__(user, account=account)

    def _auth_headers(self):
        return {
            "Authorization": f"Bearer {self.account.access_token}",
            "Content-Type": "application/json",
        }

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

    def _get_oauth1_auth(self):
        """Build OAuth 1.0a auth object from account metadata."""
        from requests_oauthlib import OAuth1

        consumer_key = getattr(settings, "TWITTER_CONSUMER_KEY", "")
        consumer_secret = getattr(settings, "TWITTER_CONSUMER_SECRET", "")
        # These come from the new OAuth 1.0a connection flow
        access_token = self.account.metadata.get("oauth1_access_token", "")
        access_token_secret = self.account.metadata.get("oauth1_access_token_secret", "")

        if all([consumer_key, consumer_secret, access_token, access_token_secret]):
            return OAuth1(consumer_key, consumer_secret, access_token, access_token_secret)
        return None

    # ── Media upload ──────────────────────────────────────────────────────

    def _upload_media(self, image_source):
        
        auth = self._get_oauth1_auth()
        if not auth:
            raise SocialPlatformError(
                "Twitter media upload requires OAuth 1.0a credentials. "
                "Please reconnect your Twitter account."
            )

        # Get image bytes
        if isinstance(image_source, str):
            if self._is_local_path(image_source):
                abs_path = self._resolve_local_path(image_source)
                with open(abs_path, "rb") as f:
                    image_data = f.read()
            else:
                resp = requests.get(image_source, timeout=30)
                resp.raise_for_status()
                image_data = resp.content
        else:
            image_source.seek(0)
            image_data = image_source.read()

        response = requests.post(
            TWITTER_UPLOAD_URL,
            files={"media": image_data},
            auth=auth,
            timeout=settings.SOCIAL_REQUEST_TIMEOUT,
        )

        if not response.ok:
            raise SocialPlatformError(
                f"Twitter media upload failed: {response.text[:300]}"
            )

        media_id = response.json().get("media_id_string")
        if not media_id:
            raise SocialPlatformError("Twitter media upload did not return a media ID.")

        logger.info("Twitter media uploaded: media_id=%s", media_id)
        return media_id

    # ── Tweet creation ────────────────────────────────────────────────────

    def _post_tweet(self, text, media_ids=None):
       
        auth = self._get_oauth1_auth()
        payload = {"text": text or ""}

        if media_ids:
            payload["media"] = {"media_ids": media_ids}

        # We use auth=auth (OAuth 1.0a) instead of Bearer token for better Free Tier support
        response = requests.post(
            f"{TWITTER_API_BASE}/tweets",
            json=payload,
            auth=auth,
            timeout=settings.SOCIAL_REQUEST_TIMEOUT,
        )

        if not response.ok:
            # Log headers to help debug quota/rate limits
            logger.debug("Twitter API Response Headers: %s", response.headers)
            
            try:
                error = response.json()
                # Handle v2 error format
                msg = error.get("detail") or error.get("title") or str(error)
                
                # Check for specific quota error
                if "does not have any credits" in msg.lower():
                    msg = (
                        "Twitter API quota exhausted (1,500 tweets/month limit). "
                        "Please check your usage at https://developer.twitter.com/en/portal/dashboard"
                    )
            except Exception:
                msg = response.text[:300]
            
            raise SocialPlatformError(f"Twitter post failed: {msg}")

        data = response.json().get("data", {})
        tweet_id = data.get("id")
        if not tweet_id:
            raise SocialPlatformError("Twitter did not return a tweet ID.")

        logger.info("Tweet posted: id=%s", tweet_id)
        return tweet_id

    def _delete_tweet(self, tweet_id):
        """Delete a tweet by ID using OAuth 1.0a."""
        auth = self._get_oauth1_auth()
        response = requests.delete(
            f"{TWITTER_API_BASE}/tweets/{tweet_id}",
            auth=auth,
            timeout=settings.SOCIAL_REQUEST_TIMEOUT,
        )
        if response.status_code == 404:
            logger.info("Twitter post already deleted or not found: id=%s", tweet_id)
            return True
        if not response.ok:
            try:
                error = response.json()
                msg = error.get("detail") or str(error)
                msg_lower = msg.lower()
                # If message contains not found, treat it as deleted successfully
                if "not found" in msg_lower or "cannot find" in msg_lower or "notfound" in msg_lower:
                    logger.info("Twitter post already deleted or not found: id=%s", tweet_id)
                    return True
            except Exception:
                msg = response.text[:300]
            raise SocialPlatformError(f"Twitter delete failed: {msg}")

        logger.info("Tweet deleted: id=%s", tweet_id)
        return True

    # ── Main entry point ─────────────────────────────────────────────────

    def create_post(self, post):
       
        content = post.content or ""

        # Enforce Twitter character limit
        if len(content) > 280:
            content = content[:277] + "..."
            logger.warning("Tweet content truncated to 280 chars")

        all_images = post.all_images
        media_ids = []

        if all_images:
            # Twitter allows max 4 images per tweet
            images_to_upload = all_images[:4]
            if len(all_images) > 4:
                logger.warning(
                    "Twitter only supports 4 images per tweet. "
                    "Uploading first 4 of %d images.", len(all_images)
                )

            for img_url in images_to_upload:
                try:
                    media_id = self._upload_media(img_url)
                    media_ids.append(media_id)
                except SocialPlatformError as exc:
                    logger.warning("Twitter image upload failed: %s", exc)
                    # Continue without this image rather than failing the whole post
                    continue

        tweet_id = self._post_tweet(content, media_ids if media_ids else None)
        tweet_url = f"https://twitter.com/i/web/status/{tweet_id}"
        logger.info("Twitter post created: tweet_id=%s url=%s", tweet_id, tweet_url)

        return {"post_id": tweet_id, "post_urn": tweet_id, "url": tweet_url}

    def delete_post(self, tweet_id):
        logger.info("Deleting tweet: id=%s", tweet_id)
        return self._delete_tweet(tweet_id)