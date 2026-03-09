# Plex Integration Spec — iOpenPod

## Overview

Add Plex as a first-class music source in iOpenPod, enabling browse-and-cherry-pick sync from a Plex
music library directly to an iPod Classic. The existing sync engine is unchanged; Plex is a new data
source that feeds `PCTrack` objects into the same pipeline.

This is a validation spike. If the end-to-end flow works (Plex → download → transcode → iPod plays),
the Python version gets open-sourced and a SwiftUI rewrite is considered.

---

## Auth & Configuration

- Server URL and token stored in `.env` (`PLEX_URL`, `PLEX_TOKEN`) for this spike
- Music library name is configurable (default: first music library found)
- Future: proper Plex OAuth / `plex.tv` managed auth for the open-source release
- Offline behavior: show a connection error and stop — no offline mode for the spike

---

## Architecture

### No file cache

Always re-download originals from Plex on each sync. No persistent audio file cache on disk.
Trade-off: slower syncs, zero disk footprint outside the iPod itself.

### Fingerprint-only cache

After first sync, store the Chromaprint fingerprint and Plex `updatedAt` timestamp in the mapping.
On subsequent syncs:
- If `updatedAt` is unchanged → skip re-download, use stored fingerprint (fast no-op path)
- If `updatedAt` changed → re-download, recompute fingerprint, re-sync

This avoids redundant downloads without keeping audio files around.

### Identity model (hybrid)

Each synced track stores both:
- `plex_rating_key` — stable Plex track ID, used for fast lookup and queue persistence
- `chromaprint_fingerprint` — acoustic fingerprint, used for cross-format deduplication

Primary key for "is this on the iPod?": `plex_rating_key`. Fingerprint is used for collision detection
and matching if ratingKey lookup fails.

### Source mode: one at a time

The iPod is in either **Local** mode or **Plex** mode — not both simultaneously.

- Switching source does **not** immediately touch the iPod
- On the next sync, all tracks from the previous source appear as "will be removed" in the diff
- User reviews the diff and confirms before anything is deleted

### Download format

Always download the original file from Plex (FLAC, ALAC, MP3, whatever is stored).
iOpenPod's existing transcoder handles conversion (FLAC→ALAC, OGG→AAC, etc.).
Never request Plex to transcode.

### Multi-version tracks

If Plex has multiple versions of a track, automatically pick the highest quality:
1. Lossless (FLAC, ALAC) over lossy
2. Higher bitrate wins among lossy formats
3. Larger file size as tiebreaker

No user decision required.

---

## Download & Transcode Flow

Triggered **immediately when user checks a track** in the browse UI:

1. Fetch original file URL from Plex API
2. Stream download to a temp file (`/tmp/iopenpod_plex_<ratingKey>.<ext>`)
3. If format needs transcoding: run iOpenPod transcoder in background, output to second temp file
4. On failure: retry up to **3 times** with exponential backoff, then mark track as failed
5. Failed tracks are skipped during sync and shown in post-sync error report
6. Temp files are cleaned up after sync completes (success or failure)

Background work runs in a `QThreadPool` worker. Progress shows per-track in the sidebar.

---

## `PlexLibrary` class — `SyncEngine/plex_library.py`

Mirrors `PCLibrary` interface. Returns `list[PCTrack]` pointing to downloaded temp files.

```python
class PlexLibrary:
    def __init__(self, url: str, token: str, library_name: str | None = None): ...

    def connect(self) -> PlexServer: ...
    def get_music_library(self) -> MusicSection: ...

    def get_artists(self) -> list[PlexArtist]: ...
    def get_albums(self, artist_key: str) -> list[PlexAlbum]: ...
    def get_tracks(self, album_key: str) -> list[PlexTrack]: ...

    def download_track(
        self,
        plex_track,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> PCTrack:
        """Download original file, return PCTrack pointing to temp file."""
        ...

    def get_artwork(self, plex_album, size: tuple[int, int] = (600, 600)) -> bytes | None:
        """Fetch album art from Plex /photo/ endpoint."""
        ...
```

### Mapping `PlexTrack` → `PCTrack`

