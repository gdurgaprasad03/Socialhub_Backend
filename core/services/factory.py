from .facebook_service import FacebookService
from .instagram_service import InstagramService
from .linkedin_service import LinkedInService
from .threads_service import ThreadsService
from .twitter_service import TwitterService
from .youtube_service import YouTubeService


SERVICE_MAP = {
    "linkedin": LinkedInService,
    "facebook": FacebookService,
    "instagram": InstagramService,
    "threads": ThreadsService,
    "twitter": TwitterService,
    "youtube": YouTubeService,
}


def get_service(platform, user, account=None):
    try:
        service_class = SERVICE_MAP[platform]
    except KeyError as exc:
        raise ValueError(f"Unsupported platform: {platform}") from exc
    return service_class(user, account=account)
