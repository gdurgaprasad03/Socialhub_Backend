"""
Platform CRUD operations beyond basic create/delete.
Read and Update operations for each supported platform.
"""
import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)

GRAPH_VERSION = getattr(settings, "META_GRAPH_API_VERSION", "v23.0")


# ── LinkedIn ──────────────────────────────────────────────────────────────

def linkedin_get_post(post_urn, access_token):
   
    encoded_urn = requests.utils.quote(post_urn, safe="")
    url = f"https://api.linkedin.com/rest/posts/{encoded_urn}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "LinkedIn-Version": "202602",
        "X-Restli-Protocol-Version": "2.0.0",
    }
    response = requests.get(url, headers=headers, timeout=settings.SOCIAL_REQUEST_TIMEOUT)
    if not response.ok:
        raise Exception(f"LinkedIn get post failed: {response.text[:300]}")
    return response.json()


# ── Facebook ──────────────────────────────────────────────────────────────

def facebook_get_post(post_id, access_token):
    """Get a Facebook post by ID."""
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{post_id}"
    response = requests.get(
        url,
        params={
            "fields": "id,message,created_time,full_picture,permalink_url",
            "access_token": access_token,
        },
        timeout=settings.SOCIAL_REQUEST_TIMEOUT,
    )
    if not response.ok:
        raise Exception(f"Facebook get post failed: {response.text[:300]}")
    return response.json()


def facebook_update_post(post_id, access_token, message):
  
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{post_id}"
    response = requests.post(
        url,
        data={"message": message, "access_token": access_token},
        timeout=settings.SOCIAL_REQUEST_TIMEOUT,
    )
    if not response.ok:
        raise Exception(f"Facebook update post failed: {response.text[:300]}")
    return response.json()


# ── Instagram ─────────────────────────────────────────────────────────────

def instagram_get_post(media_id, access_token):
    """Get an Instagram media post by ID."""
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{media_id}"
    response = requests.get(
        url,
        params={
            "fields": "id,caption,media_type,media_url,permalink,timestamp",
            "access_token": access_token,
        },
        timeout=settings.SOCIAL_REQUEST_TIMEOUT,
    )
    if not response.ok:
        raise Exception(f"Instagram get post failed: {response.text[:300]}")
    return response.json()


def instagram_update_post(media_id, access_token, caption):
    
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{media_id}"
    response = requests.post(
        url,
        params={"caption": caption, "access_token": access_token},
        timeout=settings.SOCIAL_REQUEST_TIMEOUT,
    )
    if not response.ok:
        raise Exception(f"Instagram update caption failed: {response.text[:300]}")
    return response.json()


# ── Twitter ───────────────────────────────────────────────────────────────

def twitter_get_tweet(tweet_id, access_token):
   
    url = f"https://api.twitter.com/2/tweets/{tweet_id}"
    response = requests.get(
        url,
        params={"tweet.fields": "id,text,created_at,public_metrics"},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=settings.SOCIAL_REQUEST_TIMEOUT,
    )
    if not response.ok:
        raise Exception(f"Twitter get tweet failed: {response.text[:300]}")
    return response.json()


# ── YouTube ───────────────────────────────────────────────────────────────

def youtube_get_video(video_id, access_token):
    url = "https://www.googleapis.com/youtube/v3/videos"
    response = requests.get(
        url,
        params={
            "part": "snippet,status,statistics",
            "id": video_id,
        },
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=settings.SOCIAL_REQUEST_TIMEOUT,
    )
    if not response.ok:
        raise Exception(f"YouTube get video failed: {response.text[:300]}")
    data = response.json()
    items = data.get("items", [])
    if not items:
        raise Exception(f"YouTube video not found: {video_id}")
    return items[0]


def youtube_update_video(video_id, access_token, title=None, description=None, privacy=None):
   
    current = youtube_get_video(video_id, access_token)
    snippet = current.get("snippet", {})
    current_status = current.get("status", {})

    update_payload = {
        "id": video_id,
        "snippet": {
            "title": title or snippet.get("title", "Untitled"),
            "description": description if description is not None else snippet.get("description", ""),
            "categoryId": snippet.get("categoryId", "22"),
        },
        "status": {
            "privacyStatus": privacy or current_status.get("privacyStatus", "public"),
        },
    }

    url = "https://www.googleapis.com/youtube/v3/videos"
    response = requests.put(
        url,
        params={"part": "snippet,status"},
        json=update_payload,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        timeout=settings.SOCIAL_REQUEST_TIMEOUT,
    )
    if not response.ok:
        raise Exception(f"YouTube update video failed: {response.text[:300]}")
    return response.json()


# ── Dispatcher ────────────────────────────────────────────────────────────

def get_platform_post(platform, post_urn, access_token):
    """Dispatch get operation to correct platform."""
    if platform == "linkedin":
        return linkedin_get_post(post_urn, access_token)
    elif platform == "facebook":
        return facebook_get_post(post_urn, access_token)
    elif platform == "instagram":
        return instagram_get_post(post_urn, access_token)
    elif platform == "twitter":
        return twitter_get_tweet(post_urn, access_token)
    elif platform == "youtube":
        return youtube_get_video(post_urn, access_token)
    else:
        raise ValueError(f"Unsupported platform for read: {platform}")


def update_platform_post(platform, post_urn, access_token, **kwargs):
   
    if platform == "linkedin":
        raise ValueError("LinkedIn does not support updating posts via API.")
    elif platform == "twitter":
        raise ValueError("Twitter does not support updating tweets via API.")
    elif platform == "facebook":
        message = kwargs.get("message") or kwargs.get("content", "")
        return facebook_update_post(post_urn, access_token, message)
    elif platform == "instagram":
        caption = kwargs.get("caption") or kwargs.get("content", "")
        return instagram_update_post(post_urn, access_token, caption)
    elif platform == "youtube":
        return youtube_update_video(
            post_urn, access_token,
            title=kwargs.get("title"),
            description=kwargs.get("description"),
            privacy=kwargs.get("privacy"),
        )
    else:
        raise ValueError(f"Unsupported platform for update: {platform}")