| Plex field | PCTrack field |
|---|---|
| `track.title` | `title` |
| `track.grandparentTitle` | `artist` |
| `track.parentTitle` | `album` |
| `track.grandparentTitle` (album artist) | `album_artist` |
| `track.parentYear` | `year` |
| `track.index` | `track_number` |
| `track.duration` (ms) | `duration_ms` |
| `track.userRating` (0–10 → 0–100) | `rating` |
| Plex artwork API | `art_hash` (computed from fetched bytes) |
| Downloaded temp file path | `path` |
| `plex_rating_key/title.ext` | `relative_path` |
| `track.media[0].parts[0].size` | `size` |
| `track.updatedAt` | stored in mapping, not in PCTrack |

---

## Mapping File Changes

Extend `iOpenPod.json` `TrackMapping` with optional Plex fields:

```json
{
  "dbid": 12345678,
  "source_format": "flac",
  "ipod_format": "alac",
  "plex_rating_key": "98234",
  "plex_updated_at": "2026-01-15T10:30:00",
  "plex_fingerprint_cache": "AQADtJm2...",
  "was_transcoded": true,
  "last_sync": "2026-03-09T12:00:00"
}
```

New `plex_selected_keys` list at the top level of `iOpenPod.json`:

```json
{
  "plex_selected_keys": ["98234", "98235", "99001"],
  "tracks": { ... }
}
```

This is the persisted queue: the set of Plex `ratingKey` values the user has checked for sync.

---

## UI Changes

### Sidebar — new "Plex Library" section

```
Sidebar:
  ┌─ iPod (connected) ──────────────────┐
  │  iPod Classic 160GB                 │
  │  ████████░░░░░░  87GB used          │
  └─────────────────────────────────────┘

  Library
    Local Library          ← existing
    Plex Library           ← new

  Playlists
    ...
```

Clicking "Plex Library" switches the main panel to the Plex browse view.
Clicking "Local Library" switches back to the existing album grid.

The sidebar also shows active download progress when tracks are being fetched:

```
  Plex Library
    ↓ Downloading... (3/12)
```

### Plex browse panel — main area

The panel has two sections: **Library** (artists → albums → tracks) and **Playlists**.

#### Library section

Navigation: **Artists → Albums → Tracks** (3-column or drill-down depending on window size).

Track state is binary — **on iPod** or **not on iPod** — shown as a filled/empty circle or
checkbox to the left of each track row.

Checking a track immediately triggers background download + transcode.
Unchecking removes it from `plex_selected_keys`; if it's on the iPod it will be removed on next sync.

Track rows show:
- Track number, title, duration
- Format badge (FLAC, MP3, ALAC, etc.) sourced from Plex media info
- Download state indicator (queued / downloading / ready / failed)
- "On iPod" indicator

#### Playlists section

Below the Library section in the Plex panel, a **Playlists** list shows all Plex music playlists.
Each playlist has a toggle (enabled/disabled) controlling whether it syncs to the iPod.

```
Playlists
  [x] After Dark              14 tracks   on iPod
  [x] Chill Work Vibes        31 tracks   on iPod
  [ ] Classical: Never H...   88 tracks
  [ ] Custom Kart              6 tracks
```

Toggling a playlist on:
- Adds all its tracks to `plex_selected_keys`
- Queues download + transcode for any tracks not yet downloaded
- Creates a corresponding iPod playlist on next sync

Toggling a playlist off:
- Removes the playlist from `plex_synced_playlist_keys` in the mapping
- On next sync: playlist is deleted from iPod; tracks not in any other synced playlist or
  individually selected are also removed from iPod

### Sync review additions

The existing sync review diff screen adds two new columns for Plex-sourced tracks:
- **Source**: "Plex" badge
- **Format**: original Plex format → iPod format (e.g., "FLAC → ALAC", "MP3 → MP3")

Estimated total download size shown at top of review screen.

### Settings additions

Under existing Settings page, new **Plex** section:
- Plex server URL (populated from `.env`, editable)
- Plex token (masked, populated from `.env`, editable)
- Music library selector (dropdown, populated after connect)
- "Test Connection" button

---

## Storage Warning

Before executing sync, estimate required iPod space:
- Sum file sizes of all tracks flagged for ADD (using `PCTrack.size`)
- Apply a 1.1× safety margin for metadata overhead
- If `required > free_space_gb`, show a blocking warning dialog with the shortfall
- User must manually uncheck tracks to reduce size — no auto-dropping

---

## Ratings

On first sync of a Plex track:
- `plex_track.userRating` (0–10 scale) is converted to iPod scale (× 10 = 0–100)
- Written as the initial iPod rating

After first sync, iPod rating is independent. No write-back to Plex.

