"""
PlexBrowser — browse and cherry-pick tracks from a Plex music library.

Layout:
  [← Back]  Breadcrumb                         [Sync to iPod]
  ─────────────────────────────────────────────────────────────
  [Connection error bar — hidden when connected]
  ┌─ Content area (QStackedWidget) ────────────────────────────┐
  │  Page 0: Artist list                                       │
  │  Page 1: Album grid (for selected artist)                  │
  │  Page 2: Track list (for selected album)                   │
  │  Page 3: Playlists list                                    │
  └────────────────────────────────────────────────────────────┘
  [bottom tab bar: Library | Playlists]
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PyQt6.QtCore import (
    Qt, QSize, QRunnable, QObject, QThreadPool, pyqtSignal, pyqtSlot,
)
from PyQt6.QtGui import QColor, QFont, QPixmap, QImage
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QStackedWidget, QListWidget, QListWidgetItem,
    QTableWidget, QTableWidgetItem, QHeaderView, QCheckBox, QAbstractItemView,
    QSizePolicy, QGridLayout,
)

from ..styles import Colors, FONT_FAMILY, Metrics, btn_css, accent_btn_css, scrollbar_css

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

try:
    from SyncEngine.plex_library import (  # type: ignore[import]
        PlexLibrary, PlexConfig, load_plex_config_from_env,
        PlexConnectionError, PlexDownloadError,
    )
    PLEX_AVAILABLE = True
except ImportError:
    PLEX_AVAILABLE = False


# ── Download worker ──────────────────────────────────────────────────────────

class _DownloadSignals(QObject):
    started = pyqtSignal()
    progress = pyqtSignal(int, int)       # bytes_done, total
    finished = pyqtSignal(str, object)    # plex_rating_key, pc_track
    failed = pyqtSignal(str, str)         # plex_rating_key, error_str


class _DownloadWorker(QRunnable):
    """QRunnable that downloads a single Plex track."""

    def __init__(self, plex_library, plex_track):
        super().__init__()
        self._library = plex_library
        self._track = plex_track
        self._rating_key = str(plex_track.ratingKey)
        self.signals = _DownloadSignals()

    @pyqtSlot()
    def run(self):
        self.signals.started.emit()
        try:
            def _progress(done: int, total: int):
                self.signals.progress.emit(done, total)

            pc_track = self._library.download_track(self._track, _progress)
            self.signals.finished.emit(self._rating_key, pc_track)
        except Exception as exc:
            log.exception("Plex download failed for key %s", self._rating_key)
            self.signals.failed.emit(self._rating_key, str(exc))


# ── Small reusable widgets ───────────────────────────────────────────────────

def _make_separator() -> QFrame:
    sep = QFrame()
    sep.setFixedHeight(1)
    sep.setStyleSheet(f"background-color: {Colors.BORDER_SUBTLE};")
    return sep


def _make_section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setFont(QFont(FONT_FAMILY, 9, QFont.Weight.Bold))
    lbl.setStyleSheet(f"color: {Colors.TEXT_TERTIARY}; background: transparent; padding: 4px 0 2px 0;")
    return lbl


def _status_badge(text: str, color: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setFont(QFont(FONT_FAMILY, 9, QFont.Weight.DemiBold))
    lbl.setStyleSheet(
        f"color: {color}; background: transparent; padding: 0 4px;"
    )
    return lbl


# ── Album card (used in album grid, Page 1) ──────────────────────────────────

class _AlbumCard(QFrame):
    clicked = pyqtSignal(object)  # emits plex album object

    _ART_SIZE = 120

    def __init__(self, album, parent=None):
        super().__init__(parent)
        self._album = album
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(148, 190)
        self.setStyleSheet(f"""
            QFrame {{
                background: {Colors.SURFACE_ALT};
                border: 1px solid {Colors.BORDER_SUBTLE};
                border-radius: {Metrics.BORDER_RADIUS}px;
            }}
            QFrame:hover {{
                background: {Colors.SURFACE_RAISED};
                border-color: {Colors.BORDER};
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)

        # Album art placeholder
        self._art_label = QLabel()
        self._art_label.setFixedSize(self._ART_SIZE, self._ART_SIZE)
        self._art_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._art_label.setStyleSheet(
            f"background: {Colors.SURFACE}; border-radius: {Metrics.BORDER_RADIUS_SM}px;"
            " border: none;"
        )
        self._art_label.setText("♫")
        self._art_label.setFont(QFont(FONT_FAMILY, 28))
        layout.addWidget(self._art_label, alignment=Qt.AlignmentFlag.AlignHCenter)

        # Album title
        title_text = getattr(album, 'title', str(album))
        title = QLabel(title_text)
        title.setFont(QFont(FONT_FAMILY, 10, QFont.Weight.DemiBold))
        title.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; background: transparent; border: none;")
        title.setWordWrap(True)
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(title)

        # Track count
        try:
            count = album.leafCount
        except Exception:
            count = "?"
        sub = QLabel(f"{count} tracks")
        sub.setFont(QFont(FONT_FAMILY, 9))
        sub.setStyleSheet(f"color: {Colors.TEXT_TERTIARY}; background: transparent; border: none;")
        sub.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(sub)

    def set_art(self, pixmap: QPixmap):
        scaled = pixmap.scaled(
            self._ART_SIZE, self._ART_SIZE,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._art_label.setPixmap(scaled)
        self._art_label.setText("")

    def mousePressEvent(self, event):
        if event and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._album)
        super().mousePressEvent(event)


# ── Artist row widget (used in artist list, Page 0) ──────────────────────────

class _ArtistRow(QWidget):
    clicked = pyqtSignal(object)  # emits plex artist object

    def __init__(self, artist, parent=None):
        super().__init__(parent)
        self._artist = artist
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"""
            QWidget {{
                background: transparent;
                border-radius: {Metrics.BORDER_RADIUS_SM}px;
            }}
            QWidget:hover {{
                background: {Colors.SURFACE_ALT};
            }}
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(10)

        # Thumbnail placeholder
        self._thumb = QLabel("🧑‍🎤")
        self._thumb.setFixedSize(40, 40)
        self._thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._thumb.setFont(QFont(FONT_FAMILY, 18))
        self._thumb.setStyleSheet(
            f"background: {Colors.SURFACE_RAISED}; border-radius: 20px; border: none;"
        )
        layout.addWidget(self._thumb)

        name_text = getattr(artist, 'title', str(artist))
        name = QLabel(name_text)
        name.setFont(QFont(FONT_FAMILY, 12, QFont.Weight.DemiBold))
        name.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; background: transparent; border: none;")
        layout.addWidget(name)
        layout.addStretch()

        chevron = QLabel("›")
        chevron.setFont(QFont(FONT_FAMILY, 16))
        chevron.setStyleSheet(f"color: {Colors.TEXT_TERTIARY}; background: transparent; border: none;")
        layout.addWidget(chevron)

    def mousePressEvent(self, event):
        if event and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._artist)
        super().mousePressEvent(event)


# ── Main PlexBrowser widget ───────────────────────────────────────────────────

class PlexBrowser(QWidget):
    """Main Plex library browse panel."""

    sync_requested = pyqtSignal()
    download_progress = pyqtSignal(int, int)  # (current, total)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._plex_library = None
        self._connected = False
        # plex_rating_key → PCTrack
        self._downloaded_tracks: dict[str, object] = {}
        # plex_rating_key → "queued" | "downloading" | "ready" | "failed"
        self._download_status: dict[str, str] = {}
        # plex_rating_key → bool (checked for sync)
        self._checked_tracks: dict[str, bool] = {}
        # playlist ratingKey → bool (enabled for sync)
        self._synced_playlists: dict[str, bool] = {}
        # nav stack: list of (page_index, label_text)
        self._nav_stack: list[tuple[int, str]] = []
        self._current_artist = None
        self._current_album = None
        # cached track list for the current album page (avoids re-fetching on status update)
        self._current_album_tracks: list = []

        self._build_ui()

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        # ── Header row ──────────────────────────────────────────────────────
        header = QHBoxLayout()
        header.setSpacing(8)

        self._back_btn = QPushButton("← Back")
        self._back_btn.setFont(QFont(FONT_FAMILY, 10))
        self._back_btn.setStyleSheet(btn_css(
            bg="transparent",
            bg_hover=Colors.SURFACE_RAISED,
            bg_press=Colors.SURFACE_ALT,
            fg=Colors.TEXT_SECONDARY,
            padding="6px 10px",
        ))
        self._back_btn.setVisible(False)
        self._back_btn.clicked.connect(self._go_back)
        header.addWidget(self._back_btn)

        self._breadcrumb = QLabel("Plex Library")
        self._breadcrumb.setFont(QFont(FONT_FAMILY, 14, QFont.Weight.Bold))
        self._breadcrumb.setStyleSheet(f"color: {Colors.TEXT_PRIMARY}; background: transparent;")
        header.addWidget(self._breadcrumb)
        header.addStretch()

        self._sync_btn = QPushButton("Sync to iPod")
        self._sync_btn.setFont(QFont(FONT_FAMILY, 10, QFont.Weight.DemiBold))
        self._sync_btn.setStyleSheet(accent_btn_css())
        self._sync_btn.setEnabled(False)
        self._sync_btn.clicked.connect(self.sync_requested)
        header.addWidget(self._sync_btn)

        root.addLayout(header)
        root.addWidget(_make_separator())

        # ── Connection error bar (hidden when OK) ────────────────────────────
        self._error_bar = QLabel("Cannot connect to Plex — check server URL and token")
        self._error_bar.setFont(QFont(FONT_FAMILY, 10))
        self._error_bar.setStyleSheet(f"""
            QLabel {{
                background: rgba(255,107,107,40);
                border: 1px solid rgba(255,107,107,120);
                border-radius: {Metrics.BORDER_RADIUS_SM}px;
                color: {Colors.DANGER};
                padding: 8px 12px;
            }}
        """)
        self._error_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._error_bar.hide()
        root.addWidget(self._error_bar)

        # ── Content stack ────────────────────────────────────────────────────
        self._content = QStackedWidget()
        self._content.setStyleSheet("QStackedWidget { background: transparent; }")

        # Page 0: Artist list
        self._artist_page = self._build_artist_page()
        self._content.addWidget(self._artist_page)

        # Page 1: Album grid
        self._album_page, self._album_grid_layout = self._build_album_page()
        self._content.addWidget(self._album_page)

        # Page 2: Track list
        self._track_page, self._track_table = self._build_track_page()
        self._content.addWidget(self._track_page)

        # Page 3: Playlist list
        self._playlist_page, self._playlist_list = self._build_playlist_page()
        self._content.addWidget(self._playlist_page)

        root.addWidget(self._content, stretch=1)

        # ── Bottom tab bar ───────────────────────────────────────────────────
        tab_row = QHBoxLayout()
        tab_row.setSpacing(0)

        self._tab_library = QPushButton("Library")
        self._tab_library.setFont(QFont(FONT_FAMILY, 10, QFont.Weight.DemiBold))
        self._tab_library.setCheckable(True)
        self._tab_library.setChecked(True)
        self._tab_library.clicked.connect(self._on_tab_library)

        self._tab_playlists = QPushButton("Playlists")
        self._tab_playlists.setFont(QFont(FONT_FAMILY, 10, QFont.Weight.DemiBold))
        self._tab_playlists.setCheckable(True)
        self._tab_playlists.setChecked(False)
        self._tab_playlists.clicked.connect(self._on_tab_playlists)

        _tab_css_active = btn_css(
            bg=Colors.ACCENT_DIM,
            bg_hover=Colors.ACCENT_HOVER,
            bg_press=Colors.ACCENT_PRESS,
            border=f"1px solid {Colors.ACCENT_BORDER}",
            radius=Metrics.BORDER_RADIUS_SM,
            padding="8px 20px",
        )
        _tab_css_idle = btn_css(
            bg=Colors.SURFACE_ALT,
            bg_hover=Colors.SURFACE_ACTIVE,
            bg_press=Colors.SURFACE,
            radius=Metrics.BORDER_RADIUS_SM,
            padding="8px 20px",
        )
        self._tab_css_active = _tab_css_active
        self._tab_css_idle = _tab_css_idle
        self._tab_library.setStyleSheet(_tab_css_active)
        self._tab_playlists.setStyleSheet(_tab_css_idle)

        tab_row.addStretch()
        tab_row.addWidget(self._tab_library)
        tab_row.addWidget(self._tab_playlists)
        tab_row.addStretch()

        root.addLayout(tab_row)

    def _build_artist_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            + scrollbar_css()
        )

        self._artist_container = QWidget()
        self._artist_container.setStyleSheet("background: transparent;")
        self._artist_layout = QVBoxLayout(self._artist_container)
        self._artist_layout.setContentsMargins(0, 0, 0, 0)
        self._artist_layout.setSpacing(2)
        self._artist_layout.addStretch()

        scroll.setWidget(self._artist_container)
        layout.addWidget(scroll)
        return page

    def _build_album_page(self) -> tuple[QWidget, QGridLayout]:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet(
            "QScrollArea { background: transparent; border: none; }"
            + scrollbar_css()
        )

        container = QWidget()
        container.setStyleSheet("background: transparent;")
        grid = QGridLayout(container)
        grid.setContentsMargins(4, 4, 4, 4)
        grid.setSpacing(Metrics.GRID_SPACING)

        scroll.setWidget(container)
        layout.addWidget(scroll)
        return page, grid

    def _build_track_page(self) -> tuple[QWidget, QTableWidget]:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        table = QTableWidget()
        table.setColumnCount(6)
        table.setHorizontalHeaderLabels(["", "#", "Title", "Duration", "Format", "Status"])
        table.setStyleSheet(f"""
            QTableWidget {{
                background: transparent;
                border: none;
                gridline-color: {Colors.GRIDLINE};
                color: {Colors.TEXT_PRIMARY};
                selection-background-color: {Colors.SELECTION};
            }}
            QHeaderView::section {{
                background: {Colors.SURFACE_ALT};
                color: {Colors.TEXT_TERTIARY};
                border: none;
                border-bottom: 1px solid {Colors.BORDER_SUBTLE};
                padding: 4px 8px;
                font-size: 10px;
                font-weight: 600;
            }}
            QTableWidget::item {{
                border: none;
                padding: 4px 8px;
            }}
            QTableWidget::item:selected {{
                background: {Colors.SELECTION};
            }}
        """ + scrollbar_css())
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().hide()
        table.setShowGrid(False)

        hh = table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(0, 36)
        table.setColumnWidth(1, 40)
        table.setColumnWidth(3, 72)
        table.setColumnWidth(4, 72)
        table.setColumnWidth(5, 100)
        table.verticalHeader().setDefaultSectionSize(36)

        layout.addWidget(table)
        return page, table

    def _build_playlist_page(self) -> tuple[QWidget, QListWidget]:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        lst = QListWidget()
        lst.setStyleSheet(f"""
            QListWidget {{
                background: transparent;
                border: none;
                color: {Colors.TEXT_PRIMARY};
            }}
            QListWidget::item {{
                padding: 4px 8px;
                border-bottom: 1px solid {Colors.BORDER_SUBTLE};
            }}
            QListWidget::item:selected {{
                background: {Colors.SELECTION};
                border-radius: {Metrics.BORDER_RADIUS_SM}px;
            }}
        """ + scrollbar_css())
        lst.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        layout.addWidget(lst)
        return page, lst

    # ── Tab handlers ─────────────────────────────────────────────────────────

    def _on_tab_library(self):
        self._tab_library.setStyleSheet(self._tab_css_active)
        self._tab_playlists.setStyleSheet(self._tab_css_idle)
        self._tab_library.setChecked(True)
        self._tab_playlists.setChecked(False)
        # Return to artist list (top of library nav)
        self._nav_stack.clear()
        self._show_artists()

    def _on_tab_playlists(self):
        self._tab_playlists.setStyleSheet(self._tab_css_active)
        self._tab_library.setStyleSheet(self._tab_css_idle)
        self._tab_playlists.setChecked(True)
        self._tab_library.setChecked(False)
        self._nav_stack.clear()
        self._show_playlists()

    # ── Navigation ───────────────────────────────────────────────────────────

    def _push_nav(self, page_index: int, label: str):
        self._nav_stack.append((page_index, label))
        self._content.setCurrentIndex(page_index)
        self._breadcrumb.setText(label)
        self._back_btn.setVisible(len(self._nav_stack) > 1)

    def _go_back(self):
        if len(self._nav_stack) > 1:
            self._nav_stack.pop()
            page_index, label = self._nav_stack[-1]
            self._content.setCurrentIndex(page_index)
            self._breadcrumb.setText(label)
            self._back_btn.setVisible(len(self._nav_stack) > 1)

    # ── Connection ───────────────────────────────────────────────────────────

    def connect_plex(self, config) -> bool:
        """Attempt to connect to Plex. Shows error bar if connection fails.

        ``config`` should expose ``.url`` and ``.token`` attributes, or be a
        plain dict with "url" and "token" keys.  Returns True on success.
        """
        if not PLEX_AVAILABLE:
            self._show_error(
                "PlexLibrary not installed. "
                "Run: pip install plexapi python-dotenv"
            )
            return False

        try:
            if isinstance(config, dict):
                url = config.get("url", "")
                token = config.get("token", "")
                library_name = config.get("library_name")
            else:
                url = getattr(config, "url", "")
                token = getattr(config, "token", "")
                library_name = getattr(config, "library_name", None)

            plex_config = PlexConfig(url=url, token=token, library_name=library_name)
            self._plex_library = PlexLibrary(plex_config)  # connects eagerly in __init__
            self._connected = True
            self._error_bar.hide()
            self._show_artists()
            return True
        except Exception as exc:
            log.warning("Plex connection failed: %s", exc)
            self._show_error(f"Cannot connect to Plex — {exc}")
            self._connected = False
            return False

    def _show_error(self, msg: str):
        self._error_bar.setText(msg)
        self._error_bar.show()

    # ── Public API ───────────────────────────────────────────────────────────

    def refresh(self):
        """Reload the current view from Plex."""
        if not self._connected:
            return
        current = self._content.currentIndex()
        if current == 0:
            self._show_artists()
        elif current == 1 and self._current_artist is not None:
            self._show_albums(self._current_artist)
        elif current == 2 and self._current_album is not None:
            self._show_tracks(self._current_album)
        elif current == 3:
            self._show_playlists()

    # ── Page builders ────────────────────────────────────────────────────────

    def _show_artists(self):
        """Load and display the artist list (Page 0)."""
        # Clear previous contents (keep the trailing stretch)
        while self._artist_layout.count() > 1:
            item = self._artist_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self._connected or self._plex_library is None:
            placeholder = QLabel("Not connected to Plex")
            placeholder.setFont(QFont(FONT_FAMILY, 12))
            placeholder.setStyleSheet(f"color: {Colors.TEXT_TERTIARY}; background: transparent;")
            placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._artist_layout.insertWidget(0, placeholder)
            self._nav_stack.clear()
            self._nav_stack.append((0, "Plex Library"))
            self._content.setCurrentIndex(0)
            self._breadcrumb.setText("Plex Library")
            self._back_btn.setVisible(False)
            return

        try:
            artists = self._plex_library.get_artists()
        except Exception as exc:
            log.exception("Failed to load artists")
            self._show_error(f"Failed to load artists — {exc}")
            return

        for artist in artists:
            row = _ArtistRow(artist)
            row.clicked.connect(self._show_albums)
            self._artist_layout.insertWidget(self._artist_layout.count() - 1, row)

        self._nav_stack.clear()
        self._nav_stack.append((0, "Plex Library"))
        self._content.setCurrentIndex(0)
        self._breadcrumb.setText("Plex Library")
        self._back_btn.setVisible(False)

    def _show_albums(self, artist):
        """Load and display albums for ``artist`` (Page 1)."""
        self._current_artist = artist

        # Clear grid
        while self._album_grid_layout.count():
            item = self._album_grid_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        artist_name = getattr(artist, 'title', str(artist))

        if self._plex_library is None:
            return

        try:
            albums = self._plex_library.get_albums(str(artist.ratingKey))
        except Exception as exc:
            log.exception("Failed to load albums for %s", artist_name)
            self._show_error(f"Failed to load albums — {exc}")
            return

        cols = max(1, 4)  # fixed 4-column grid
        for idx, album in enumerate(albums):
            card = _AlbumCard(album)
            card.clicked.connect(self._show_tracks)
            self._album_grid_layout.addWidget(card, idx // cols, idx % cols)

        # Add spacer row at bottom
        self._album_grid_layout.setRowStretch(
            (len(albums) // cols) + 1, 1
        )

        label = f"{artist_name} / Albums"
        self._push_nav(1, label)

    def _show_tracks(self, album):
        """Load and display tracks for ``album`` (Page 2)."""
        self._current_album = album

        self._track_table.setRowCount(0)

        album_title = getattr(album, 'title', str(album))
        artist_name = getattr(album, 'parentTitle', '')

        if self._plex_library is None:
            return

        try:
            tracks = self._plex_library.get_tracks(str(album.ratingKey))
        except Exception as exc:
            log.exception("Failed to load tracks for %s", album_title)
            self._show_error(f"Failed to load tracks — {exc}")
            return

        self._current_album_tracks = tracks
        self._track_table.setRowCount(len(tracks))
        for row, track in enumerate(tracks):
            rating_key = str(track.ratingKey)
            is_checked = self._checked_tracks.get(rating_key, False)

            # Column 0: checkbox
            cb = QCheckBox()
            cb.setChecked(is_checked)
            cb.setStyleSheet("QCheckBox { margin-left: 8px; }")
            cb.toggled.connect(lambda checked, k=rating_key: self._on_track_toggled(k, checked))
            cb_widget = QWidget()
            cb_layout = QHBoxLayout(cb_widget)
            cb_layout.setContentsMargins(4, 0, 0, 0)
            cb_layout.addWidget(cb)
            self._track_table.setCellWidget(row, 0, cb_widget)

            # Column 1: track number
            index = getattr(track, 'index', row + 1)
            num_item = QTableWidgetItem(str(index))
            num_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            num_item.setForeground(
                QColor(Colors.TEXT_TERTIARY)
            )
            self._track_table.setItem(row, 1, num_item)

            # Column 2: title
            title = getattr(track, 'title', '—')
            title_item = QTableWidgetItem(title)
            self._track_table.setItem(row, 2, title_item)

            # Column 3: duration
            duration_ms = getattr(track, 'duration', 0) or 0
            secs = duration_ms // 1000
            dur_str = f"{secs // 60}:{secs % 60:02d}"
            dur_item = QTableWidgetItem(dur_str)
            dur_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            dur_item.setForeground(
                QColor(Colors.TEXT_SECONDARY)
            )
            self._track_table.setItem(row, 3, dur_item)

            # Column 4: format badge
            fmt = self._get_track_format(track)
            fmt_item = QTableWidgetItem(fmt)
            fmt_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._track_table.setItem(row, 4, fmt_item)

            # Column 5: status
            status = self._download_status.get(rating_key, "")
            status_text, status_color = self._status_display(status)
            status_item = QTableWidgetItem(status_text)
            status_item.setForeground(
                QColor(status_color)
            )
            self._track_table.setItem(row, 5, status_item)

        breadcrumb = f"{artist_name} / {album_title}" if artist_name else album_title
        self._push_nav(2, breadcrumb)

    def _show_playlists(self):
        """Load and display Plex playlists (Page 3)."""
        self._playlist_list.clear()

        if not self._connected or self._plex_library is None:
            item = QListWidgetItem("Not connected to Plex")
            item.setForeground(
                QColor(Colors.TEXT_TERTIARY)
            )
            self._playlist_list.addItem(item)
            self._nav_stack.clear()
            self._nav_stack.append((3, "Playlists"))
            self._content.setCurrentIndex(3)
            self._breadcrumb.setText("Playlists")
            self._back_btn.setVisible(False)
            return

        try:
            playlists = self._plex_library.get_all_playlists()
        except Exception as exc:
            log.exception("Failed to load playlists")
            self._show_error(f"Failed to load playlists — {exc}")
            playlists = []

        for pl in playlists:
            pl_key = str(pl.ratingKey)
            enabled = self._synced_playlists.get(pl_key, False)
            name = getattr(pl, 'title', str(pl))
            try:
                count = pl.leafCount
            except Exception:
                count = "?"
            display = f"{'[✓] ' if enabled else '[ ] '}{name}   ({count} tracks)"
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, pl)
            self._playlist_list.addItem(item)

        self._playlist_list.itemClicked.connect(self._on_playlist_item_clicked)

        self._nav_stack.clear()
        self._nav_stack.append((3, "Playlists"))
        self._content.setCurrentIndex(3)
        self._breadcrumb.setText("Playlists")
        self._back_btn.setVisible(False)

    # ── Track interaction ────────────────────────────────────────────────────

    def _on_track_toggled(self, plex_rating_key: str, checked: bool):
        """Called when the user checks/unchecks a track checkbox."""
        self._checked_tracks[plex_rating_key] = checked
        if checked:
            self._queue_download(plex_rating_key)
        self._update_sync_button()

    def _queue_download(self, plex_rating_key: str):
        """Find the track object for this key and start a download worker."""
        if self._plex_library is None:
            return
        if self._download_status.get(plex_rating_key) in ("downloading", "ready"):
            return

        # Use the cached track list — avoids a network round-trip per checkbox click
        tracks = self._current_album_tracks
        if not tracks:
            return

        track_obj = None
        for t in tracks:
            if str(t.ratingKey) == plex_rating_key:
                track_obj = t
                break

        if track_obj is None:
            return

        self._download_status[plex_rating_key] = "queued"
        self._refresh_track_row_status(plex_rating_key, "queued")

        worker = _DownloadWorker(self._plex_library, track_obj)
        worker.signals.started.connect(
            lambda k=plex_rating_key: self._on_download_started(k)
        )
        worker.signals.finished.connect(self._on_download_finished)
        worker.signals.failed.connect(self._on_download_failed)
        QThreadPool.globalInstance().start(worker)

    def _on_download_started(self, plex_rating_key: str):
        self._download_status[plex_rating_key] = "downloading"
        self._refresh_track_row_status(plex_rating_key, "downloading")
        self._emit_progress()

    def _on_download_finished(self, plex_rating_key: str, pc_track):
        self._download_status[plex_rating_key] = "ready"
        self._downloaded_tracks[plex_rating_key] = pc_track
        self._refresh_track_row_status(plex_rating_key, "ready")
        self._emit_progress()
        self._update_sync_button()

    def _on_download_failed(self, plex_rating_key: str, error_str: str):
        log.warning("Download failed for key %s: %s", plex_rating_key, error_str)
        self._download_status[plex_rating_key] = "failed"
        self._refresh_track_row_status(plex_rating_key, "failed")
        self._emit_progress()

    def _emit_progress(self):
        downloading = sum(
            1 for s in self._download_status.values() if s == "downloading"
        )
        total_active = sum(
            1 for s in self._download_status.values()
            if s in ("queued", "downloading")
        )
        self.download_progress.emit(
            len(self._downloaded_tracks),
            len(self._checked_tracks),
        )

    def _refresh_track_row_status(self, plex_rating_key: str, status: str):
        """Update the Status cell in the track table for this rating key."""
        if self._content.currentIndex() != 2:
            return
        # The checkbox widget stores the key via lambda capture; scan all rows
        for row in range(self._track_table.rowCount()):
            cb_widget = self._track_table.cellWidget(row, 0)
            if cb_widget is None:
                continue
            cb = cb_widget.findChild(QCheckBox)
            if cb is None:
                continue
            # We can't easily reverse-lookup key from cb; re-render whole table
            # is simpler, but to avoid flicker we find by title matching.
            # Instead, refresh by re-calling _show_tracks if we have the album.
            break
        # Simple approach: just update the status column by scanning
        # The rating key is stored implicitly in the checkbox toggle lambda.
        # Since we can't cheaply map row→key here, trigger a lightweight
        # re-render only of the status column via _update_all_status_cells.
        self._update_all_status_cells()

    def _update_all_status_cells(self):
        """Refresh the Status column for all rows in the track table."""
        if self._content.currentIndex() != 2:
            return
        tracks = self._current_album_tracks
        for row, track in enumerate(tracks):
            if row >= self._track_table.rowCount():
                break
            rating_key = str(track.ratingKey)
            status = self._download_status.get(rating_key, "")
            status_text, status_color = self._status_display(status)
            item = self._track_table.item(row, 5)
            if item is None:
                item = QTableWidgetItem()
                self._track_table.setItem(row, 5, item)
            item.setText(status_text)
            item.setForeground(QColor(status_color))

    # ── Playlist interaction ─────────────────────────────────────────────────

    def _on_playlist_item_clicked(self, item: QListWidgetItem):
        pl = item.data(Qt.ItemDataRole.UserRole)
        if pl is None:
            return
        pl_key = str(pl.ratingKey)
        currently_enabled = self._synced_playlists.get(pl_key, False)
        self._synced_playlists[pl_key] = not currently_enabled
        if not currently_enabled:
            # Toggling ON — add all tracks to selected keys and queue downloads
            self._enable_playlist(pl)
        else:
            # Toggling OFF — remove from synced list (tracks remain until sync diff)
            pass
        # Refresh the playlist view
        self._show_playlists()
        self._update_sync_button()

    def _enable_playlist(self, pl):
        """Add all tracks in a playlist to the download queue."""
        if self._plex_library is None:
            return
        try:
            tracks = pl.items()
        except Exception as exc:
            log.warning("Could not fetch playlist tracks: %s", exc)
            return
        for track in tracks:
            rating_key = str(track.ratingKey)
            self._checked_tracks[rating_key] = True
            if self._download_status.get(rating_key) not in ("downloading", "ready", "queued"):
                # We need the actual track object — store temporarily
                self._download_status[rating_key] = "queued"
                worker = _DownloadWorker(self._plex_library, track)
                worker.signals.started.connect(
                    lambda k=rating_key: self._on_download_started(k)
                )
                worker.signals.finished.connect(self._on_download_finished)
                worker.signals.failed.connect(self._on_download_failed)
                QThreadPool.globalInstance().start(worker)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _get_track_format(self, track) -> str:
        """Extract format string (FLAC, MP3, etc.) from Plex track media info."""
        try:
            for media in track.media:
                for part in media.parts:
                    container = (part.container or "").upper()
                    if container:
                        return container
            # Fallback: use codec
            codec = getattr(track.media[0], 'audioCodec', '') if track.media else ''
            return codec.upper() if codec else "?"
        except Exception:
            return "?"

    def _status_display(self, status: str) -> tuple[str, str]:
        """Return (display_text, color) for a download status string."""
        if status == "queued":
            return "Queued", Colors.TEXT_TERTIARY
        elif status == "downloading":
            return "Downloading…", Colors.ACCENT
        elif status == "ready":
            return "Ready", Colors.SUCCESS
        elif status == "failed":
            return "Failed", Colors.DANGER
        else:
            return "", Colors.TEXT_TERTIARY

    def _update_sync_button(self):
        """Enable Sync button only when tracks are ready and none are downloading."""
        has_ready = any(s == "ready" for s in self._download_status.values())
        any_downloading = any(s == "downloading" for s in self._download_status.values())
        self._sync_btn.setEnabled(has_ready and not any_downloading)

    def get_downloaded_tracks(self) -> dict[str, object]:
        """Return mapping of plex_rating_key → PCTrack for all downloaded tracks."""
        return dict(self._downloaded_tracks)

    def get_checked_keys(self) -> list[str]:
        """Return plex_rating_keys that the user has checked for sync."""
        return [k for k, v in self._checked_tracks.items() if v]

    def get_synced_playlist_keys(self) -> list[str]:
        """Return playlist ratingKeys enabled for sync."""
        return [k for k, v in self._synced_playlists.items() if v]
