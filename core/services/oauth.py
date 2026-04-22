import base64
import hashlib
import secrets
import requests
from datetime import timedelta
from urllib.parse import urlencode
from django.conf import settings
from django.utils import timezone


class OAuthConfigurationError(Exception):
    pass


class SocialPlatformError(Exception):
    pass


LINKEDIN_DEFAULT_SCOPES = [
    "openid",
    "profile",
    "email",
    "w_member_social",
]

META_DEFAULT_SCOPES = [
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_posts",
    "business_management",
    "instagram_basic",
    "instagram_content_publish",
    "instagram_manage_contents",
    "publish_video",
]

TWITTER_DEFAULT_SCOPES = [
    "tweet.read",
    "tweet.write",
    "users.read",
    "offline.access",  # required for refresh tokens
]

LINKEDIN_AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
LINKEDIN_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
LINKEDIN_USERINFO_URL = "https://api.linkedin.com/v2/userinfo"

TWITTER_AUTH_URL = "https://twitter.com/i/oauth2/authorize"
TWITTER_TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
TWITTER_USERINFO_URL = "https://api.twitter.com/2/users/me"

TWITTER_OAUTH1_REQUEST_TOKEN_URL = "https://api.twitter.com/oauth/request_token"
TWITTER_OAUTH1_AUTH_URL = "https://api.twitter.com/oauth/authorize"
TWITTER_OAUTH1_ACCESS_TOKEN_URL = "https://api.twitter.com/oauth/access_token"
 
YOUTUBE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
YOUTUBE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
 
YOUTUBE_DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "openid",
    "email",
    "profile",
]


def generate_state():
    return secrets.token_urlsafe(32)


def generate_code_verifier():
    return secrets.token_urlsafe(96)[:128]


def generate_code_challenge(code_verifier):
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")


def oauth_expiry():
    return timezone.now() + timedelta(seconds=settings.SOCIAL_OAUTH_STATE_TTL_SECONDS)


# ── LinkedIn ──────────────────────────────────────────────────────────────

def build_linkedin_auth_url(redirect_uri, state):
    redirect_uri = redirect_uri or settings.LINKEDIN_REDIRECT_URI
    if not settings.LINKEDIN_CLIENT_ID:
        raise OAuthConfigurationError("LINKEDIN_CLIENT_ID is not configured")
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.LINKEDIN_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": " ".join(LINKEDIN_DEFAULT_SCOPES),
        }
    )
    return f"{LINKEDIN_AUTH_URL}?{query}"


def exchange_linkedin_code(code, redirect_uri=None):
    redirect_uri = redirect_uri or settings.LINKEDIN_REDIRECT_URI
    response = requests.post(
        LINKEDIN_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": settings.LINKEDIN_CLIENT_ID,
            "client_secret": settings.LINKEDIN_CLIENT_SECRET,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        timeout=settings.SOCIAL_REQUEST_TIMEOUT,
    )
    payload = _json_or_raise(response)
    _raise_for_error(response)
    if "access_token" not in payload:
        raise SocialPlatformError("LinkedIn token response did not include an access token.")
    return payload


def fetch_linkedin_profile(access_token):
    response = requests.get(
        LINKEDIN_USERINFO_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        timeout=settings.SOCIAL_REQUEST_TIMEOUT,
    )
    payload = _json_or_raise(response)
    _raise_for_error(response)
    if "sub" not in payload:
        raise SocialPlatformError("LinkedIn profile response did not include the user id.")
    return payload


def fetch_linkedin_pages(access_token):
    """Fetch LinkedIn Pages the user administers."""
    response = requests.get(
        "https://api.linkedin.com/v2/organizationAcls",
        params={
            "q": "roleAssignee",
            "role": "ADMINISTRATOR",
            "projection": "(elements*(organization~(id,name,localizedName)))",
        },
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "X-Restli-Protocol-Version": "2.0.0",
        },
        timeout=settings.SOCIAL_REQUEST_TIMEOUT,
    )
    payload = _json_or_raise(response)
    _raise_for_error(response)
    return payload.get("elements", [])


# ── Meta (Facebook + Instagram) ──────────────────────────────────────────

