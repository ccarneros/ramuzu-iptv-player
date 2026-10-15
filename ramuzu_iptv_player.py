#!/usr/bin/env python3
"""
Ramuzu IPTV Player
Author: ccarneros

Modified by ccarneros in 2026 from earlier GPL v2 code.
Distributed under the GNU GPL v2, see LICENSE.

A lightweight Xtream Codes / M3U player front-end. It only browses the
catalogue of a provider you supply and hands streams to an external player
such as VLC or mpv; it does not provide any content.
"""

import gzip
import hashlib
import html
import io
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from urllib.parse import parse_qs, quote, urlparse

import requests
from PyQt5.QtCore import (
    QCoreApplication, QEvent, QObject, QPointF, QRunnable, QSize,
    QSortFilterProxyModel, QStandardPaths, Qt, QThreadPool, QTimer, QUrl,
    QByteArray, pyqtSignal,
)
from PyQt5.QtGui import (
    QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QIcon, QKeySequence,
    QPainter, QPalette, QPixmap, QPolygonF, QStandardItem, QStandardItemModel,
)
from PyQt5.QtWidgets import (
    QAbstractItemView, QActionGroup, QApplication, QDialog,
    QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListView,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QProgressBar, QPushButton, QShortcut, QSizePolicy, QStackedWidget, QStyle,
    QStyledItemDelegate, QTabWidget, QTextBrowser, QToolButton, QToolTip,
    QVBoxLayout, QWidget,
)

try:
    import keyring
except ImportError:  # optional: without it passwords are simply not saved
    keyring = None


APP_NAME = "Ramuzu IPTV Player"
APP_VERSION = "5.0"
APP_AUTHOR = "ccarneros"

API_USER_AGENT = "okhttp/4.12.0"
PLAYER_USER_AGENT = "AppleCoreMedia/1.0.0.20L563 (Apple TV; U; CPU OS 16_5 like Mac OS X; en_us)"
WEB_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"

API_TIMEOUT = 15
EPG_TIMEOUT = 90
EPG_CACHE_TTL = 3600
MAX_IMAGE_BYTES = 5 * 1024 * 1024
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

PAYLOAD_ROLE = Qt.UserRole + 1
SUBTITLE_ROLE = Qt.UserRole + 2
FOLDER_ROLE = Qt.UserRole + 3
FAVORITE_ROLE = Qt.UserRole + 4

# Item kinds that can be favourited, and the field that identifies them.
FAVORITE_ID_FIELDS = {"live": "stream_id", "movie": "stream_id", "series": "series_id"}

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"


# ─── Paths & settings ────────────────────────────────────────────────────────

class Paths:
    data = Path.home() / ".ramuzu-iptv"
    cache = data / "cache"

    @classmethod
    def init(cls):
        """Resolve per-user folders (e.g. %APPDATA% and %LOCALAPPDATA% on Windows)."""
        data = QStandardPaths.writableLocation(QStandardPaths.AppDataLocation)
        cache = QStandardPaths.writableLocation(QStandardPaths.CacheLocation)
        if data:
            cls.data = Path(data)
        if cache:
            cls.cache = Path(cache)
        for folder in (cls.data, cls.cache / "images", cls.cache / "epg"):
            folder.mkdir(parents=True, exist_ok=True)


class Settings:
    """Small JSON settings file. Never holds passwords."""

    DEFAULTS = {
        "theme": "system",
        "font_size": 10,
        "use_post": False,
        "load_epg": False,
        "on_top": False,
        "player": "",
        "accounts": [],
        "favorites": {},
        "auto_connect": True,
        "last_account": "",
        "geometry": "",
    }

    def __init__(self, path):
        self.path = Path(path)
        self.values = dict(self.DEFAULTS)
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                self.values.update({k: v for k, v in stored.items() if k in self.DEFAULTS})
        except (OSError, ValueError):
            pass

    def __getitem__(self, key):
        return self.values[key]

    def __setitem__(self, key, value):
        self.values[key] = value
        self.save()

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.values, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            print(f"Could not save settings: {exc}")


# ─── Password storage (system keychain) ──────────────────────────────────────

def account_key(server, username):
    return f"{username}@{server}"


def store_password(key, password):
    if keyring is None:
        return False
    try:
        keyring.set_password(APP_NAME, key, password)
        return True
    except Exception as exc:
        print(f"Keychain unavailable: {exc}")
        return False


def load_password(key):
    if keyring is None:
        return ""
    try:
        return keyring.get_password(APP_NAME, key) or ""
    except Exception:
        return ""


def delete_password(key):
    if keyring is None:
        return
    try:
        keyring.delete_password(APP_NAME, key)
    except Exception:
        pass


# ─── Small helpers ───────────────────────────────────────────────────────────

SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SAFE_EXT = re.compile(r"^[A-Za-z0-9]{1,6}$")


def normalize_server(text):
    text = (text or "").strip().rstrip("/")
    if text and not re.match(r"^https?://", text, re.I):
        text = "http://" + text
    return text


