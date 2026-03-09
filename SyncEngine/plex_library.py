"""
Plex Library — bridge between PlexAPI and the iOpenPod sync engine.

Connects to a Plex Media Server, browses the music library, downloads
original audio files to temp paths, and returns PCTrack objects that
feed directly into the existing sync pipeline.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import requests

logger = logging.getLogger(__name__)

try:
    from plexapi.server import PlexServer
    from plexapi.exceptions import Unauthorized, NotFound

    PLEXAPI_AVAILABLE = True
except ImportError:
    PLEXAPI_AVAILABLE = False
    logger.warning("plexapi not installed — Plex integration disabled")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PlexConnectionError(Exception):
    """Raised when connecting to the Plex server fails."""


class PlexDownloadError(Exception):
    """Raised when downloading a track fails after all retries."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class PlexConfig:
    """Plex connection parameters."""

    url: str
    token: str
    library_name: Optional[str] = None


# ---------------------------------------------------------------------------
# .env helper
# ---------------------------------------------------------------------------


def load_plex_config_from_env(
    env_path: "str | Path | None" = None,
) -> PlexConfig:
    """Load PlexConfig from a .env file or environment variables.

    Looks for PLEX_URL and PLEX_TOKEN (required) and optionally
    PLEX_LIBRARY_NAME.

    env_path: path to .env file. If None, searches for .env in CWD
              and parent directories (python-dotenv default behaviour).
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        raise ImportError(
            "python-dotenv is required for load_plex_config_from_env. "
            "Install it with: pip install python-dotenv"
        )

    import os

    if env_path is not None:
        load_dotenv(dotenv_path=str(env_path))
    else:
        load_dotenv()  # searches CWD and parents

    url = os.environ.get("PLEX_URL", "").strip()
    token = os.environ.get("PLEX_TOKEN", "").strip()
    library_name = os.environ.get("PLEX_LIBRARY_NAME", "").strip() or None

    if not url:
        raise ValueError("PLEX_URL is not set in environment / .env file")
    if not token:
        raise ValueError("PLEX_TOKEN is not set in environment / .env file")

    return PlexConfig(url=url, token=token, library_name=library_name)


# ---------------------------------------------------------------------------
# Lossless codec set (for best-media selection)
# ---------------------------------------------------------------------------

_LOSSLESS_CODECS = {"flac", "alac", "pcm", "aiff"}


# ---------------------------------------------------------------------------
# PlexLibrary
# ---------------------------------------------------------------------------


class PlexLibrary:
    """Browse a Plex music library and download tracks as PCTrack objects."""

    def __init__(self, config: PlexConfig) -> None:
        if not PLEXAPI_AVAILABLE:
            raise PlexConnectionError(
                "plexapi is not installed. Install it with: pip install plexapi"
            )

        self._config = config
        self._server: PlexServer = self._connect()
        self._music_section = self._get_music_section()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> "PlexServer":
        try:
            server = PlexServer(self._config.url, self._config.token)
            return server
        except Exception as exc:
            raise PlexConnectionError(
                f"Could not connect to Plex at {self._config.url!r}: {exc}"
            ) from exc

    def _get_music_section(self):
        """Return the configured (or first) music library section."""
        try:
            sections = self._server.library.sections()
        except Exception as exc:
            raise PlexConnectionError(
                f"Failed to retrieve library sections: {exc}"
            ) from exc

        music_sections = [s for s in sections if s.type == "artist"]
        if not music_sections:
            raise PlexConnectionError("No music library found on this Plex server")

        if self._config.library_name:
            for section in music_sections:
                if section.title == self._config.library_name:
                    return section
            raise PlexConnectionError(
                f"Music library {self._config.library_name!r} not found. "
                f"Available: {[s.title for s in music_sections]}"
            )

        return music_sections[0]

    # ------------------------------------------------------------------
    # Public browse API
    # ------------------------------------------------------------------

    def get_artists(self) -> list:
        """Return all artist objects in the music library."""
        return self._music_section.all()

    def get_albums(self, artist_rating_key: str) -> list:
        """Return all albums for the given artist rating key."""
        artist = self._music_section.fetchItem(int(artist_rating_key))
        return artist.albums()

    def get_tracks(self, album_rating_key: str) -> list:
        """Return all tracks for the given album rating key."""
        album = self._music_section.fetchItem(int(album_rating_key))
        return album.tracks()

    def get_all_playlists(self) -> list:
        """Return all music playlists on the server."""
        try:
            all_playlists = self._server.playlists()
        except Exception as exc:
            logger.warning("Failed to fetch playlists: %s", exc)
            return []
        return [p for p in all_playlists if p.playlistType == "audio"]

    def get_playlist_tracks(self, playlist_rating_key: str) -> list:
        """Return all tracks in the given playlist."""
        playlist = self._server.fetchItem(int(playlist_rating_key))
        return playlist.items()

    # ------------------------------------------------------------------
    # Best-media selection
    # ------------------------------------------------------------------

    def pick_best_media_part(self, plex_track) -> tuple:
        """Pick the best (media, part) pair from a track's media list.

        Selection priority:
        1. Lossless codec over lossy
        2. Higher bitrate among same type
        3. Larger file size as tiebreaker
        """

        def _score(media_part_pair):
            media, part = media_part_pair
            codec = (media.audioCodec or "").lower()
            is_lossless = codec in _LOSSLESS_CODECS
            bitrate = media.bitrate or 0
            size = part.size or 0
            # Primary sort: lossless first (True > False numerically), then bitrate, then size
            return (is_lossless, bitrate, size)

        candidates = []
        for media in plex_track.media:
            for part in media.parts:
                candidates.append((media, part))

        if not candidates:
            raise PlexDownloadError(
                f"Track {plex_track.title!r} has no media parts"
            )

        return max(candidates, key=_score)

    # ------------------------------------------------------------------
    # Artwork
    # ------------------------------------------------------------------

    def get_artwork(
        self,
        plex_item,
        size: tuple[int, int] = (600, 600),
    ) -> Optional[bytes]:
        """Fetch album art from the Plex /photo/ endpoint.

        Returns raw image bytes, or None if unavailable.
        """
        thumb = getattr(plex_item, "thumb", None)
        if not thumb:
            thumb = getattr(plex_item, "art", None)
        if not thumb:
            return None

        try:
            url = self._server.url(
                f"/photo/:/transcode?url={requests.utils.quote(thumb, safe='')}"
                f"&width={size[0]}&height={size[1]}&minSize=1&upscale=1"
            )
            token = self._server._token
            response = requests.get(
                url,
                headers={"X-Plex-Token": token},
                timeout=30,
            )
            response.raise_for_status()
            return response.content
        except Exception as exc:
            logger.debug("Could not fetch artwork for %r: %s", plex_item, exc)
            return None

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def download_track(
        self,
        plex_track,
        progress_callback: "Callable[[int, int], None] | None" = None,
        max_retries: int = 3,
    ) -> "PCTrack":
        """Download a Plex track to a temp file and return a PCTrack.

        Retries up to max_retries times with exponential backoff.
        Raises PlexDownloadError after all retries are exhausted.
        """
        from .pc_library import PCTrack
        from .transcoder import needs_transcoding as _needs_transcoding

        media, part = self.pick_best_media_part(plex_track)

        # Determine file extension
        if part.file:
            ext = Path(part.file).suffix.lstrip(".")
        else:
            ext = media.audioCodec or "mp3"

        rating_key = str(plex_track.ratingKey)
        tmp_path = Path(f"/tmp/iopenpod_plex_{rating_key}.{ext}")
        # Always start fresh — stale partials from previous sessions cause 416s
        tmp_path.unlink(missing_ok=True)

        last_exc: Exception = RuntimeError("No attempts made")
        for attempt in range(max_retries):
            if attempt > 0:
                sleep_secs = 2 ** (attempt - 1)  # 0s first, 2s, 4s
                logger.debug(
                    "Retry %d/%d for track %r — sleeping %ds",
                    attempt,
                    max_retries - 1,
                    plex_track.title,
                    sleep_secs,
                )
                time.sleep(sleep_secs)

            try:
                self._stream_part(part, plex_track, tmp_path, progress_callback)
                break  # success
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "Download attempt %d failed for %r: %s",
                    attempt + 1,
                    plex_track.title,
                    exc,
                )
        else:
            raise PlexDownloadError(
                f"Failed to download {plex_track.title!r} after {max_retries} attempts: {last_exc}"
            ) from last_exc

        # Build artwork hash (best-effort)
        art_hash: Optional[str] = None
        try:
            art_bytes = self.get_artwork(plex_track)
            if art_bytes:
                art_hash = hashlib.md5(art_bytes).hexdigest()
        except Exception as exc:
            logger.debug("Art hash failed for %r: %s", plex_track.title, exc)

        # Map Plex rating (0-10) → iPod rating (0-100)
        plex_rating = getattr(plex_track, "userRating", None)
        ipod_rating: Optional[int] = None
        if plex_rating is not None:
            try:
                ipod_rating = int(float(plex_rating) * 10)
            except (ValueError, TypeError):
                pass

        # VBR heuristic
        codec = (media.audioCodec or "").lower()
        vbr = codec == "mp3"  # MP3 from Plex may be VBR; others we assume CBR

        # relative_path for iPod mapping
        track_index = getattr(plex_track, "index", None)
        index_str = f"{track_index:02d} " if track_index else ""
        safe_title = plex_track.title or "Unknown"
        relative_path = (
            f"{plex_track.grandparentTitle or 'Unknown Artist'}/"
            f"{plex_track.parentTitle or 'Unknown Album'}/"
            f"{index_str}{safe_title}.{ext}"
        )

        needs_tc = _needs_transcoding(str(tmp_path))

        return PCTrack(
            # File info
            path=str(tmp_path),
            relative_path=relative_path,
            filename=plex_track.title or "Unknown",
            extension=ext,
            mtime=0.0,
            size=part.size or 0,
            # Metadata
            title=plex_track.title or "",
            artist=plex_track.grandparentTitle or "",
            album=plex_track.parentTitle or "",
            album_artist=plex_track.grandparentTitle or None,
            genre=None,
            year=getattr(plex_track, "parentYear", None),
            track_number=getattr(plex_track, "index", None),
            track_total=None,
            disc_number=None,
            disc_total=None,
            duration_ms=int(plex_track.duration) if plex_track.duration else 0,
            bitrate=media.bitrate or None,
            sample_rate=None,
            rating=ipod_rating,
            # Gapless / sound check (computed during sync)
            sound_check=0,
            pregap=0,
            postgap=0,
            sample_count=0,
            gapless_data=0,
            vbr=vbr,
            # Artwork
            art_hash=art_hash,
            # Transcoding flag
            needs_transcoding=needs_tc,
        )

    # ------------------------------------------------------------------
    # Private streaming helper
    # ------------------------------------------------------------------

    def _stream_part(
        self,
        part,
        plex_track,
        dest: Path,
        progress_callback: "Callable[[int, int], None] | None",
    ) -> None:
        """Stream a media part to *dest*, calling progress_callback(bytes_done, total)."""
        url = self._server.url(part.key)
        token = self._server._token

        response = requests.get(
            url,
            headers={"X-Plex-Token": token},
            stream=False,  # load full response; avoids urllib3 IncompleteRead on wrong Content-Length
            timeout=(10, 120),
        )
        response.raise_for_status()

        data = response.content
        dest.write_bytes(data)

        if progress_callback is not None:
            progress_callback(len(data), len(data))
