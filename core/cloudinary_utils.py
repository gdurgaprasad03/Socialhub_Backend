import logging
import os
import requests
import tempfile

import cloudinary
import cloudinary.uploader

logger = logging.getLogger(__name__)


def get_transformed_url(url, transformation="c_pad,ar_1:1,b_auto"):
    """
    Injects Cloudinary transformation parameters into a Cloudinary URL.
    If the URL is not a Cloudinary URL, it returns it as-is.
    """
    if not url or "res.cloudinary.com" not in url:
        return url

    # Insert transformation after /upload/
    if "/upload/" in url:
        parts = url.split("/upload/")
        return f"{parts[0]}/upload/{transformation}/{parts[1]}"
    return url


def upload_image_to_cloudinary(source, filename="image"):
    """
    Upload an image to Cloudinary from a local file path, URL, or file object.
    Returns the secure public URL.
    
    Args:
        source: Can be:
            - str: local file path
            - str: http/https URL
            - file-like object: Django InMemoryUploadedFile or similar
        filename: used as the public_id prefix
    
    Returns:
        str: secure HTTPS URL of the uploaded image
    """
    try:
        if hasattr(source, "read"):
            # File object (Django uploaded file)
            source.seek(0)
            result = cloudinary.uploader.upload(
                source,
                folder="socialmedia/images",
                resource_type="image",
            )
        elif isinstance(source, str) and source.startswith(("http://", "https://")):
            # Remote URL — download first then upload
            logger.info("Downloading image from URL for Cloudinary upload: %s", source)
            resp = requests.get(source, timeout=30)
            resp.raise_for_status()
            with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
                tmp.write(resp.content)
                tmp_path = tmp.name
            try:
                result = cloudinary.uploader.upload(
                    tmp_path,
                    folder="socialmedia/images",
                    resource_type="image",
                )
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
        elif isinstance(source, str):
            # Local file path
            if not os.path.exists(source):
                raise ValueError(f"Local file not found: {source}")
            result = cloudinary.uploader.upload(
                source,
                folder="socialmedia/images",
                resource_type="image",
            )
        else:
            raise ValueError(f"Unsupported source type: {type(source)}")

        public_url = result.get("secure_url")
        if not public_url:
            raise ValueError("Cloudinary did not return a secure URL")

        logger.info("Cloudinary image upload success: url=%s", public_url)
        return public_url

    except Exception as exc:
        logger.exception("Cloudinary image upload failed: %s", exc)
        raise


def upload_video_to_cloudinary(source):
    """
    Upload a video to Cloudinary from a local file path or URL.
    Returns the secure public URL.
    
    Args:
        source: local file path string or http/https URL string
    
    Returns:
        str: secure HTTPS URL of the uploaded video
    """
    try:
        if isinstance(source, str) and source.startswith(("http://", "https://")):
            logger.info("Downloading video from URL for Cloudinary upload: %s", source)
            resp = requests.get(source, stream=True, timeout=120)
            resp.raise_for_status()
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as tmp:
                for chunk in resp.iter_content(chunk_size=4 * 1024 * 1024):
                    tmp.write(chunk)
                tmp_path = tmp.name
            try:
                result = cloudinary.uploader.upload(
                    tmp_path,
                    folder="socialmedia/videos",
                    resource_type="video",
                )
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
        elif isinstance(source, str):
            if not os.path.exists(source):
                raise ValueError(f"Local file not found: {source}")
            result = cloudinary.uploader.upload(
                source,
                folder="socialmedia/videos",
                resource_type="video",
            )
        else:
            raise ValueError(f"Unsupported source type: {type(source)}")

        public_url = result.get("secure_url")
        if not public_url:
            raise ValueError("Cloudinary did not return a secure URL")

        logger.info("Cloudinary video upload success: url=%s", public_url)
        return public_url

    except Exception as exc:
        logger.exception("Cloudinary video upload failed: %s", exc)
        raise