def parse_m3u_url(url):
    """Return (server, username, password) from an Xtream get.php link, or None."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    query = parse_qs(parsed.query)
    username = query.get("username", [""])[0]
    password = query.get("password", [""])[0]
    if not username or not password:
        return None
    return f"{parsed.scheme}://{parsed.netloc}", username, password


def normalize_channel_name(name):
    name = re.sub(r"\s+", " ", (name or "").lower().strip())
    name = re.sub(r"[^\w\s]", "", name)
    name = re.sub(r"\b(hd|sd|fhd|uhd|channel|tv)\b", "", name)
    return name.strip()


def to_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def truncate(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def parse_xmltv_time(value):
    """Parse XMLTV timestamps such as '20260101203000 +0100'."""
    value = (value or "").strip()
    if len(value) < 14:
        return None
    try:
        offset = value[14:].strip()
        if offset:
            return datetime.strptime(f"{value[:14]} {offset}", "%Y%m%d%H%M%S %z")
        return datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def describe_error(exc):
    if isinstance(exc, LoginError):
        return str(exc)
    if isinstance(exc, requests.exceptions.SSLError):
        return "Secure connection failed: the server certificate is not valid."
    if isinstance(exc, requests.Timeout):
        return "The server took too long to respond."
    if isinstance(exc, requests.ConnectionError):
        return "Could not reach the server. Check the address and your connection."
    if isinstance(exc, requests.HTTPError):
        code = exc.response.status_code if exc.response is not None else "?"
        return f"The server returned an error ({code})."
    if isinstance(exc, ValueError):
        return "The server sent an unexpected response."
    return str(exc) or exc.__class__.__name__


class LoginError(Exception):
    pass


# ─── Networking (runs in worker threads) ─────────────────────────────────────

_thread_local = threading.local()


def web_session():
    session = getattr(_thread_local, "web", None)
    if session is None:
        session = requests.Session()
        session.headers["User-Agent"] = WEB_USER_AGENT
        _thread_local.web = session
    return session


class XtreamClient:
    """Minimal Xtream Codes API client. One requests.Session per thread."""

    def __init__(self, server, username, password, use_post=False):
        self.server = server
        self.username = username
        self.password = password
        self.use_post = use_post
        self._local = threading.local()

    @property
    def host(self):
        return urlparse(self.server).netloc or self.server

    @property
    def encrypted(self):
        return self.server.lower().startswith("https://")

    def _session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers["User-Agent"] = API_USER_AGENT
            self._local.session = session
        return session

    def _request(self, path, params, timeout=API_TIMEOUT):
        url = f"{self.server}/{path}"
        # With POST the credentials travel in the body instead of the URL.
        if self.use_post:
            response = self._session().post(url, data=params, timeout=timeout)
        else:
            response = self._session().get(url, params=params, timeout=timeout)
        response.raise_for_status()
        return response

    def api(self, action=None, **params):
        query = {"username": self.username, "password": self.password}
        if action:
            query["action"] = action
        query.update(params)
        return self._request("player_api.php", query).json()

    def epg_xml(self):
        params = {"username": self.username, "password": self.password}
        data = self._request("xmltv.php", params, timeout=EPG_TIMEOUT).content
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        return data

    def stream_url(self, kind, stream_id, extension):
        # Values come from the server: only allow plain ids/extensions so a
        # malicious provider cannot smuggle extra arguments to the player.
        stream_id = str(stream_id or "").strip()
        if kind not in ("live", "movie", "series") or not SAFE_ID.match(stream_id):
            return None
        extension = str(extension or "").strip()
        if not SAFE_EXT.match(extension):
            extension = "ts" if kind == "live" else "m3u8"
        user = quote(self.username, safe="")
        password = quote(self.password, safe="")
        return f"{self.server}/{kind}/{user}/{password}/{stream_id}.{extension}"


def login_job(client):
    info = client.api()
    user_info = info.get("user_info") if isinstance(info, dict) else None
    if not isinstance(user_info, dict) or str(user_info.get("auth", 1)) == "0":
        raise LoginError("Wrong username or password.")

    actions = {
        "live": "get_live_categories",
        "movie": "get_vod_categories",
        "series": "get_series_categories",
    }
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {kind: pool.submit(client.api, action) for kind, action in actions.items()}
        categories = {}
        for kind, future in futures.items():
            data = future.result()
            categories[kind] = [c for c in data if isinstance(c, dict)] if isinstance(data, list) else []
    return {"info": info, "categories": categories}


def load_epg_job(client):
    key = hashlib.sha1(f"{client.server}|{client.username}".encode()).hexdigest()
    cache = Paths.cache / "epg" / f"{key}.xml"
    data = None
    try:
        if cache.exists() and time.time() - cache.stat().st_mtime < EPG_CACHE_TTL:
            data = cache.read_bytes()
    except OSError:
        data = None
    if data is None:
        data = client.epg_xml()
        try:
            tmp = cache.with_suffix(".part")
            tmp.write_bytes(data)
            os.replace(tmp, cache)
        except OSError:
            pass
    return parse_epg(data)


def parse_epg(data):
    """Streaming XMLTV parse that keeps only programmes that have not ended yet."""
    now = datetime.now(timezone.utc)
    programmes = {}
    names = {}
    for _, elem in ET.iterparse(io.BytesIO(data), events=("end",)):
        if elem.tag == "channel":
            channel_id = (elem.get("id") or "").strip().lower()
            if channel_id:
                for display in elem.findall("display-name"):
                    if display.text:
                        names.setdefault(normalize_channel_name(display.text), channel_id)
            elem.clear()
        elif elem.tag == "programme":
            channel_id = (elem.get("channel") or "").strip().lower()
            stop = parse_xmltv_time(elem.get("stop"))
            if channel_id and stop and stop > now:
                start = parse_xmltv_time(elem.get("start"))
                if start:
                    title = (elem.findtext("title") or "").strip()
                    desc = (elem.findtext("desc") or "").strip()
                    programmes.setdefault(channel_id, []).append((start, stop, title, desc))
            elem.clear()
    for items in programmes.values():
        items.sort(key=lambda p: p[0])
    return programmes, names


def cached_image(url):
    """Download an artwork image once (TLS verified, size-capped) and return its local path."""
    if not url or not str(url).lower().startswith(("http://", "https://")):
        return ""
    url = str(url)
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    if ext not in IMAGE_EXTENSIONS:
        ext = ".jpg"
    path = Paths.cache / "images" / (hashlib.sha1(url.encode()).hexdigest() + ext)
    if path.exists():
        return str(path)
    try:
        with web_session().get(url, timeout=10, stream=True) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_content(65536):
                data += chunk
                if len(data) > MAX_IMAGE_BYTES:
                    return ""
        tmp = path.with_suffix(ext + ".part")
        tmp.write_bytes(bytes(data))
        os.replace(tmp, path)
        return str(path)
    except (requests.RequestException, OSError):
        return ""


_TMDB_OVERVIEW = [
    re.compile(r'<div class="overview"[^>]*>\s*<p[^>]*>(.*?)</p>', re.S),
    re.compile(r'<meta name="description" content="(.*?)"', re.S),
]


def tmdb_overview(tmdb_id, kind):
    """Fallback synopsis scraped from themoviedb.org when the provider has none."""
    if not str(tmdb_id or "").isdigit():
        return ""
    for path in (["movie", "tv"] if kind == "movie" else ["tv", "movie"]):
        try:
            response = web_session().get(f"https://www.themoviedb.org/{path}/{tmdb_id}", timeout=10)
            if response.status_code == 404:
                continue
            response.raise_for_status()
        except requests.RequestException:
            return ""
        for pattern in _TMDB_OVERVIEW:
            match = pattern.search(response.text)
            if match:
                return html.unescape(re.sub(r"<[^>]+>", "", match.group(1))).strip()
        return ""
    return ""


def fetch_details(client, kind, entry):
    """Collect the info shown in a movie / series / episode tooltip."""
    info = {}
    try:
        if kind == "movie":
            data = client.api("get_vod_info", vod_id=entry.get("stream_id"))
            if isinstance(data, dict) and isinstance(data.get("info"), dict):
                info = data["info"]
        elif kind == "series":
            info = entry
        elif kind == "episode" and isinstance(entry.get("info"), dict):
            info = entry["info"]
    except (requests.RequestException, ValueError):
        info = {}

    series = entry.get("_series") or {}
    if kind == "episode":
        title = entry.get("_label") or entry.get("title") or "Episode"
    else:
        title = info.get("name") or entry.get("name") or "Untitled"

    plot = info.get("plot") or info.get("description") or entry.get("plot") or ""
    if not plot:
        tmdb_id = info.get("tmdb_id") or entry.get("tmdb") or entry.get("tmdb_id") or series.get("tmdb")
        plot = tmdb_overview(tmdb_id, "movie" if kind == "movie" else "tv")

    release = str(info.get("releasedate") or info.get("releaseDate") or info.get("release_date") or "")
    rating = info.get("rating") or entry.get("rating") or ""
    try:
        rating = f"{float(rating):.1f}" if float(rating) > 0 else ""
    except (TypeError, ValueError):
        rating = ""

    duration = str(info.get("duration") or "")
    match = re.match(r"^(\d+):(\d{2}):\d{2}$", duration)
    if match:
        hours, minutes = int(match.group(1)), int(match.group(2))
        duration = f"{hours}h {minutes:02d}m" if hours else f"{minutes} min"

    poster = (info.get("movie_image") or info.get("cover_big") or info.get("cover")
              or entry.get("stream_icon") or entry.get("cover") or series.get("cover"))
    return {
        "title": str(title),
        "plot": str(plot),
        "year": release[:4] if release[:4].isdigit() else "",
        "genre": str(info.get("genre") or ""),
        "rating": rating,
        "duration": duration,
        "cast": str(info.get("cast") or info.get("actors") or ""),
        "director": str(info.get("director") or ""),
        "image": cached_image(poster),
    }


# ─── External player ─────────────────────────────────────────────────────────

def detect_player():
    candidates = []
    if IS_WINDOWS:
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if base:
                candidates += [Path(base) / "VideoLAN" / "VLC" / "vlc.exe", Path(base) / "mpv" / "mpv.exe"]
    elif IS_MAC:
        candidates += [Path("/Applications/VLC.app/Contents/MacOS/VLC"),
                       Path("/Applications/mpv.app/Contents/MacOS/mpv")]
    for name in ("vlc", "mpv", "smplayer"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return ""


def resolve_mac_app(path):
    """Turn 'Foo.app' into the executable inside the bundle."""
    bundle = Path(path)
    try:
        with open(bundle / "Contents" / "Info.plist", "rb") as fh:
            executable = plistlib.load(fh).get("CFBundleExecutable")
        if executable and (bundle / "Contents" / "MacOS" / executable).is_file():
            return str(bundle / "Contents" / "MacOS" / executable)
    except (OSError, ValueError):
        pass
    return ""


def player_command(player, url, title):
    name = Path(player).stem.lower()
    if "vlc" in name:
        return [player, url, f":http-user-agent={PLAYER_USER_AGENT}", f":meta-title={title}"]
    if "mpv" in name:
        return [player, f"--user-agent={PLAYER_USER_AGENT}", f"--force-media-title={title}", "--", url]
    return [player, url]


# ─── Theme ───────────────────────────────────────────────────────────────────

THEMES = {
    "light": {
        "bg": "#F5F5F7", "surface": "#FFFFFF", "field": "#FFFFFF", "border": "#E3E3E8",
        "scroll": "#D2D2D9", "text": "#1D1D22", "muted": "#71717C", "accent": "#2F6FEB",
        "accent_hover": "#2560D4", "accent_text": "#FFFFFF", "selection": "#E6EEFE",
        "hover": "#F0F0F4", "ok": "#1F9D55", "warn": "#B5650D",
    },
    "dark": {
        "bg": "#121215", "surface": "#1A1A1E", "field": "#202025", "border": "#2D2D34",
        "scroll": "#3A3A42", "text": "#E8E8EC", "muted": "#8D8D98", "accent": "#5B8DEF",
        "accent_hover": "#6E9BF2", "accent_text": "#FFFFFF", "selection": "#243150",
        "hover": "#24242A", "ok": "#3CC47C", "warn": "#E3A04F",
    },
}

QSS = Template("""
QMainWindow, QDialog { background: $bg; }
QWidget { color: $text; }
#TopBar { background: $surface; border-bottom: 1px solid $border; }
QLabel#Muted, QLabel#EmptyState { color: $muted; }
QLabel#AccountName { font-weight: 600; }
QLabel#Warning { color: $warn; }
QLabel#Dot { color: $ok; }
QLineEdit { background: $field; border: 1px solid $border; border-radius: 7px; padding: 6px 10px;
            selection-background-color: $accent; selection-color: $accent_text; }
