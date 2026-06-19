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
    # r_organization_social and w_organization_social require LinkedIn Community
    # Management API approval. Add them back once your app is approved.
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

# Scopes for "Instagram API with Instagram Login" (direct login, no FB Page).
INSTAGRAM_LOGIN_SCOPES = [
    "instagram_business_basic",
    "instagram_business_content_publish",
]

INSTAGRAM_LOGIN_AUTH_URL = "https://www.instagram.com/oauth/authorize"
INSTAGRAM_LOGIN_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
INSTAGRAM_GRAPH_BASE = "https://graph.instagram.com"

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
    """Fetch LinkedIn Pages the user administers. Returns a clean list of {id, name}."""
    response = requests.get(
        "https://api.linkedin.com/v2/organizationAcls",
        params={
            "q": "roleAssignee",
            "role": "ADMINISTRATOR",
            "projection": "(elements*(organization~(id,localizedName)))",
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
    pages = []
    for element in payload.get("elements", []):
        org = element.get("organization~", {})
        org_id = org.get("id")
        if org_id:
            pages.append({
                "id": str(org_id),
                "name": org.get("localizedName", f"Page {org_id}"),
            })
    return pages


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


# ── Instagram (direct Instagram Login, no Facebook Page) ──────────────────

def build_instagram_login_auth_url(redirect_uri, state):
    """Build the authorization URL for 'Instagram API with Instagram Login'.

    The user signs in with their Instagram professional account directly —
    no Facebook account or linked Facebook Page is involved.
    """
    redirect_uri = redirect_uri or settings.INSTAGRAM_REDIRECT_URI
    if not settings.INSTAGRAM_APP_ID:
        raise OAuthConfigurationError("INSTAGRAM_APP_ID is not configured")

    query = urlencode(
        {
            "client_id": settings.INSTAGRAM_APP_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": ",".join(INSTAGRAM_LOGIN_SCOPES),
            "state": state,
        }
    )
    return f"{INSTAGRAM_LOGIN_AUTH_URL}?{query}"


def exchange_instagram_login_code(code, redirect_uri=None):
    """Exchange the auth code for a long-lived Instagram user access token.

    Step 1: code -> short-lived token (+ user_id) via api.instagram.com.
    Step 2: short-lived -> long-lived (60 day) token via graph.instagram.com.
    """
    redirect_uri = redirect_uri or settings.INSTAGRAM_REDIRECT_URI
    if not settings.INSTAGRAM_APP_ID or not settings.INSTAGRAM_APP_SECRET:
        raise OAuthConfigurationError("Instagram Login credentials are not fully configured")

    short_lived_response = requests.post(
        INSTAGRAM_LOGIN_TOKEN_URL,
        data={
            "client_id": settings.INSTAGRAM_APP_ID,
            "client_secret": settings.INSTAGRAM_APP_SECRET,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        },
        headers={"Accept": "application/json"},
        timeout=settings.SOCIAL_REQUEST_TIMEOUT,
    )
    short_lived_payload = _json_or_raise(short_lived_response)
    _raise_for_error(short_lived_response)

    # Older responses nest the token inside a "data" list; newer ones are flat.
    if isinstance(short_lived_payload.get("data"), list) and short_lived_payload["data"]:
        short_lived_payload = short_lived_payload["data"][0]

    short_lived_token = short_lived_payload.get("access_token")
    user_id = short_lived_payload.get("user_id")
    if not short_lived_token:
        raise SocialPlatformError("Instagram token response did not include an access token.")

    long_lived_response = requests.get(
        f"{INSTAGRAM_GRAPH_BASE}/access_token",
        params={
            "grant_type": "ig_exchange_token",
            "client_secret": settings.INSTAGRAM_APP_SECRET,
            "access_token": short_lived_token,
        },
        headers={"Accept": "application/json"},
        timeout=settings.SOCIAL_REQUEST_TIMEOUT,
    )
    long_lived_payload = _json_or_raise(long_lived_response)
    _raise_for_error(long_lived_response)

    access_token = long_lived_payload.get("access_token")
    if not access_token:
        raise SocialPlatformError("Instagram long-lived token response did not include an access token.")

    return {
        "access_token": access_token,
        "token_type": long_lived_payload.get("token_type", "Bearer"),
        "expires_in": long_lived_payload.get("expires_in"),
        "user_id": user_id,
    }


def fetch_instagram_login_profile(access_token):
    """Fetch the Instagram professional account profile (Instagram Login)."""
    response = requests.get(
        f"{INSTAGRAM_GRAPH_BASE}/{settings.META_GRAPH_API_VERSION}/me",
        params={
            "fields": "user_id,username,account_type,name",
            "access_token": access_token,
        },
        headers={"Accept": "application/json"},
        timeout=settings.SOCIAL_REQUEST_TIMEOUT,
    )
    payload = _json_or_raise(response)
    _raise_for_error(response)
    if not (payload.get("user_id") or payload.get("id")):
        raise SocialPlatformError("Instagram profile response did not include the user id.")
    return payload


def refresh_instagram_login_token(access_token):
    """Refresh a long-lived Instagram Login token (valid 60 days, refreshable)."""
    response = requests.get(
        f"{INSTAGRAM_GRAPH_BASE}/refresh_access_token",
        params={
            "grant_type": "ig_refresh_token",
            "access_token": access_token,
        },
        headers={"Accept": "application/json"},
        timeout=settings.SOCIAL_REQUEST_TIMEOUT,
    )
    payload = _json_or_raise(response)
    _raise_for_error(response)
    if "access_token" not in payload:
        raise SocialPlatformError("Instagram refresh response did not include an access token.")
    return payload


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
    """Fetch YouTube/Google user profile + all channels the account manages."""
    # Step 1: Get Google user info (gives us sub/email/name)
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

    # Step 2: Fetch all YouTube channels for this Google account
    channels_response = requests.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={
            "part": "snippet,contentDetails",
            "mine": "true",
            "maxResults": 50,
        },
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        timeout=settings.SOCIAL_REQUEST_TIMEOUT,
    )
    channels_payload = {}
    try:
        channels_payload = _json_or_raise(channels_response)
    except Exception:
        pass  # Non-fatal — just won't show channel selector

    channels = []
    for item in channels_payload.get("items", []):
        channels.append({
            "id": item.get("id"),
            "title": item.get("snippet", {}).get("title", ""),
            "thumbnail": item.get("snippet", {}).get("thumbnails", {}).get("default", {}).get("url", ""),
        })

    payload["available_channels"] = channels
    # If exactly one channel, auto-set channel_id and title
    if len(channels) == 1:
        payload["channel_id"] = channels[0]["id"]
        payload["channel_title"] = channels[0]["title"]

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
        available_pages = profile_payload.get("available_pages", [])
        return {
            "account_id": account_id,
            "account_type": "personal",
            "platform_username": profile_payload.get("name", ""),
            "access_token": token_payload["access_token"],
            "refresh_token": token_payload.get("refresh_token", ""),
            "token_type": token_payload.get("token_type", "Bearer"),
            "expires_at": expires_at,
            "metadata": {
                "email": profile_payload.get("email"),
                "given_name": profile_payload.get("given_name"),
                "family_name": profile_payload.get("family_name"),
                "available_pages": available_pages,
                "page_count": len(available_pages),
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
        # Use specific channel ID if available (single channel or auto-selected)
        channel_id = profile_payload.get("channel_id") or profile_payload.get("sub")
        channel_title = profile_payload.get("channel_title") or profile_payload.get("name", "")
        account_id = channel_id
        if not account_id:
            raise SocialPlatformError("YouTube profile is missing the user/channel id.")
        available_channels = profile_payload.get("available_channels", [])
        return {
            "account_id": account_id,
            "platform_username": channel_title,
            "access_token": token_payload["access_token"],
            "refresh_token": token_payload.get("refresh_token", ""),
            "token_type": token_payload.get("token_type", "Bearer"),
            "expires_at": expires_at,
            "metadata": {
                "email": profile_payload.get("email"),
                "picture": profile_payload.get("picture"),
                "google_sub": profile_payload.get("sub"),
                "available_channels": available_channels,
                "channel_count": len(available_channels),
            },
        }

    raise SocialPlatformError(f"Unsupported OAuth platform: {platform}")


def build_linkedin_page_account_data(token_payload, page_data, admin_profile):
    """Build SocialAccount data for a LinkedIn Page (organization).

    The access token is the same personal user token — LinkedIn uses the
    member's token to post *on behalf of* the organization they administer.
    The account_id is the organization numeric ID; _author_urn() in the service
    converts it to urn:li:organization:{id} when posting.
    """
    expires_at = None
    expires_in = token_payload.get("expires_in")
    if expires_in:
        expires_at = timezone.now() + timedelta(seconds=int(expires_in))

    page_name = page_data.get("name", "")
    page_id = str(page_data.get("id", ""))
    if not page_id:
        raise SocialPlatformError("LinkedIn page data is missing the organization id.")

    return {
        "account_id": page_id,
        "account_type": "page",
        "platform_username": page_name,
        "account_label": page_name,
        "access_token": token_payload["access_token"],
        "refresh_token": token_payload.get("refresh_token", ""),
        "token_type": token_payload.get("token_type", "Bearer"),
        "expires_at": expires_at,
        "metadata": {
            "page_name": page_name,
            "admin_person_id": admin_profile.get("sub", ""),
            "admin_name": admin_profile.get("name", ""),
        },
    }


def build_instagram_login_account_data(token_payload, profile_payload):
    """Build SocialAccount data for an Instagram-Login (direct) connection.

    Stored under the same 'instagram' platform as the Facebook-based flow, but
    tagged with metadata.login_type='instagram' so the publishing service knows
    to talk to graph.instagram.com with the IG user token (instead of
    graph.facebook.com with a Page token).
    """
    account_id = (
        profile_payload.get("user_id")
        or profile_payload.get("id")
        or token_payload.get("user_id")
    )
    if not account_id:
        raise SocialPlatformError("Instagram account data is incomplete.")

    expires_at = None
    expires_in = token_payload.get("expires_in")
    if expires_in:
        expires_at = timezone.now() + timedelta(seconds=int(expires_in))

    return {
        "account_id": str(account_id),
        "platform_username": profile_payload.get("username", ""),
        "access_token": token_payload["access_token"],
        "refresh_token": "",
        "token_type": token_payload.get("token_type", "Bearer"),
        "expires_at": expires_at,
        "metadata": {
            "login_type": "instagram",
            "account_type": profile_payload.get("account_type"),
            "name": profile_payload.get("name"),
        },
    }


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