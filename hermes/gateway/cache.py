"""
Media cache: download platform media to local files.

Platform media URLs are typically temporary. Adapters should download on
receipt and pass the local path downstream.
"""

from __future__ import annotations

from hermes.config import HERMES_HOME


MEDIA_CACHE_DIR = HERMES_HOME / "cache"


def cache_image(data: bytes, filename: str) -> str:
    """Save image bytes to local cache, return the local path."""
    img_dir = MEDIA_CACHE_DIR / "images"
    img_dir.mkdir(parents=True, exist_ok=True)
    path = img_dir / filename
    path.write_bytes(data)
    return str(path)


def cache_audio(data: bytes, filename: str) -> str:
    """Save audio bytes to local cache, return the local path."""
    audio_dir = MEDIA_CACHE_DIR / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    path = audio_dir / filename
    path.write_bytes(data)
    return str(path)