QLineEdit:focus { border: 1px solid $accent; }
QLineEdit:disabled { background: $bg; color: $muted; }
QPushButton { background: $field; border: 1px solid $border; border-radius: 7px; padding: 6px 14px; }
QPushButton:hover { background: $hover; }
QPushButton:pressed { background: $border; }
QPushButton:disabled { color: $muted; }
QPushButton#Primary { background: $accent; border-color: $accent; color: $accent_text; font-weight: 600; }
QPushButton#Primary:hover { background: $accent_hover; border-color: $accent_hover; }
QPushButton#Primary:disabled { background: $border; border-color: $border; color: $muted; }
QToolButton { background: transparent; border: none; border-radius: 7px; padding: 4px 9px; }
QToolButton:hover { background: $hover; }
QToolButton:disabled { color: $scroll; }
QToolButton::menu-indicator { image: none; width: 0px; }
QTabWidget::pane { border: none; }
QTabBar::tab { background: transparent; border: none; border-bottom: 2px solid transparent;
               color: $muted; padding: 8px 2px; margin-right: 22px; }
QTabBar::tab:hover { color: $text; }
QTabBar::tab:selected { color: $text; border-bottom: 2px solid $accent; }
QListView, QListWidget, QTextBrowser { background: $surface; border: 1px solid $border; border-radius: 10px; }
QListView, QListWidget { padding: 4px 0px; }
QTextBrowser { padding: 14px; }
QScrollBar:vertical { background: transparent; width: 12px; margin: 6px 3px 6px 3px; }
QScrollBar::handle:vertical { background: $scroll; border-radius: 3px; min-height: 36px; }
QScrollBar:horizontal { background: transparent; height: 12px; margin: 3px 6px 3px 6px; }
QScrollBar::handle:horizontal { background: $scroll; border-radius: 3px; min-width: 36px; }
QScrollBar::handle:hover { background: $muted; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0px; height: 0px; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QMenu { background: $surface; border: 1px solid $border; padding: 6px; }
QMenu::item { padding: 6px 24px 6px 26px; border-radius: 6px; }
QMenu::item:selected { background: $hover; color: $text; }
QMenu::item:disabled { color: $muted; }
QMenu::separator { height: 1px; background: $border; margin: 5px 8px; }
QToolTip { background: $surface; color: $text; border: 1px solid $border; padding: 8px; }
QStatusBar { background: $bg; color: $muted; }
QStatusBar::item { border: none; }
QStatusBar QLabel { color: $muted; }
QProgressBar { background: $hover; border: none; border-radius: 2px; }
QProgressBar::chunk { background: $accent; border-radius: 2px; }
""")


def make_palette(t):
    palette = QPalette()
    roles = {
        QPalette.Window: t["bg"], QPalette.WindowText: t["text"], QPalette.Base: t["field"],
        QPalette.AlternateBase: t["hover"], QPalette.Text: t["text"], QPalette.Button: t["field"],
        QPalette.ButtonText: t["text"], QPalette.Highlight: t["accent"],
        QPalette.HighlightedText: t["accent_text"], QPalette.ToolTipBase: t["surface"],
        QPalette.ToolTipText: t["text"], QPalette.Link: t["accent"], QPalette.Light: t["surface"],
        QPalette.Midlight: t["border"], QPalette.Mid: t["border"], QPalette.Dark: t["scroll"],
        QPalette.Shadow: t["scroll"], QPalette.BrightText: t["accent_text"],
    }
    if hasattr(QPalette, "PlaceholderText"):
        roles[QPalette.PlaceholderText] = t["muted"]
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, QColor(t["muted"]))
    return palette


def system_prefers_dark():
    if IS_WINDOWS:
        try:
            import winreg
            key = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
                return winreg.QueryValueEx(handle, "AppsUseLightTheme")[0] == 0
        except OSError:
            return False
    return QApplication.palette().color(QPalette.Window).lightness() < 128


def set_dark_title_bar(widget, dark):
    """Match the native Windows 10/11 title bar to the theme."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        value = ctypes.c_int(1 if dark else 0)
        hwnd = int(widget.winId())
        for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new / old builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass


def make_app_icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#2F6FEB"))
    painter.drawRoundedRect(4, 4, 56, 56, 14, 14)
    painter.setBrush(QColor("#FFFFFF"))
    painter.drawPolygon(QPolygonF([QPointF(26, 20), QPointF(26, 44), QPointF(46, 32)]))
    painter.end()
    return QIcon(pixmap)


def make_menu_icon(color):
    pixmap = QPixmap(40, 40)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(color))
    for x in (9, 20, 31):
        painter.drawEllipse(QPointF(x, 20), 2.8, 2.8)
    painter.end()
    return QIcon(pixmap)


# ─── Background tasks ────────────────────────────────────────────────────────

class _TaskSignals(QObject):
    done = pyqtSignal(object)
    failed = pyqtSignal(object)


class Task(QRunnable):
    def __init__(self, fn, args):
        super().__init__()
        self.fn = fn
        self.args = args
        self.signals = _TaskSignals()

    def run(self):
        try:
            result = self.fn(*self.args)
        except Exception as exc:
            self.signals.failed.emit(exc)
            return
        self.signals.done.emit(result)


# ─── List widgets ────────────────────────────────────────────────────────────

class _Ref:
    """Keeps a Python object as-is inside a Qt item (PyQt would turn a dict into a copy)."""
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


def item_payload(item):
    """Payload stored on a QStandardItem / QListWidgetItem / QModelIndex."""
    ref = item.data(PAYLOAD_ROLE)
    return ref.value if isinstance(ref, _Ref) else ref


def favorite_id(payload):
    """Stable id such as 'live:123' for items that can be favourited, else None."""
    if not isinstance(payload, dict):
        return None
    kind = payload.get("_kind")
    value = payload.get(FAVORITE_ID_FIELDS.get(kind, ""))
    return f"{kind}:{value}" if value not in (None, "") else None


class ItemDelegate(QStyledItemDelegate):
    """Flat rows with rounded hover/selection, optional muted subtitle and chevron."""

    def __init__(self, theme, parent=None):
        super().__init__(parent)
        self.theme = theme

    def sizeHint(self, option, index):
        return QSize(100, QFontMetrics(option.font).height() + 16)

    def paint(self, painter, option, index):
        t = self.theme()
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        rect = option.rect.adjusted(6, 1, -6, -1)

        if option.state & QStyle.State_Selected:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(t["selection"]))
            painter.drawRoundedRect(rect, 6, 6)
        elif option.state & QStyle.State_MouseOver:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(t["hover"]))
            painter.drawRoundedRect(rect, 6, 6)

        metrics = QFontMetrics(option.font)
        painter.setFont(option.font)
        text_rect = rect.adjusted(12, 0, -12, 0)

        if index.data(FOLDER_ROLE):
            painter.setPen(QColor(t["muted"]))
            chevron_width = metrics.horizontalAdvance("›")
            painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignRight, "›")
            text_rect.setRight(text_rect.right() - chevron_width - 10)

        if index.data(FAVORITE_ROLE):
            painter.setPen(QColor(t["accent"]))
            star_width = metrics.horizontalAdvance("★")
            painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignRight, "★")
            text_rect.setRight(text_rect.right() - star_width - 10)

        subtitle = index.data(SUBTITLE_ROLE)
        if subtitle:
            subtitle = metrics.elidedText(subtitle, Qt.ElideRight, int(text_rect.width() * 0.45))
            painter.setPen(QColor(t["muted"]))
            painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignRight, subtitle)
            text_rect.setRight(text_rect.right() - metrics.horizontalAdvance(subtitle) - 16)

        painter.setPen(QColor(t["text"]))
        text = metrics.elidedText(index.data(Qt.DisplayRole) or "", Qt.ElideRight, text_rect.width())
        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, text)
        painter.restore()