def build_meta_auth_url(redirect_uri, state):
    if not settings.META_APP_ID:
        raise OAuthConfigurationError("META_APP_ID is not configured")

    query = urlencode(
        {
            "client_id": settings.META_APP_ID,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": ",".join(META_DEFAULT_SCOPES),
            "response_type": "code",
        }
    )
    return f"https://www.facebook.com/{settings.META_GRAPH_API_VERSION}/dialog/oauth?{query}"


def exchange_meta_code(code, redirect_uri):
    if not settings.META_APP_ID or not settings.META_APP_SECRET:
        raise OAuthConfigurationError("Meta OAuth credentials are not fully configured")

    short_lived_response = requests.get(
        f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}/oauth/access_token",
        params={
            "client_id": settings.META_APP_ID,
            "redirect_uri": redirect_uri,
            "client_secret": settings.META_APP_SECRET,
            "code": code,
        },
        headers={"Accept": "application/json"},
        timeout=settings.SOCIAL_REQUEST_TIMEOUT,
    )
    short_lived_payload = _json_or_raise(short_lived_response)
    _raise_for_error(short_lived_response)

    short_lived_token = short_lived_payload.get("access_token")
    if not short_lived_token:
        raise SocialPlatformError("Meta short-lived token response did not include an access token.")

    long_lived_response = requests.get(
        f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": settings.META_APP_ID,
            "client_secret": settings.META_APP_SECRET,
            "fb_exchange_token": short_lived_token,
        },
        headers={"Accept": "application/json"},
        timeout=settings.SOCIAL_REQUEST_TIMEOUT,
    )
    long_lived_payload = _json_or_raise(long_lived_response)
    _raise_for_error(long_lived_response)

    if "access_token" not in long_lived_payload:
        raise SocialPlatformError("Meta long-lived token response did not include an access token.")
    return long_lived_payload


def fetch_meta_accounts(access_token):
    response = requests.get(
        f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}/me/accounts",
        params={
            "fields": "id,name,access_token,instagram_business_account{id,username},link",
            "access_token": access_token,
        },
        headers={"Accept": "application/json"},
        timeout=settings.SOCIAL_REQUEST_TIMEOUT,
    )
    payload = _json_or_raise(response)
    _raise_for_error(response)
    return payload.get("data", [])


# ── Twitter OAuth 2.0 PKCE ────────────────────────────────────────────────

def build_twitter_auth_url(redirect_uri, state, code_verifier):
    """Build Twitter OAuth 2.0 PKCE authorization URL."""
    if not getattr(settings, "TWITTER_CLIENT_ID", ""):
        raise OAuthConfigurationError("TWITTER_CLIENT_ID is not configured")

    code_challenge = generate_code_challenge(code_verifier)

    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.TWITTER_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "scope": " ".join(TWITTER_DEFAULT_SCOPES),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{TWITTER_AUTH_URL}?{query}"


def exchange_twitter_code(code, redirect_uri, code_verifier):
    """Exchange authorization code for access token using PKCE."""
    if not getattr(settings, "TWITTER_CLIENT_ID", "") or not getattr(settings, "TWITTER_CLIENT_SECRET", ""):
        raise OAuthConfigurationError("TWITTER_CLIENT_ID and TWITTER_CLIENT_SECRET are not configured")

    response = requests.post(
        TWITTER_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "client_id": settings.TWITTER_CLIENT_ID,
        },
        auth=(settings.TWITTER_CLIENT_ID, settings.TWITTER_CLIENT_SECRET),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=settings.SOCIAL_REQUEST_TIMEOUT,
    )
    payload = _json_or_raise(response)
    _raise_for_error(response)
    if "access_token" not in payload:
        raise SocialPlatformError("Twitter token response did not include an access token.")
    return payload


def fetch_twitter_profile(access_token):
    """Fetch Twitter user profile."""
    response = requests.get(
        TWITTER_USERINFO_URL,
        params={"user.fields": "id,name,username,profile_image_url"},
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        timeout=settings.SOCIAL_REQUEST_TIMEOUT,
    )
    payload = _json_or_raise(response)
    _raise_for_error(response)
    data = payload.get("data", {})
    if "id" not in data:
        raise SocialPlatformError("Twitter profile response did not include user id.")
    return data


