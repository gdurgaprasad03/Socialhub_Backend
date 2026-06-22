import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

from ..models import SocialAccount


class SocialPlatformError(Exception):
    pass


class BaseSocialService:
    platform = None

    def __init__(self, user, account=None):
        if self.platform is None:
            raise ValueError("Service platform must be defined")
        
        if account:
            self.account = account
        else:
            # Fallback for safety, picking the first one found if not specified
            self.account = SocialAccount.objects.filter(user=user, platform=self.platform).first()
            
        if not self.account:
            raise SocialAccount.DoesNotExist(f"No {self.platform} account found for user {user.id}")

        self.timeout = settings.SOCIAL_REQUEST_TIMEOUT
        self.ensure_active_token()

    def ensure_active_token(self):
        if self.account.is_expired:
            try:
                self.refresh_access_token()
            except Exception as exc:
                raise SocialPlatformError(f"{self.platform} token refresh failed: {exc}") from exc

    def refresh_access_token(self):
        """Override this in subclasses to handle token refresh."""
        raise SocialPlatformError(f"{self.platform} access token has expired and refresh is not implemented")

    def _request(self, method, url, **kwargs):
        """Wrapper to handle 401 Unauthorized by refreshing token once."""
        headers = kwargs.get("headers", {})
        # Ensure we have the latest token in headers if they were pre-built
        if "Authorization" in headers and self.account.access_token not in headers["Authorization"]:
             # Update the header with the current (potentially refreshed) token
             headers["Authorization"] = f"Bearer {self.account.access_token}"

        logger.info("%s requesting: %s %s", self.platform, method, url)
        response = requests.request(method, url, timeout=self.timeout, **kwargs)
        logger.info("%s response: %s %d", self.platform, method, response.status_code)

        if response.status_code == 401:
            logger.info("%s returned 401, attempting token refresh...", self.platform)
            try:
                self.refresh_access_token()
                # Update headers with new token
                if "headers" in kwargs:
                    kwargs["headers"]["Authorization"] = f"Bearer {self.account.access_token}"
                # Retry once
                response = requests.request(method, url, timeout=self.timeout, **kwargs)
            except Exception as refresh_exc:
                logger.error("%s token refresh failed during 401 retry: %s", self.platform, refresh_exc)
                # Fall through to raise the original 401 or the refresh error

        self._raise_for_error(response)
        return response

    def post_json(self, url, *, headers=None, payload=None):
        response = self._request("POST", url, json=payload, headers=headers)
        body = response.json() if response.content else {}
        return {"body": body, "headers": dict(response.headers)}

    def post_form(self, url, *, data=None):
        response = self._request("POST", url, data=data)
        if response.content:
            return response.json()
        return {}

    def delete_request(self, url, *, headers=None):
        try:
            self._request("DELETE", url, headers=headers)
        except SocialPlatformError as exc:
            exc_str = str(exc).lower()
            if "not_found" in exc_str or "not found" in exc_str or "404" in exc_str:
                logger.info("Post already deleted or not found on platform: %s", url)
                return True
            # Log clearly before re-raising so the cause is visible in logs
            logger.error(
                "Platform delete failed — platform=%s url=%s error=%s",
                self.platform, url, exc,
            )
            raise
        return True

    def get_request(self, url, *, headers=None):
        return self._request("GET", url, headers=headers)

    def put_request(self, url, *, data=None, headers=None):
        return self._request("PUT", url, data=data, headers=headers)

    def _raise_for_error(self, response):
        try:
            response.raise_for_status()
        except requests.RequestException as exc:
            message = response.text[:500] if getattr(response, "text", "") else str(exc)
            raise SocialPlatformError(message) from exc