class BrowserPane(QWidget):
    """One catalogue tab: breadcrumb, search and a fast filterable list with a level stack."""

    activated = pyqtSignal(object)
    context_requested = pyqtSignal(object, object)  # payload, global position

    def __init__(self, kind, title, theme, parent=None):
        super().__init__(parent)
        self.kind = kind
        self.root_title = title
        self.theme = theme
        self.levels = []
        self.busy = False

        self.back_button = QToolButton()
        self.back_button.setText("←")
        self.back_button.setToolTip("Back (Backspace)")
        self.back_button.clicked.connect(self.pop)

        self.crumbs = QLabel()
        self.crumbs.setTextFormat(Qt.RichText)
        self.crumbs.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

        self.search = QLineEdit()
        self.search.setPlaceholderText(f"Search {title.lower()}")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(160)
        self.search.setMaximumWidth(280)
        self.search.installEventFilter(self)
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(120)
        self._filter_timer.timeout.connect(self._apply_filter)
        self.search.textChanged.connect(lambda _: self._filter_timer.start())

        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self._empty_model = QStandardItemModel(self)

        self.view = QListView()
        self.view.setModel(self.proxy)
        self.view.setItemDelegate(ItemDelegate(theme, self.view))
        self.view.setUniformItemSizes(True)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setMouseTracking(True)
        self.view.viewport().setAttribute(Qt.WA_Hover)
        self.view.activated.connect(self._on_activated)
        self.view.installEventFilter(self)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._on_context_menu)

        self.empty = QLabel()
        self.empty.setObjectName("EmptyState")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)

        self.body = QStackedWidget()
        self.body.addWidget(self.view)
        self.body.addWidget(self.empty)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        header.addWidget(self.back_button)
        header.addWidget(self.crumbs, 1)
        header.addWidget(self.search)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 12, 0, 0)
        layout.setSpacing(10)
        layout.addLayout(header)
        layout.addWidget(self.body, 1)

        self.show_message("Connect to a server to start browsing.")

    # navigation
    def show_message(self, text):
        self.levels = []
        self.proxy.setSourceModel(self._empty_model)
        self.empty.setText(text)
        self.body.setCurrentWidget(self.empty)
        self._update_header()

    def set_root(self, items, keep_view=False):
        """Replace the top level. With keep_view, stay where the user is."""
        if keep_view and len(self.levels) > 1:
            self.levels[0]["model"] = self._build_model(items)
            return
        position = {}
        if keep_view and self.levels:
            self._remember_position()
            position = {k: self.levels[0][k] for k in ("scroll", "row")}
        self.levels = []
        self._push(self.root_title, items, **position)

    def push(self, title, items):
        if self.levels:
            self._remember_position()
        self._push(title, items)

    def pop(self):
        if len(self.levels) > 1:
            self.levels.pop()
            self._show_current()

    def _push(self, title, items, scroll=0, row=None):
        self.levels.append({"title": title, "model": self._build_model(items), "scroll": scroll, "row": row})
        self._show_current()

    @staticmethod
    def _build_model(items):
        """items: (text, payload, subtitle, is_folder[, is_favorite]) tuples."""
        model = QStandardItemModel()
        rows = []
        for text, payload, subtitle, folder, *extra in items:
            item = QStandardItem(text)
            item.setEditable(False)
            item.setData(_Ref(payload), PAYLOAD_ROLE)
            if subtitle:
                item.setData(subtitle, SUBTITLE_ROLE)
            if folder:
                item.setData(True, FOLDER_ROLE)
            if extra and extra[0]:
                item.setData(True, FAVORITE_ROLE)
            rows.append(item)
        if rows:
            model.invisibleRootItem().appendRows(rows)
        return model

    def _remember_position(self):
        level = self.levels[-1]
        level["scroll"] = self.view.verticalScrollBar().value()
        current = self.view.currentIndex()
        level["row"] = self.proxy.mapToSource(current).row() if current.isValid() else None

    def _show_current(self):
        level = self.levels[-1]
        self._filter_timer.stop()
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.proxy.setFilterFixedString("")
        self.proxy.setSourceModel(level["model"])
        self._update_header()
        self._update_empty()
        row = level.get("row")
        if row is not None and row < level["model"].rowCount():
            self.view.setCurrentIndex(self.proxy.mapFromSource(level["model"].index(row, 0)))
        scroll = level["scroll"]
        QTimer.singleShot(0, lambda: self.view.verticalScrollBar().setValue(scroll))

    def _update_header(self):
        muted = self.theme()["muted"]
        titles = [level["title"] for level in self.levels] or [self.root_title]
        parts = [f'<span style="color:{muted}">{html.escape(t)}</span>' for t in titles[:-1]]
        parts.append(f"<b>{html.escape(titles[-1])}</b>")
        self.crumbs.setText(f'<span style="color:{muted}">&nbsp;&nbsp;/&nbsp;&nbsp;</span>'.join(parts))
        self.back_button.setEnabled(len(self.levels) > 1)
        self.search.setEnabled(bool(self.levels))

    def _update_empty(self):
        if not self.levels:
            return
        if self.proxy.rowCount():
            self.body.setCurrentWidget(self.view)
        else:
            self.empty.setText("No results." if self.search.text().strip() else "Nothing here.")
            self.body.setCurrentWidget(self.empty)

    def _apply_filter(self):
        self.proxy.setFilterFixedString(self.search.text().strip())
        self._update_empty()

    def _on_activated(self, index):
        if self.busy:
            return
        payload = item_payload(index)
        if payload is not None:
            self.activated.emit(payload)

    def _on_context_menu(self, pos):
        index = self.view.indexAt(pos)
        if index.isValid():
            self.view.setCurrentIndex(index)
            self.context_requested.emit(item_payload(index), self.view.viewport().mapToGlobal(pos))

    def current_payload(self):
        index = self.view.currentIndex()
        return item_payload(index) if index.isValid() and self.body.currentWidget() is self.view else None

    # helpers for the window
    def refresh_theme(self):
        self._update_header()
        self.view.viewport().update()

    def set_busy(self, busy):
        self.busy = busy
        if busy:
            self.view.viewport().setCursor(Qt.BusyCursor)
        else:
            self.view.viewport().unsetCursor()

    def payload_at(self, pos):
        index = self.view.indexAt(pos)
        return item_payload(index) if index.isValid() else None

    def current_model(self):
        return self.levels[-1]["model"] if self.levels else None

    def models(self):
        return [level["model"] for level in self.levels]

    def focus_search(self):
        if self.search.isEnabled():
            self.search.setFocus()
            self.search.selectAll()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress:
            key = event.key()
            if obj is self.view:
                if key == Qt.Key_Backspace or (key == Qt.Key_Left and event.modifiers() & Qt.AltModifier):
                    self.pop()
                    return True
                text = event.text()
                if text and text.isprintable() and not event.modifiers() & (Qt.ControlModifier | Qt.AltModifier):
                    self.search.setFocus()
                    self.search.setText(self.search.text() + text)
                    return True
            elif obj is self.search:
                if key == Qt.Key_Escape and self.search.text():
                    self.search.clear()
                    return True
                if key in (Qt.Key_Down, Qt.Key_Return, Qt.Key_Enter) and self.proxy.rowCount():
                    self.view.setFocus()
                    if not self.view.currentIndex().isValid():
                        self.view.setCurrentIndex(self.proxy.index(0, 0))
                    return True
        return super().eventFilter(obj, event)


# ─── Dialogs ─────────────────────────────────────────────────────────────────