def refresh_twitter_token(refresh_token):
    """Refresh an expired Twitter access token."""
    response = requests.post(
        TWITTER_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": settings.TWITTER_CLIENT_ID,
        },
        auth=(settings.TWITTER_CLIENT_ID, settings.TWITTER_CLIENT_SECRET),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=settings.SOCIAL_REQUEST_TIMEOUT,
    )
    payload = _json_or_raise(response)
    _raise_for_error(response)
    if "access_token" not in payload:
        raise SocialPlatformError("Twitter refresh token response did not include an access token.")
    return payload


# ── Twitter OAuth 1.0a (User Context) ─────────────────────────────────────

def fetch_twitter_request_token(callback_uri):
    """Fetch an OAuth 1.0a request token from Twitter."""
    from requests_oauthlib import OAuth1
    if not settings.TWITTER_CONSUMER_KEY or not settings.TWITTER_CONSUMER_SECRET:
        raise OAuthConfigurationError("Twitter Consumer Key/Secret not configured.")

    auth = OAuth1(settings.TWITTER_CONSUMER_KEY, settings.TWITTER_CONSUMER_SECRET, callback_uri=callback_uri)
    response = requests.post(TWITTER_OAUTH1_REQUEST_TOKEN_URL, auth=auth, timeout=settings.SOCIAL_REQUEST_TIMEOUT)
    if not response.ok:
        raise SocialPlatformError(f"Twitter request token failed: {response.text[:300]}")
    
    from urllib.parse import parse_qs
    tokens = parse_qs(response.text)
    return {
        "oauth_token": tokens.get("oauth_token", [None])[0],
        "oauth_token_secret": tokens.get("oauth_token_secret", [None])[0],
    }

def build_twitter_oauth1_auth_url(oauth_token):
    """Build the authorization URL for Twitter OAuth 1.0a."""
    return f"{TWITTER_OAUTH1_AUTH_URL}?oauth_token={oauth_token}"

def exchange_twitter_oauth1_code(oauth_token, oauth_token_secret, oauth_verifier):
    """Exchange the verifier for an access token and secret."""
    from requests_oauthlib import OAuth1
    auth = OAuth1(
        settings.TWITTER_CONSUMER_KEY,
        settings.TWITTER_CONSUMER_SECRET,
        resource_owner_key=oauth_token,
        resource_owner_secret=oauth_token_secret,
        verifier=oauth_verifier
    )
    response = requests.post(TWITTER_OAUTH1_ACCESS_TOKEN_URL, auth=auth, timeout=settings.SOCIAL_REQUEST_TIMEOUT)
    if not response.ok:
        raise SocialPlatformError(f"Twitter access token exchange failed: {response.text[:300]}")
    
    from urllib.parse import parse_qs
    tokens = parse_qs(response.text)
    return {
        "access_token": tokens.get("oauth_token", [None])[0],
        "access_token_secret": tokens.get("oauth_token_secret", [None])[0],
        "user_id": tokens.get("user_id", [None])[0],
        "screen_name": tokens.get("screen_name", [None])[0],
    }


# ── YouTube OAuth 2.0 ────────────────────────────────────────────────────

def build_youtube_auth_url(redirect_uri, state):
    """Build YouTube OAuth 2.0 authorization URL."""
    if not getattr(settings, "YOUTUBE_CLIENT_ID", ""):
        raise OAuthConfigurationError("YOUTUBE_CLIENT_ID is not configured")

    query = urlencode(
        {
            "client_id": settings.YOUTUBE_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(YOUTUBE_DEFAULT_SCOPES),
            "state": state,
            "access_type": "offline",   # needed to get refresh token
            "prompt": "consent",         # force consent screen to always get refresh token
        }
    )
    return f"{YOUTUBE_AUTH_URL}?{query}"


def exchange_youtube_code(code, redirect_uri):
    """Exchange authorization code for YouTube access + refresh tokens."""
    if not getattr(settings, "YOUTUBE_CLIENT_ID", "") or not getattr(settings, "YOUTUBE_CLIENT_SECRET", ""):
        raise OAuthConfigurationError("YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET are not configured")

    response = requests.post(
        YOUTUBE_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": settings.YOUTUBE_CLIENT_ID,
            "client_secret": settings.YOUTUBE_CLIENT_SECRET,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=settings.SOCIAL_REQUEST_TIMEOUT,
    )
    payload = _json_or_raise(response)
    _raise_for_error(response)
    if "access_token" not in payload:
        raise SocialPlatformError("YouTube token response did not include an access token.")
    return payload