---

## Playlist Sync

### Rules

- **Direction**: Plex → iPod only. Plex is always the source of truth.
- **Format**: All Plex playlists (including smart/auto playlists) become **static snapshots** on the
  iPod. Smart playlist rules are not converted — the iPod gets whatever tracks Plex resolved at
  sync time.
- **Opt-in**: User explicitly toggles each playlist on/off in the Plex panel. All playlists default
  to off.
- **Track auto-add**: Enabling a playlist implicitly adds all its tracks to the sync queue. You
  don't need to individually check each track.
- **Track removal**: A track is removed from the iPod on next sync only if it's no longer in any
  enabled playlist AND is not individually checked in the library view.

### Mapping additions

Two new top-level fields in `iOpenPod.json`:

```json
{
  "plex_selected_keys": ["98234", "98235"],
  "plex_synced_playlist_keys": ["5001", "5008"],
  "plex_playlist_snapshots": {
    "5001": {
      "name": "After Dark",
      "track_rating_keys": ["98234", "98235", "99100"],
      "last_synced": "2026-03-09T12:00:00"
    }
  }
}
```

- `plex_synced_playlist_keys`: the set of Plex playlist keys the user has opted in
- `plex_playlist_snapshots`: last-known state of each synced playlist (used to diff on re-sync)

### Update behavior on re-sync

When a synced playlist is fetched from Plex on re-sync, diff its current track list against the
snapshot:

- Tracks added to Plex playlist → added to iPod playlist (downloaded if not on iPod)
- Tracks removed from Plex playlist → removed from iPod playlist; track itself removed from iPod
  if not referenced elsewhere
- Tracks reordered → iPod playlist order updated to match Plex order
- Snapshot updated to reflect new state

This is a diff-and-patch, not a full replace — tracks already on the iPod are not re-downloaded.

### Playlist deletion

If a previously-synced Plex playlist no longer exists on Plex (deleted or no longer accessible):
- Remove from `plex_synced_playlist_keys`
- Delete the corresponding iPod playlist on next sync
- Orphaned tracks (not in any other synced playlist or individually selected) are also removed

---

## Error Handling

| Scenario | Behavior |
|---|---|
| Plex unreachable | Show connection error in sidebar, don't crash |
| Track download fails after 3 retries | Skip track, add to post-sync error report |
| Transcode fails | Skip track, log error, report to user |
| Partial sync (some tracks failed) | iPod is valid state with successfully synced tracks |
| Storage full mid-sync | Abort remaining adds, report which tracks were skipped |
| Plex playlist deleted mid-sync | Treat as removed: delete iPod playlist, remove orphaned tracks |
| Playlist track fetch fails | Skip that playlist's diff, leave existing iPod playlist unchanged, report error |

---

## Dependencies to Add

In `pyproject.toml`:
```
"plexapi>=4.15.0,<5.0.0",
"python-dotenv>=1.0.0,<2.0.0",
```

---

## New Files

| File | Purpose |
|---|---|
| `SyncEngine/plex_library.py` | `PlexLibrary` class: connect, browse, download, map to `PCTrack` |
| `GUI/widgets/plexBrowser.py` | Browse panel: artists → albums → tracks with check state |
| `GUI/widgets/plexSettingsSection.py` | Settings UI section for Plex connection config |

## Modified Files

| File | Change |
|---|---|
| `pyproject.toml` | Add `plexapi`, `python-dotenv` deps |
| `SyncEngine/mapping.py` | Add `plex_rating_key`, `plex_updated_at`, `plex_fingerprint_cache` to `TrackMapping`; add `plex_selected_keys`, `plex_synced_playlist_keys`, `plex_playlist_snapshots` to `MappingFile` |
| `SyncEngine/__init__.py` | Export `PlexLibrary` |
| `GUI/widgets/sidebar.py` | Add "Plex Library" entry under Library section |
| `GUI/app.py` | Wire sidebar Plex click → show `PlexBrowser` panel |
| `GUI/widgets/syncReview.py` | Add Source and Format columns for Plex tracks |
| `GUI/widgets/settingsPage.py` | Add Plex settings section |

---

## Out of Scope (Spike)

- Play count / scrobble write-back to Plex (future)
- Offline browsing / metadata cache (future)
- Multiple simultaneous sources (future)
- Plex managed auth / plex.tv OAuth (future)
- SwiftUI frontend (future, pending spike validation)