class AccountDialog(QDialog):
    def __init__(self, parent, name="", server="", username="", password=""):
        super().__init__(parent)
        self.setWindowTitle("Save account")
        self.setMinimumWidth(420)

        self.name_edit = QLineEdit(name)
        self.server_edit = QLineEdit(server)
        self.server_edit.setPlaceholderText("http://host:port or M3U link")
        self.user_edit = QLineEdit(username)
        self.pass_edit = QLineEdit(password)
        self.pass_edit.setEchoMode(QLineEdit.Password)
        self.server_edit.textChanged.connect(self._maybe_split_m3u)

        form = QFormLayout()
        form.setSpacing(10)
        form.addRow("Name", self.name_edit)
        form.addRow("Server", self.server_edit)
        form.addRow("Username", self.user_edit)
        form.addRow("Password", self.pass_edit)

        hint = QLabel("Tip: paste an M3U link into Server to fill in everything at once.")
        if keyring is None:
            hint.setText("Secure storage is not available, so the password will not be saved.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)

        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        save = QPushButton("Save")
        save.setObjectName("Primary")
        save.setDefault(True)
        save.clicked.connect(self._validate)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(save)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addLayout(buttons)

    def _maybe_split_m3u(self, text):
        parsed = parse_m3u_url(text)
        if parsed and ("get.php" in text or "username=" in text):
            server, username, password = parsed
            self.server_edit.blockSignals(True)
            self.server_edit.setText(server)
            self.server_edit.blockSignals(False)
            self.user_edit.setText(username)
            self.pass_edit.setText(password)
            if not self.name_edit.text().strip():
                self.name_edit.setText(urlparse(server).hostname or "")

    def _validate(self):
        if not all(w.text().strip() for w in (self.name_edit, self.server_edit, self.user_edit, self.pass_edit)):
            QMessageBox.warning(self, "Save account", "Please fill in all fields.")
            return
        self.accept()

    def values(self):
        return (self.name_edit.text().strip(), normalize_server(self.server_edit.text()),
                self.user_edit.text().strip(), self.pass_edit.text().strip())


class AccountsDialog(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.setWindowTitle("Saved accounts")
        self.setMinimumSize(460, 340)

        self.list = QListWidget()
        self.list.setItemDelegate(ItemDelegate(lambda: window.theme, self.list))
        self.list.setMouseTracking(True)
        self.list.viewport().setAttribute(Qt.WA_Hover)
        self.list.itemActivated.connect(lambda _: self._connect())

        add = QPushButton("Add…")
        add.clicked.connect(self._add)
        self.remove_button = QPushButton("Remove")
        self.remove_button.clicked.connect(self._remove)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        self.connect_button = QPushButton("Connect")
        self.connect_button.setObjectName("Primary")
        self.connect_button.setDefault(True)
        self.connect_button.clicked.connect(self._connect)

        buttons = QHBoxLayout()
        buttons.addWidget(add)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        buttons.addWidget(close)
        buttons.addWidget(self.connect_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons)
        self._refresh()

    def _refresh(self):
        self.list.clear()
        for account in self.window.settings["accounts"]:
            item = QListWidgetItem(account["name"])
            item.setData(PAYLOAD_ROLE, _Ref(account))
            item.setData(SUBTITLE_ROLE, f'{account["username"]} · {urlparse(account["server"]).netloc}')
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)
        has_items = self.list.count() > 0
        self.remove_button.setEnabled(has_items)
        self.connect_button.setEnabled(has_items)

    def _selected(self):
        item = self.list.currentItem()
        return item_payload(item) if item else None

    def _add(self):
        w = self.window
        server = normalize_server(w.server_edit.text())
        dialog = AccountDialog(self, urlparse(server).hostname or "", server,
                               w.user_edit.text().strip(), w.pass_edit.text().strip())
        if dialog.exec_() == QDialog.Accepted:
            w.save_account(*dialog.values())
            self._refresh()

    def _remove(self):
        account = self._selected()
        if account and QMessageBox.question(
                self, "Remove account", f'Remove "{account["name"]}" and its saved password?') == QMessageBox.Yes:
            self.window.remove_account(account["name"])
            self._refresh()

    def _connect(self):
        account = self._selected()
        if account:
            self.accept()
            self.window.connect_saved(account)


# ─── Main window ─────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self, settings, system_dark):
        super().__init__()
        self.settings = settings
        self.system_dark = system_dark
        self.theme = THEMES["light"]
        self.client = None
        self.account_info = None
        self.epg = {}
        self.epg_names = {}
        self.generation = 0
        self._busy = 0
        self._signals = set()
        self._detected_player = None

        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(6)

        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(640, 440)
        self.resize(940, 640)

        self._build_ui()
        self._build_menu()
        self.apply_theme()
        self.apply_font_size(self.settings["font_size"])

        geometry = self.settings["geometry"]
        if geometry:
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode()))
        if self.settings["on_top"]:
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)

        self.epg_timer = QTimer(self)
        self.epg_timer.setInterval(60_000)
        self.epg_timer.timeout.connect(self._refresh_live_subtitles)
        self.epg_timer.start()

        # Connect to the last used saved account once the window is on screen.
        QTimer.singleShot(0, self._auto_connect)

    # ── UI construction
    def _build_ui(self):
        top = QWidget()
        top.setObjectName("TopBar")
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(16, 12, 12, 12)
        top_layout.setSpacing(8)

        self.server_edit = QLineEdit()
        self.server_edit.setPlaceholderText("Server (http://host:port) or M3U link")
        self.server_edit.textChanged.connect(self._maybe_split_m3u)
        self.user_edit = QLineEdit()
        self.user_edit.setPlaceholderText("Username")
        self.pass_edit = QLineEdit()
        self.pass_edit.setPlaceholderText("Password")
        self.pass_edit.setEchoMode(QLineEdit.Password)
        for edit in (self.server_edit, self.user_edit, self.pass_edit):
            edit.returnPressed.connect(self.login)
        self.connect_button = QPushButton("Connect")
        self.connect_button.setObjectName("Primary")
        self.connect_button.clicked.connect(self.login)

        login_page = QWidget()
        login_layout = QHBoxLayout(login_page)
        login_layout.setContentsMargins(0, 0, 0, 0)
        login_layout.setSpacing(8)
        login_layout.addWidget(self.server_edit, 4)
        login_layout.addWidget(self.user_edit, 2)
        login_layout.addWidget(self.pass_edit, 2)
        login_layout.addWidget(self.connect_button)

        self.account_dot = QLabel("●")
        self.account_dot.setObjectName("Dot")
        self.account_label = QLabel()
        self.account_label.setObjectName("AccountName")
        self.account_host = QLabel()
        self.account_host.setObjectName("Muted")
        self.security_label = QLabel("Not encrypted (HTTP)")
        self.security_label.setObjectName("Warning")
        self.security_label.setToolTip(
            "This provider only offers plain HTTP: your username and password travel "
            "unencrypted. Avoid public Wi-Fi or use HTTPS if your provider supports it.")
        switch_button = QPushButton("Switch account")
        switch_button.clicked.connect(self.show_login)

        account_page = QWidget()
        account_layout = QHBoxLayout(account_page)
        account_layout.setContentsMargins(4, 0, 0, 0)
        account_layout.setSpacing(8)
        account_layout.addWidget(self.account_dot)
        account_layout.addWidget(self.account_label)
        account_layout.addWidget(self.account_host)
        account_layout.addSpacing(8)
        account_layout.addWidget(self.security_label)
        account_layout.addStretch(1)
        account_layout.addWidget(switch_button)

        self.top_stack = QStackedWidget()
        self.top_stack.addWidget(login_page)
        self.top_stack.addWidget(account_page)

        self.menu_button = QToolButton()
        self.menu_button.setIconSize(QSize(20, 20))
        self.menu_button.setToolTip("Menu")
        self.menu_button.setPopupMode(QToolButton.InstantPopup)

        top_layout.addWidget(self.top_stack, 1)
        top_layout.addWidget(self.menu_button)

        self.panes = {
            "favorites": BrowserPane("favorites", "Favorites", lambda: self.theme),
            "live": BrowserPane("live", "Live TV", lambda: self.theme),
            "movie": BrowserPane("movie", "Movies", lambda: self.theme),
            "series": BrowserPane("series", "Series", lambda: self.theme),
        }
        self.panes["favorites"].show_message("Connect to a server to see your favorites.")
        self._favorite_ids = set()
        self._viewports = {}
        self.tabs = QTabWidget()
        self.tabs.tabBar().setDrawBase(False)
        for pane in self.panes.values():
            pane.activated.connect(lambda payload, p=pane: self.on_activated(p, payload))
            pane.context_requested.connect(lambda payload, pos, p=pane: self._show_item_menu(p, payload, pos))
            pane.view.viewport().installEventFilter(self)
            self._viewports[pane.view.viewport()] = pane
            title = "★  Favorites" if pane.kind == "favorites" else pane.root_title
            self.tabs.addTab(pane, title)
        QShortcut(QKeySequence.Delete, self.panes["favorites"].view,
                  activated=self._remove_current_favorite, context=Qt.WidgetShortcut)

        self.account_view = QTextBrowser()
        self.account_view.setOpenLinks(False)
        account_tab = QWidget()
        account_tab_layout = QVBoxLayout(account_tab)
        account_tab_layout.setContentsMargins(0, 12, 0, 0)
        account_tab_layout.addWidget(self.account_view)
        self.tabs.addTab(account_tab, "Account")

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(16, 6, 16, 8)
        content_layout.addWidget(self.tabs)

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(top)
        central_layout.addWidget(content, 1)
        self.setCentralWidget(central)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedSize(110, 4)
        self.progress.hide()
        status = self.statusBar()
        status.setSizeGripEnabled(False)
        status.setContentsMargins(0, 0, 12, 4)
        self.status_label = QLabel()
        self.status_label.setContentsMargins(14, 0, 0, 0)
        status.addWidget(self.status_label, 1)
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self.status_label.clear)
        status.addPermanentWidget(self.progress)

        QShortcut(QKeySequence.Find, self, activated=self._focus_search)
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self.show_login)
        QShortcut(QKeySequence("Ctrl+D"), self, activated=self._toggle_current_favorite)

    def _build_menu(self):
        menu = QMenu(self)
        menu.addAction("Saved accounts…", self.open_accounts, QKeySequence("Ctrl+O"))
        self.save_account_action = menu.addAction("Save this account…", self.save_current_account)
        menu.addAction("Open M3U link…", self.open_m3u_link)
        menu.addSeparator()

        self.player_action = menu.addAction("", self.choose_player)
        self._update_player_action()

        theme_menu = menu.addMenu("Theme")
        group = QActionGroup(self)
        for key, label in (("system", "System"), ("light", "Light"), ("dark", "Dark")):
            action = theme_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(self.settings["theme"] == key)
            action.triggered.connect(lambda _, k=key: self.set_theme(k))
            group.addAction(action)

        size_menu = menu.addMenu("Text size")
        group = QActionGroup(self)
        for size, label in ((9, "Small"), (10, "Normal"), (12, "Large"), (14, "Extra large")):
            action = size_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(self.settings["font_size"] == size)
            action.triggered.connect(lambda _, s=size: self.apply_font_size(s, save=True))
            group.addAction(action)

        menu.addSeparator()
        self._add_toggle(menu, "Load TV guide (EPG)", "load_epg", self._on_epg_toggled)
        self._add_toggle(menu, "Send requests with POST", "use_post", self._on_post_toggled)
        self._add_toggle(menu, "Always on top", "on_top", self._on_top_toggled)
        self._add_toggle(menu, "Connect on startup", "auto_connect",
                         lambda enabled: self.settings.__setitem__("auto_connect", enabled))
        menu.addSeparator()
        menu.addAction("Clear image and guide cache", self.clear_cache)
        menu.addAction(f"About {APP_NAME}", self.show_about)
        self.menu_button.setMenu(menu)

    def _add_toggle(self, menu, label, key, handler):
        action = menu.addAction(label)
        action.setCheckable(True)
        action.setChecked(bool(self.settings[key]))
        action.toggled.connect(handler)
        return action

    # ── Theme / font
    def set_theme(self, key):
        self.settings["theme"] = key
        self.apply_theme()

    def apply_theme(self):
        mode = self.settings["theme"]
        dark = mode == "dark" or (mode == "system" and self.system_dark)
        self.theme = THEMES["dark" if dark else "light"]
        app = QApplication.instance()
        app.setPalette(make_palette(self.theme))
        app.setStyleSheet(QSS.substitute(self.theme))
        set_dark_title_bar(self, dark)
        self.menu_button.setIcon(make_menu_icon(self.theme["text"]))
        for pane in self.panes.values():
            pane.refresh_theme()
        self._render_account()

    def apply_font_size(self, size, save=False):
        font = QApplication.instance().font()
        font.setPointSize(size)
        QApplication.instance().setFont(font)
        for widget in [p.view for p in self.panes.values()] + [self.account_view]:
            widget.setFont(font)
        if save:
            self.settings["font_size"] = size

    # ── Busy indicator / status
    def notify(self, text, timeout=6000):
        self.status_label.setText(text)
        if timeout:
            self._status_timer.start(timeout)
        else:
            self._status_timer.stop()

    def _set_busy(self, delta):
        self._busy = max(0, self._busy + delta)
        self.progress.setVisible(self._busy > 0)

    def run_task(self, fn, *args, on_done=None, on_error=None, busy=True):
        task = Task(fn, args)
        signals = task.signals
        self._signals.add(signals)

        def finish():
            self._signals.discard(signals)
            if busy:
                self._set_busy(-1)

        def done(result):
            finish()
            if on_done:
                on_done(result)

        def failed(exc):
            finish()
            if on_error:
                on_error(exc)
            else:
                self.notify(describe_error(exc), 10000)

        signals.done.connect(done)
        signals.failed.connect(failed)
        if busy:
            self._set_busy(1)
        self.pool.start(task)

    # ── Login
    def _maybe_split_m3u(self, text):
        if "get.php" not in text and "username=" not in text:
            return
        parsed = parse_m3u_url(text)
        if parsed:
            server, username, password = parsed
            self.server_edit.blockSignals(True)
            self.server_edit.setText(server)
            self.server_edit.blockSignals(False)
            self.user_edit.setText(username)
            self.pass_edit.setText(password)

    def open_m3u_link(self):
        text, ok = QInputDialog.getText(self, "Open M3U link", "Paste your M3U (get.php) link:")
        if not ok or not text.strip():
            return
        parsed = parse_m3u_url(text)
        if not parsed:
            QMessageBox.warning(self, "Open M3U link", "That link does not contain a username and password.")
            return
        self.show_login()
        server, username, password = parsed
        self.server_edit.setText(server)
        self.user_edit.setText(username)
        self.pass_edit.setText(password)
        self.login()

    def show_login(self):
        self.top_stack.setCurrentIndex(0)
        self.server_edit.setFocus()

    def login(self):
        server = normalize_server(self.server_edit.text())
        username = self.user_edit.text().strip()
        password = self.pass_edit.text().strip()
        for edit, value in ((self.server_edit, server), (self.user_edit, username), (self.pass_edit, password)):
            if not value:
                edit.setFocus()
                self.notify("Fill in server, username and password.")
                return
        self.server_edit.setText(server)

        self.generation += 1
        generation = self.generation
        client = XtreamClient(server, username, password, self.settings["use_post"])
        self.connect_button.setEnabled(False)
        self.connect_button.setText("Connecting…")
        self.notify(f"Connecting to {client.host}…", 0)
        self.run_task(login_job, client,
                      on_done=lambda result: self._on_login(generation, client, result),
                      on_error=lambda exc: self._on_login_failed(generation, exc))

    def _on_login_failed(self, generation, exc):
        if generation != self.generation:
            return
        self.connect_button.setEnabled(True)
        self.connect_button.setText("Connect")
        self.notify(describe_error(exc), 10000)

    def _on_login(self, generation, client, result):
        if generation != self.generation:
            return
        self.connect_button.setEnabled(True)
        self.connect_button.setText("Connect")
        self.client = client
        self.account_info = result["info"]
        self.epg, self.epg_names = {}, {}

        for kind, pane in self.panes.items():
            if kind == "favorites":
                continue
            categories = sorted(result["categories"][kind],
                                key=lambda c: str(c.get("category_name", "")).casefold())
            items = []
            for category in categories:
                name = str(category.get("category_name") or "Untitled").strip()
                payload = {"_kind": "category", "category_id": category.get("category_id"), "name": name}
                items.append((name, payload, None, True))
            if items:
                pane.set_root(items)
            else:
                pane.show_message("This provider has nothing in this section.")

        self.account_label.setText(client.username)
        self.account_host.setText(client.host)
        self.security_label.setVisible(not client.encrypted)
        self.top_stack.setCurrentIndex(1)
        self._render_account()
        self._favorite_ids = {favorite_id(f) for f in self._favorites()}
        saved = next((a for a in self.settings["accounts"]
                      if a.get("server") == client.server and a.get("username") == client.username), None)
        if saved:
            self.settings["last_account"] = saved["name"]
        self._refresh_favorites_pane()
        if self._favorite_ids:
            self.tabs.setCurrentWidget(self.panes["favorites"])
        elif self._current_kind() == "favorites":
            self.tabs.setCurrentWidget(self.panes["live"])
        self.notify(f"Connected to {client.host}")
        self.panes.get(self._current_kind() or "live").view.setFocus()
        if self.settings["load_epg"]:
            self.load_epg()

    # ── Navigation
    def _current_kind(self):
        pane = self.tabs.currentWidget()
        return pane.kind if isinstance(pane, BrowserPane) else None

    def _focus_search(self):
        pane = self.tabs.currentWidget()
        if isinstance(pane, BrowserPane):
            pane.focus_search()

    def on_activated(self, pane, payload):
        if not self.client:
            return
        kind = payload.get("_kind")
        if kind == "category":
            self._open_category(pane, payload)
        elif kind == "series":
            self._open_series(pane, payload)
        elif kind == "season":
            self._open_season(pane, payload)
        elif kind in ("live", "movie", "episode"):
            self.play(payload)

    def _load_into(self, pane, label, job, build):
        generation = self.generation
        pane.set_busy(True)
        self.notify(f"Loading {label}…", 0)

        def done(data):
            pane.set_busy(False)
            if generation != self.generation:
                return
            items = build(data)
            pane.push(label, items)
            self.notify(f"{len(items)} items", 4000)

        def failed(exc):
            pane.set_busy(False)
            if generation == self.generation:
                self.notify(describe_error(exc), 10000)

        self.run_task(job, on_done=done, on_error=failed)

    def _open_category(self, pane, payload):
        action = {"live": "get_live_streams", "movie": "get_vod_streams", "series": "get_series"}[pane.kind]
        client = self.client

        def job():
            data = client.api(action, category_id=payload["category_id"])
            return data if isinstance(data, list) else []

        self._load_into(pane, payload["name"], job, lambda data: self._stream_items(pane.kind, data))

    def _stream_items(self, kind, data):
        items = []
        for entry in data:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "Untitled").strip()
            if kind == "live":
                entry["_kind"] = "live"
                subtitle, folder = self._epg_subtitle(entry), False
            elif kind == "movie":
                entry["_kind"] = "movie"
                subtitle, folder = None, False
            else:
                entry["_kind"] = "series"
                subtitle, folder = None, True
            items.append((name, entry, subtitle, folder, favorite_id(entry) in self._favorite_ids))
        items.sort(key=lambda item: item[0].casefold())
        return items

    def _open_series(self, pane, payload):
        client = self.client

        def job():
            data = client.api("get_series_info", series_id=payload.get("series_id"))
            return data if isinstance(data, dict) else {}

        def build(data):
            seasons = {}
            episodes = data.get("episodes") or {}
            groups = episodes.items() if isinstance(episodes, dict) else enumerate(episodes)
            for key, value in groups:
                for episode in value if isinstance(value, list) else [value]:
                    if isinstance(episode, dict):
                        number = to_int(episode.get("season"), to_int(key, 0))
                        seasons.setdefault(number, []).append(episode)
            items = []
            for number in sorted(seasons):
                count = len(seasons[number])
                payload_season = {"_kind": "season", "number": number,
                                  "episodes": seasons[number], "series": payload}
                items.append((f"Season {number}", payload_season,
                              f"{count} episode{'s' if count != 1 else ''}", True))
            return items

        self._load_into(pane, payload.get("name") or "Series", job, build)

    def _open_season(self, pane, payload):
        series = payload["series"]
        series_title = str(series.get("name") or "").strip()
        season = payload["number"]
        items = []
        for raw in sorted(payload["episodes"], key=lambda e: to_int(e.get("episode_num"))):
            episode = dict(raw)
            number = to_int(episode.get("episode_num"))
            title = str(episode.get("title") or "").strip()
            if series_title:
                title = re.sub(re.escape(series_title), "", title, flags=re.I).strip(" -–·:|")
            label = f"S{season:02d}E{number:02d}" + (f"  ·  {title}" if title else "")
            episode.update({"_kind": "episode", "_series": series, "_label": label})
            items.append((label, episode, None, False))
        pane.push(f"Season {season}", items)
        self.notify(f"{len(items)} episodes", 4000)

    # ── Playback
    def player_path(self):
        player = self.settings["player"]
        if player and Path(player).exists():
            return player
        if self._detected_player is None:
            self._detected_player = detect_player()
        return self._detected_player

    def _update_player_action(self):
        player = self.player_path()
        label = Path(player).stem if player else "not set"
        self.player_action.setText(f"Media player: {label}…")

    def choose_player(self):
        if IS_WINDOWS:
            filters = "Programs (*.exe)"
        elif IS_MAC:
            filters = "Applications (*.app);;All files (*)"
        else:
            filters = "All files (*)"
        start = str(Path(self.player_path()).parent) if self.player_path() else str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "Choose media player", start, filters)
        if not path:
            return
        if IS_MAC and path.endswith(".app"):
            path = resolve_mac_app(path)
            if not path:
                QMessageBox.warning(self, "Media player", "Could not find the program inside that app.")
                return
        if not IS_WINDOWS and not os.access(path, os.X_OK):
            QMessageBox.warning(self, "Media player", "That file is not executable.")
            return
        self.settings["player"] = path
        self._update_player_action()
        self.notify(f"Media player set to {Path(path).stem}")

    def play(self, payload):
        kind = payload["_kind"]
        if kind == "live":
            url = self.client.stream_url("live", payload.get("stream_id"), "ts")
            title = payload.get("name", "")
        elif kind == "movie":
            url = self.client.stream_url("movie", payload.get("stream_id"), payload.get("container_extension"))
            title = payload.get("name", "")
        else:
            url = self.client.stream_url("series", payload.get("id"), payload.get("container_extension"))
            title = f'{payload["_series"].get("name", "")} {payload.get("_label", "")}'.strip()
        if not url:
            self.notify("This item has no valid stream address.")
            return

        player = self.player_path()
        if not player:
            answer = QMessageBox.question(
                self, "Media player",
                "No media player was found. Choose one now? (VLC is recommended: videolan.org)")
            if answer == QMessageBox.Yes:
                self.choose_player()
            player = self.player_path()
            if not player:
                return
        if IS_WINDOWS and Path(player).suffix.lower() in (".bat", ".cmd"):
            self.notify("Batch files cannot be used as the media player. Choose an .exe file.")
            return

        try:
            subprocess.Popen(
                player_command(player, url, title),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                close_fds=True, start_new_session=not IS_WINDOWS)
            self.notify(f"Opening {title} in {Path(player).stem}…")
        except OSError as exc:
            self.notify(f"Could not start the media player: {exc}", 10000)

    # ── EPG
    def load_epg(self):
        if not self.client:
            return
        generation = self.generation
        self.notify("Loading TV guide…", 0)

        def done(result):
            if generation != self.generation or not self.settings["load_epg"]:
                return
            self.epg, self.epg_names = result
            self._refresh_live_subtitles(all_levels=True)
            self.notify(f"TV guide loaded for {len(self.epg)} channels")

        def failed(exc):
            if generation == self.generation:
                self.notify(f"TV guide unavailable: {describe_error(exc)}", 10000)

        self.run_task(load_epg_job, self.client, on_done=done, on_error=failed)

    def _on_epg_toggled(self, enabled):
        self.settings["load_epg"] = enabled
        if enabled:
            if self.client and not self.epg:
                self.load_epg()
        else:
            self.epg, self.epg_names = {}, {}
            self._refresh_live_subtitles(all_levels=True)

    def _programmes(self, entry):
        if not self.epg:
            return None
        channel_id = (entry.get("epg_channel_id") or "").strip().lower()
        if channel_id not in self.epg:
            channel_id = self.epg_names.get(normalize_channel_name(entry.get("name")), "")
        return self.epg.get(channel_id)

    def _now_next(self, entry):
        programmes = self._programmes(entry)
        if not programmes:
            return None, None
        now = datetime.now(timezone.utc)
        for i, programme in enumerate(programmes):
            if programme[0] <= now < programme[1]:
                return programme, programmes[i + 1] if i + 1 < len(programmes) else None
            if programme[0] > now:
                return None, programme
        return None, None

    def _epg_subtitle(self, entry):
        current, _ = self._now_next(entry)
        return current[2] if current else None

    def _refresh_live_subtitles(self, all_levels=False):
        models = []
        for pane in (self.panes["live"], self.panes["favorites"]):
            models += pane.models() if all_levels else [pane.current_model()]
        for model in models:
            if model is None:
                continue
            for row in range(model.rowCount()):
                item = model.item(row)
                payload = item_payload(item)
                if isinstance(payload, dict) and payload.get("_kind") == "live":
                    subtitle = self._epg_subtitle(payload)
                    if item.data(SUBTITLE_ROLE) != subtitle:
                        item.setData(subtitle, SUBTITLE_ROLE)

    # ── Favorites
    def _favorites(self):
        if not self.client:
            return []
        return list(self.settings["favorites"].get(account_key(self.client.server, self.client.username), []))

    def _save_favorites(self, favorites):
        stored = dict(self.settings["favorites"])
        stored[account_key(self.client.server, self.client.username)] = favorites
        self.settings["favorites"] = stored
        self._favorite_ids = {favorite_id(f) for f in favorites}

    def toggle_favorite(self, payload):
        fid = favorite_id(payload)
        if not self.client or not fid:
            self.notify("Only channels, movies and series can be added to favorites.")
            return
        name = payload.get("name", "")
        favorites = self._favorites()
        if fid in self._favorite_ids:
            favorites = [f for f in favorites if favorite_id(f) != fid]
            message = f"Removed {name} from favorites"
        else:
            # Keep the provider data (for playback and tooltips), not our runtime caches.
            favorites.append({k: v for k, v in payload.items() if k == "_kind" or not k.startswith("_")})
            message = f"Added {name} to favorites"
        self._save_favorites(favorites)
        self._sync_favorite_marks(fid)
        self._refresh_favorites_pane(keep_view=True)
        self.notify(message)

    def _sync_favorite_marks(self, fid):
        marked = fid in self._favorite_ids
        for kind in ("live", "movie", "series"):
            for model in self.panes[kind].models():
                for row in range(model.rowCount()):
                    item = model.item(row)
                    if favorite_id(item_payload(item)) == fid:
                        item.setData(True if marked else None, FAVORITE_ROLE)

    def _refresh_favorites_pane(self, keep_view=False):
        pane = self.panes["favorites"]
        if not self.client:
            pane.show_message("Connect to a server to see your favorites.")
            return
        items = []
        for favorite in self._favorites():
            entry = dict(favorite)
            kind = entry.get("_kind")
            name = str(entry.get("name") or "Untitled").strip()
            if kind == "live":
                items.append((name, entry, self._epg_subtitle(entry), False))
            elif kind == "movie":
                items.append((name, entry, "Movie", False))
            elif kind == "series":
                items.append((name, entry, "Series", True))
        if items:
            pane.set_root(items, keep_view=keep_view)
        else:
            pane.show_message("No favorites yet.\n\nRight-click a channel, movie or series "
                              "and choose Add to favorites, or select it and press Ctrl+D.")

    def _toggle_current_favorite(self):
        pane = self.tabs.currentWidget()
        if isinstance(pane, BrowserPane):
            payload = pane.current_payload()
            if payload is not None:
                self.toggle_favorite(payload)

    def _remove_current_favorite(self):
        payload = self.panes["favorites"].current_payload()
        if favorite_id(payload) in self._favorite_ids:
            self.toggle_favorite(payload)

    def _show_item_menu(self, pane, payload, global_pos):
        if not isinstance(payload, dict) or not self.client:
            return
        kind = payload.get("_kind")
        menu = QMenu(self)
        if kind in ("live", "movie", "episode"):
            menu.addAction("Play", lambda: self.on_activated(pane, payload))
        else:
            menu.addAction("Open", lambda: self.on_activated(pane, payload))
        fid = favorite_id(payload)
        if fid:
            menu.addSeparator()
            label = "Remove from favorites" if fid in self._favorite_ids else "Add to favorites"
            action = menu.addAction(label, lambda: self.toggle_favorite(payload))
            action.setShortcut(QKeySequence("Ctrl+D"))
        menu.exec_(global_pos)

    # ── Tooltips
    def eventFilter(self, obj, event):
        if event.type() == QEvent.ToolTip and obj in self._viewports:
            self._show_tooltip(self._viewports[obj], event.pos(), event.globalPos())
            return True
        return super().eventFilter(obj, event)

    def _show_tooltip(self, pane, pos, global_pos):
        payload = pane.payload_at(pos)
        kind = payload.get("_kind") if isinstance(payload, dict) else None
        viewport = pane.view.viewport()
        if kind == "live":
            text = self._live_tooltip(payload)
            if text:
                QToolTip.showText(global_pos, text, viewport)
            else:
                QToolTip.hideText()
        elif kind in ("movie", "series", "episode") and self.client:
            details = payload.get("_details")
            if details is not None:
                QToolTip.showText(global_pos, self._media_tooltip(details), viewport)
                return
            QToolTip.showText(global_pos, f'<span style="color:{self.theme["muted"]}">Loading…</span>', viewport)
            if payload.get("_details_loading"):
                return
            payload["_details_loading"] = True

            def done(result):
                payload["_details"] = result
                payload.pop("_details_loading", None)
                cursor = QCursor.pos()
                if viewport.underMouse() and pane.payload_at(viewport.mapFromGlobal(cursor)) is payload:
                    QToolTip.showText(cursor, self._media_tooltip(result), viewport)

            def failed(_):
                payload.pop("_details_loading", None)

            self.run_task(fetch_details, self.client, kind, dict(payload),
                          on_done=done, on_error=failed, busy=False)
        else:
            QToolTip.hideText()

    def _live_tooltip(self, entry):
        current, upcoming = self._now_next(entry)
        if not current and not upcoming:
            return ""
        muted = self.theme["muted"]

        def hhmm(value):
            return value.astimezone().strftime("%H:%M")

        parts = [f"<b>{html.escape(entry.get('name', ''))}</b>"]
        if current:
            parts.append(f'<br><span style="color:{muted}">Now · {hhmm(current[0])}–{hhmm(current[1])}</span>'
                         f"<br>{html.escape(current[2])}")
            if current[3]:
                parts.append(f'<p style="margin-top:6px">{html.escape(truncate(current[3], 420))}</p>')
        if upcoming:
            parts.append(f'<br><span style="color:{muted}">Next · {hhmm(upcoming[0])}</span>'
                         f" {html.escape(upcoming[2])}")
        return f'<table width="340"><tr><td>{"".join(parts)}</td></tr></table>'

    def _media_tooltip(self, d):
        muted = self.theme["muted"]
        meta = " · ".join(x for x in (d["year"], d["genre"], f'★ {d["rating"]}' if d["rating"] else "",
                                       d["duration"]) if x)
        body = [f"<b>{html.escape(d['title'])}</b>"]
        if meta:
            body.append(f'<br><span style="color:{muted}">{html.escape(meta)}</span>')
        body.append(f'<p>{html.escape(truncate(d["plot"], 520)) or "No description available."}</p>')
        if d["director"]:
            body.append(f'<span style="color:{muted}">Director:</span> {html.escape(truncate(d["director"], 80))}<br>')
        if d["cast"]:
            body.append(f'<span style="color:{muted}">Cast:</span> {html.escape(truncate(d["cast"], 140))}')
        image = ""
        if d["image"]:
            src = html.escape(QUrl.fromLocalFile(d["image"]).toString(), quote=True)
            image = f'<td valign="top" width="112"><img src="{src}" width="100"></td>'
        return (f'<table cellspacing="0" cellpadding="0" width="{440 if image else 340}"><tr>{image}'
                f'<td valign="top">{"".join(body)}</td></tr></table>')

    # ── Account tab
    def _render_account(self):
        t = self.theme
        if not self.client or not self.account_info:
            self.account_view.setHtml(f'<p style="color:{t["muted"]}">Not connected.</p>')
            return
        user = self.account_info.get("user_info") or {}
        server = self.account_info.get("server_info") or {}

        def date(value):
            stamp = to_int(value, 0)
            return datetime.fromtimestamp(stamp).strftime("%d %b %Y") if stamp > 0 else "—"

        expires = user.get("exp_date")
        rows = [
            ("Status", str(user.get("status") or "—")),
            ("Expires", date(expires) if expires not in (None, "", "null", "0", 0) else "Never"),
            ("Trial", "Yes" if str(user.get("is_trial")) == "1" else "No"),
            ("Connections", f'{user.get("active_cons", 0)} in use of {user.get("max_connections", "—")}'),
            ("Created", date(user.get("created_at"))),
            ("Username", self.client.username),
            ("Server", self.client.host),
            ("Connection", "HTTPS (encrypted)" if self.client.encrypted else "HTTP (not encrypted)"),
            ("Server time zone", str(server.get("timezone") or "—")),
        ]
        cells = "".join(
            f'<tr><td style="color:{t["muted"]};padding:6px 24px 6px 0">{html.escape(k)}</td>'
            f'<td style="padding:6px 0">{html.escape(v)}</td></tr>' for k, v in rows)
        self.account_view.setHtml(f"<table cellspacing='0'>{cells}</table>")

    # ── Saved accounts
    def open_accounts(self):
        dialog = AccountsDialog(self)
        set_dark_title_bar(dialog, self.theme is THEMES["dark"])
        dialog.exec_()

    def save_current_account(self):
        server = normalize_server(self.server_edit.text())
        dialog = AccountDialog(self, urlparse(server).hostname or "", server,
                               self.user_edit.text().strip(), self.pass_edit.text().strip())
        set_dark_title_bar(dialog, self.theme is THEMES["dark"])
        if dialog.exec_() == QDialog.Accepted:
            self.save_account(*dialog.values())

    def save_account(self, name, server, username, password):
        accounts = [a for a in self.settings["accounts"] if a.get("name") != name]
        accounts.append({"name": name, "server": server, "username": username})
        accounts.sort(key=lambda a: a["name"].casefold())
        self.settings["accounts"] = accounts
        if store_password(account_key(server, username), password):
            self.notify(f'Saved "{name}". The password is kept in the system keychain.')
        else:
            QMessageBox.warning(
                self, "Save account",
                "The account was saved, but the system keychain is not available, so the "
                "password was not stored. You will be asked for it when connecting.")

    def remove_account(self, name):
        accounts = self.settings["accounts"]
        removed = [a for a in accounts if a.get("name") == name]
        remaining = [a for a in accounts if a.get("name") != name]
        self.settings["accounts"] = remaining
        for account in removed:
            key = account_key(account["server"], account["username"])
            if not any(account_key(a["server"], a["username"]) == key for a in remaining):
                delete_password(key)

    def _auto_connect(self):
        accounts = self.settings["accounts"]
        if not self.settings["auto_connect"] or not accounts or self.client:
            return
        last = self.settings["last_account"]
        account = next((a for a in accounts if a.get("name") == last), accounts[0])
        self.connect_saved(account)

    def connect_saved(self, account):
        self.show_login()
        self.server_edit.setText(account["server"])
        self.user_edit.setText(account["username"])
        password = load_password(account_key(account["server"], account["username"]))
        self.pass_edit.setText(password)
        if password:
            self.login()
        else:
            self.pass_edit.setFocus()
            self.notify("Enter the password to connect.")

    # ── Menu toggles
    def _on_post_toggled(self, enabled):
        self.settings["use_post"] = enabled
        if self.client:
            self.client.use_post = enabled

    def _on_top_toggled(self, enabled):
        self.settings["on_top"] = enabled
        self.setWindowFlag(Qt.WindowStaysOnTopHint, enabled)
        self.show()

    def clear_cache(self):
        removed = 0
        for folder in ("images", "epg"):
            for file in (Paths.cache / folder).glob("*"):
                try:
                    file.unlink()
                    removed += 1
                except OSError:
                    pass
        for pane in self.panes.values():
            for model in pane.models():
                for row in range(model.rowCount()):
                    payload = item_payload(model.item(row))
                    if isinstance(payload, dict):
                        payload.pop("_details", None)
        self.notify(f"Cache cleared ({removed} files)")

    def show_about(self):
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<b>{APP_NAME}</b> {APP_VERSION}<br>by {APP_AUTHOR}<br><br>"
            "Licensed under the GNU General Public License v2.<br><br>"
            "This application is only a player. It does not provide channels, "
            "playlists or subscriptions.")

    def closeEvent(self, event):
        self.settings["geometry"] = bytes(self.saveGeometry().toBase64()).decode()
        super().closeEvent(event)


def main():
    QCoreApplication.setOrganizationName(APP_AUTHOR)
    QCoreApplication.setApplicationName(APP_NAME)
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    if hasattr(QGuiApplication, "setHighDpiScaleFactorRoundingPolicy"):
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication(sys.argv)
    system_dark = system_prefers_dark()
    app.setStyle("Fusion")
    app.setWindowIcon(make_app_icon())
    if IS_WINDOWS:
        app.setFont(QFont("Segoe UI", 10))

    Paths.init()
    window = MainWindow(Settings(Paths.data / "settings.json"), system_dark)
    window.show()
    code = app.exec_()

    # Do not let a slow download keep the process alive after the window closes.
    window.pool.clear()
    if not window.pool.waitForDone(1500):
        os._exit(code)
    sys.exit(code)


if __name__ == "__main__":
    main()