def fetch_youtube_profile(access_token):
    """Fetch YouTube/Google user profile."""
    response = requests.get(
        YOUTUBE_USERINFO_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        timeout=settings.SOCIAL_REQUEST_TIMEOUT,
    )
    payload = _json_or_raise(response)
    _raise_for_error(response)
    if "sub" not in payload:
        raise SocialPlatformError("YouTube profile response did not include user id.")
    return payload


# ── Social account data builder ───────────────────────────────────────────

def build_social_account_data(platform, token_payload, profile_payload):
    expires_at = None
    expires_in = token_payload.get("expires_in")
    if expires_in:
        expires_at = timezone.now() + timedelta(seconds=int(expires_in))

    if platform == "linkedin":
        account_id = profile_payload.get("sub")
        if not account_id:
            raise SocialPlatformError("LinkedIn profile is missing the account id.")
        return {
            "account_id": account_id,
            "platform_username": profile_payload.get("name", ""),
            "access_token": token_payload["access_token"],
            "refresh_token": token_payload.get("refresh_token", ""),
            "token_type": token_payload.get("token_type", "Bearer"),
            "expires_at": expires_at,
            "metadata": {
                "email": profile_payload.get("email"),
                "given_name": profile_payload.get("given_name"),
                "family_name": profile_payload.get("family_name"),
            },
        }

    if platform == "facebook":
        account_id = profile_payload.get("id")
        page_access_token = profile_payload.get("access_token")
        if not account_id or not page_access_token:
            raise SocialPlatformError("Facebook page data is incomplete.")
        return {
            "account_id": account_id,
            "platform_username": profile_payload.get("name", ""),
            "access_token": page_access_token,
            "refresh_token": "",
            "token_type": token_payload.get("token_type", "Bearer"),
            "expires_at": expires_at,
            "metadata": {
                "page_name": profile_payload.get("name"),
                "page_link": profile_payload.get("link"),
            },
        }

    if platform == "instagram":
        instagram_account = profile_payload.get("instagram_business_account") or {}
        account_id = instagram_account.get("id")
        if not account_id:
            raise SocialPlatformError("Instagram business account data is incomplete.")
        return {
            "account_id": account_id,
            "platform_username": instagram_account.get("username", ""),
            "access_token": token_payload["access_token"],
            "refresh_token": "",
            "token_type": token_payload.get("token_type", "Bearer"),
            "expires_at": expires_at,
            "metadata": {
                "page_id": profile_payload.get("id"),
                "page_name": profile_payload.get("name"),
                "page_link": profile_payload.get("link"),
            },
        }

    if platform == "twitter":
        account_id = profile_payload.get("id")
        if not account_id:
            raise SocialPlatformError("Twitter profile is missing the user id.")
        return {
            "account_id": account_id,
            "platform_username": profile_payload.get("username", ""),
            "access_token": token_payload["access_token"],
            "refresh_token": token_payload.get("refresh_token", ""),
            "token_type": token_payload.get("token_type", "Bearer"),
            "expires_at": expires_at,
            "metadata": {
                "name": profile_payload.get("name"),
                "username": profile_payload.get("username"),
                "profile_image_url": profile_payload.get("profile_image_url"),
                "oauth1_access_token": token_payload.get("oauth1_access_token", ""),
                "oauth1_access_token_secret": token_payload.get("oauth1_access_token_secret", ""),
            },
        }

    if platform == "youtube":
        account_id = profile_payload.get("sub")
        if not account_id:
            raise SocialPlatformError("YouTube profile is missing the user id.")
        return {
            "account_id": account_id,
            "platform_username": profile_payload.get("name", ""),
            "access_token": token_payload["access_token"],
            "refresh_token": token_payload.get("refresh_token", ""),
            "token_type": token_payload.get("token_type", "Bearer"),
            "expires_at": expires_at,
            "metadata": {
                "email": profile_payload.get("email"),
                "picture": profile_payload.get("picture"),
            },
        }

    raise SocialPlatformError(f"Unsupported OAuth platform: {platform}")


def _json_or_raise(response):
    try:
        return response.json()
    except ValueError as exc:
        raise SocialPlatformError("Provider returned an invalid response.") from exc


def _raise_for_error(response):
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        message = response.text[:500] if getattr(response, "text", "") else str(exc)
        raise SocialPlatformError(message) from exc