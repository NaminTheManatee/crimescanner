import argparse
import html
import json
import sqlite3
import hashlib
import re
import threading
import webbrowser
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import URLError
from urllib.parse import urljoin, quote
from urllib.request import Request, urlopen

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except Exception as exc:
    raise RuntimeError("tkinter is required to run this GUI.") from exc

try:
    from PIL import Image, ImageDraw, ImageTk  # type: ignore
except Exception:
    Image = None
    ImageDraw = None
    ImageTk = None

APP_NAME = "CrimeScanner"
DEFAULT_THEME = "Red"
DEFAULT_DEDUPE_SECONDS = 60
DEFAULT_SETTINGS_FILE = "crime_logger_game_log_settings.json"
DEFAULT_RSI_CACHE_FILE = "crime_scanner_rsi_cache.json"
DEFAULT_IMAGE_CACHE_DIR = "crime_scanner_image_cache"
DEFAULT_STATS_CACHE_FILE = "crime_scanner_stats_cache.json"
DEFAULT_DB_FILE = "crime_scanner_history.sqlite3"
RSI_CACHE_SCHEMA_VERSION = 4
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CrimeScanner/1.0"
RSI_CITIZEN_BASE = "https://robertsspaceindustries.com/en/citizens/"
RSI_ORG_BASE = "https://robertsspaceindustries.com/en/orgs/"
DEFAULT_LOGO_URL = "https://cdn.robertsspaceindustries.com/static/images/organization/defaults/logo/syndicate.jpg"
DEFAULT_PLAYER_AVATAR_URL = "https://cdn.robertsspaceindustries.com/static/images/account/avatar_default_big.jpg"
DEFAULT_ORG_LOGO_URL = "https://cdn.robertsspaceindustries.com/static/images/account/avatar_default_big.jpg"
REDACTED_ORG_LOGO_NAME = "59da34b90674408651c0a9373bff19c6dd65da524605b874f914bd576bdae2b1.png"


def build_citizen_url(player: str) -> str:
    return f"{RSI_CITIZEN_BASE}{quote(player.strip(), safe='')}"

THEME_COLORS = {
    "Red": {"accent": "#b30e20", "bright": "#ff3048", "glow": "#ff5c6d"},
    "Blue": {"accent": "#1b5cff", "bright": "#4d86ff", "glow": "#6aa0ff"},
    "Green": {"accent": "#198a39", "bright": "#34d063", "glow": "#61e68a"},
    "Purple": {"accent": "#6f2bce", "bright": "#9653f6", "glow": "#b483ff"},
    "Orange": {"accent": "#c75d10", "bright": "#ff8b2c", "glow": "#ffac63"},
    "Cyan": {"accent": "#0e91a4", "bright": "#25cde4", "glow": "#6fe7f4"},
    "Gold": {"accent": "#af820d", "bright": "#d9aa2e", "glow": "#ebc867"},
    "Pink": {"accent": "#be2f73", "bright": "#f2559d", "glow": "#ff8fbe"},
}

TIMESTAMP_RE = re.compile(
    r'^<(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)>',
    re.IGNORECASE,
)
CRIME_TEXT_RE = re.compile(
    r'Crime\s+Committed:\s*(?P<crime>.+?)\s*$',
    re.IGNORECASE,
)
AGAINST_PLAYER_RE = re.compile(
    r'against\s+(?P<player>.+?):\s*"\s*(?:\[(?P<id>\d+)\])?',
    re.IGNORECASE,
)
SELF_CRIME_RE = re.compile(
    r'"?(?P<player>[A-Za-z0-9_\-][A-Za-z0-9_\- ]*?)\s+committed\s+(?P<crime>.+?)\s+against\s+you\.?\s*$',
    re.IGNORECASE,
)


@dataclass
class AppSettings:
    theme_name: str = DEFAULT_THEME
    dedupe_seconds: int = DEFAULT_DEDUPE_SECONDS
    game_folder_path: str = ""


@dataclass
class CrimeEvent:
    player: str
    crime: str
    timestamp_utc: datetime
    target_type: str = "other"
    player_url: str = ""
    player_avatar_url: str = DEFAULT_PLAYER_AVATAR_URL
    organization: str = ""
    organization_url: str = ""
    organization_logo_url: str = ""

    @property
    def display_time(self) -> str:
        return self.timestamp_utc.strftime("%H:%M, %d/%m/%Y")


class RSICacheStore:
    def __init__(self, path: str = DEFAULT_RSI_CACHE_FILE):
        self.path = Path(path)

    def load(self) -> Dict[str, Dict[str, str]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save(self, cache: Dict[str, Dict[str, str]]) -> None:
        try:
            self.path.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def clear(self) -> None:
        try:
            if self.path.exists():
                self.path.unlink()
        except Exception:
            pass


class ImageDiskCache:
    def __init__(self, cache_dir: str = DEFAULT_IMAGE_CACHE_DIR):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()
        suffix = ".img"
        lower = url.lower()
        for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            if ext in lower:
                suffix = ext
                break
        return self.cache_dir / f"{digest}{suffix}"

    def read_bytes(self, url: str) -> Optional[bytes]:
        path = self._cache_path(url)
        if path.exists():
            try:
                return path.read_bytes()
            except Exception:
                return None
        return None

    def write_bytes(self, url: str, data: bytes) -> None:
        try:
            self._cache_path(url).write_bytes(data)
        except Exception:
            pass

    def clear(self) -> None:
        try:
            if self.cache_dir.exists():
                for child in self.cache_dir.iterdir():
                    if child.is_file():
                        try:
                            child.unlink()
                        except Exception:
                            pass
        except Exception:
            pass


class StatsCacheStore:
    def __init__(self, path: str = DEFAULT_STATS_CACHE_FILE):
        self.path = Path(path)

    def load(self) -> Dict[str, object]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save(self, payload: Dict[str, object]) -> None:
        try:
            self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


class EventDatabase:
    def __init__(self, path: str = DEFAULT_DB_FILE):
        self.path = Path(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    player TEXT NOT NULL,
                    crime TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    timestamp_utc TEXT NOT NULL,
                    display_time TEXT NOT NULL,
                    player_url TEXT NOT NULL DEFAULT '',
                    player_avatar_url TEXT NOT NULL DEFAULT '',
                    organization TEXT NOT NULL DEFAULT '',
                    organization_url TEXT NOT NULL DEFAULT '',
                    organization_logo_url TEXT NOT NULL DEFAULT '',
                    UNIQUE(player, crime, target_type, timestamp_utc)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp_utc)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_player ON events(player)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_org ON events(organization)"
            )

    def upsert_events(self, events: List[CrimeEvent]) -> None:
        rows = [
            (
                event.player,
                event.crime,
                event.target_type,
                event.timestamp_utc.isoformat(),
                event.display_time,
                event.player_url or "",
                event.player_avatar_url or DEFAULT_PLAYER_AVATAR_URL,
                event.organization or "",
                event.organization_url or "",
                event.organization_logo_url or "",
            )
            for event in events
        ]
        if not rows:
            return
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO events (
                    player, crime, target_type, timestamp_utc, display_time,
                    player_url, player_avatar_url, organization, organization_url, organization_logo_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(player, crime, target_type, timestamp_utc) DO UPDATE SET
                    display_time=excluded.display_time,
                    player_url=excluded.player_url,
                    player_avatar_url=CASE
                        WHEN excluded.player_avatar_url <> '' THEN excluded.player_avatar_url
                        ELSE events.player_avatar_url
                    END,
                    organization=CASE
                        WHEN excluded.organization <> '' THEN excluded.organization
                        ELSE events.organization
                    END,
                    organization_url=CASE
                        WHEN excluded.organization_url <> '' THEN excluded.organization_url
                        ELSE events.organization_url
                    END,
                    organization_logo_url=CASE
                        WHEN excluded.organization_logo_url <> '' THEN excluded.organization_logo_url
                        ELSE events.organization_logo_url
                    END
                """
            , rows)

    def update_event_metadata(self, event: CrimeEvent) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE events
                SET player_url=?,
                    player_avatar_url=?,
                    organization=?,
                    organization_url=?,
                    organization_logo_url=?,
                    display_time=?
                WHERE player=? AND crime=? AND target_type=? AND timestamp_utc=?
                """,
                (
                    event.player_url or "",
                    event.player_avatar_url or DEFAULT_PLAYER_AVATAR_URL,
                    event.organization or "",
                    event.organization_url or "",
                    event.organization_logo_url or "",
                    event.display_time,
                    event.player,
                    event.crime,
                    event.target_type,
                    event.timestamp_utc.isoformat(),
                ),
            )

    def stats_summary(self) -> Dict[str, int]:
        with self._connect() as connection:
            total_events = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            total_other = connection.execute("SELECT COUNT(*) FROM events WHERE target_type='other'").fetchone()[0]
            total_self = connection.execute("SELECT COUNT(*) FROM events WHERE target_type='self'").fetchone()[0]
        return {
            "total_events": int(total_events),
            "total_other": int(total_other),
            "total_self": int(total_self),
        }

    def stats_rows(self, key: str) -> List[Tuple[str, int]]:
        if key not in {"encountered", "encountered_org", "killed", "killed_by"}:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT player, organization, crime, target_type, timestamp_utc FROM events"
            ).fetchall()

        grouped: Dict[Tuple[str, str, str], str] = {}
        for row in rows:
            minute_key = str(row["timestamp_utc"])[:16]
            player = str(row["player"] or "")
            org = str(row["organization"] or "")
            crime = str(row["crime"] or "")
            target_type = str(row["target_type"] or "")

            if key == "encountered":
                label = player
                if not label:
                    continue
                group_key = (label.casefold(), player.casefold(), minute_key)
            elif key == "encountered_org":
                label = org.strip() or "Unknown"
                group_key = (label.casefold(), player.casefold(), minute_key)
            elif key == "killed":
                if target_type != "other":
                    continue
                label = player
                if not label:
                    continue
                group_key = (label.casefold(), player.casefold(), minute_key)
            else:  # killed_by
                if target_type != "self":
                    continue
                label = player
                if not label:
                    continue
                group_key = (label.casefold(), player.casefold(), minute_key)

            grouped[group_key] = label

        counts: Dict[str, int] = {}
        for label in grouped.values():
            counts[label] = counts.get(label, 0) + 1

        return sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold()))


class AppSettingsStore:
    def __init__(self, path: str = DEFAULT_SETTINGS_FILE):
        self.path = Path(path)

    def load(self) -> AppSettings:
        if not self.path.exists():
            return AppSettings()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            legacy_log_path = str(data.get("last_log_path", "")).strip()
            game_folder_path = str(data.get("game_folder_path", "")).strip()
            if not game_folder_path and legacy_log_path:
                try:
                    legacy_path = Path(legacy_log_path)
                    game_folder_path = str(legacy_path.parent if legacy_path.suffix.lower() == ".log" else legacy_path)
                except Exception:
                    game_folder_path = ""
            return AppSettings(
                theme_name=str(data.get("theme_name", DEFAULT_THEME)),
                dedupe_seconds=max(0, int(data.get("dedupe_seconds", DEFAULT_DEDUPE_SECONDS))),
                game_folder_path=game_folder_path,
            )
        except Exception:
            return AppSettings()

    def save(self, settings: AppSettings) -> None:
        self.path.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")


class GameLogParser:
    """Normalize repeated notification wrappers into canonical crime events."""

    def __init__(self, dedupe_seconds: int = DEFAULT_DEDUPE_SECONDS):
        self.dedupe_seconds = max(0, int(dedupe_seconds))

    @staticmethod
    def _parse_timestamp(text: str) -> datetime:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).astimezone(timezone.utc)

    @staticmethod
    def _extract_timestamp(line: str) -> Optional[datetime]:
        match = TIMESTAMP_RE.match(line)
        if not match:
            return None
        return GameLogParser._parse_timestamp(match.group("ts"))

    @staticmethod
    def _clean_spaces(text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    def _should_keep(self, seen: Dict[Tuple[str, str, str], datetime], player: str, crime: str, target_type: str, ts: datetime) -> bool:
        key = (player.casefold(), crime.casefold(), target_type)
        previous = seen.get(key)
        if previous is not None and (ts - previous).total_seconds() <= self.dedupe_seconds:
            return False
        seen[key] = ts
        return True

    def parse(self, log_path: Path) -> List[CrimeEvent]:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        results: List[CrimeEvent] = []
        seen: Dict[Tuple[str, str, str], datetime] = {}

        i = 0
        while i < len(lines):
            line = lines[i].rstrip("\n")

            # Family 1: "<player> committed <crime> against you"
            if " committed " in line and " against you" in line:
                ts = self._extract_timestamp(line)
                match = SELF_CRIME_RE.search(line)
                if ts is not None and match:
                    player = self._clean_spaces(match.group("player"))
                    if " " in player:
                        i += 1
                        continue
                    crime = self._clean_spaces(match.group("crime"))
                    if self._should_keep(seen, player, crime, "self", ts):
                        results.append(
                            CrimeEvent(
                                player=player,
                                crime=crime,
                                timestamp_utc=ts,
                                target_type="self",
                                player_url=build_citizen_url(player),
                            )
                        )
                    i += 1
                    continue

            # Family 2: two-line "Crime Committed: <crime>" then "against <player>:"
            if i + 1 < len(lines) and "Crime Committed:" in line:
                next_line = lines[i + 1].rstrip("\n")
                ts = self._extract_timestamp(line)
                crime_match = CRIME_TEXT_RE.search(line)
                player_match = AGAINST_PLAYER_RE.search(next_line)
                if ts is not None and crime_match and player_match:
                    crime = self._clean_spaces(crime_match.group("crime"))
                    player = self._clean_spaces(player_match.group("player"))
                    if " " in player:
                        i += 2
                        continue
                    if self._should_keep(seen, player, crime, "other", ts):
                        results.append(
                            CrimeEvent(
                                player=player,
                                crime=crime,
                                timestamp_utc=ts,
                                target_type="other",
                                player_url=build_citizen_url(player),
                            )
                        )
                    i += 2
                    continue

            i += 1

        return results


class RSIProfileLookup:
    def __init__(self, timeout_seconds: float = 4.0, cache_store: Optional[RSICacheStore] = None):
        self.timeout_seconds = timeout_seconds
        self.cache_store = cache_store or RSICacheStore()
        self._disk_cache = self.cache_store.load()
        self._cache: Dict[str, Tuple[str, str, str, str, str]] = {}
        for key, value in self._disk_cache.items():
            if isinstance(value, dict):
                self._cache[key] = (
                    str(value.get("org_name", "")),
                    str(value.get("org_url", "")),
                    str(value.get("player_avatar_url", DEFAULT_PLAYER_AVATAR_URL)),
                    str(value.get("org_logo_url", "")),
                    str(value.get("org_state", "known")),
                )


    @staticmethod
    def _normalize_org_state(org_name: str, org_logo_url: str) -> str:
        if not (org_name or "").strip():
            return "unknown"
        return "known"

    def lookup_player_details(self, player: str) -> Tuple[str, str, str, str, str]:
        if not player:
            return "", "", DEFAULT_PLAYER_AVATAR_URL, "", "unknown"
        if player in self._cache:
            # lightweight refresh check (no heavy parsing)
            try:
                player_url = build_citizen_url(player)
                request = Request(player_url, headers={"User-Agent": USER_AGENT})
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    page_html = response.read().decode("utf-8", errors="replace")

                page_html = html.unescape(page_html)
                new_avatar = self._extract_player_avatar(page_html, player_url) or DEFAULT_PLAYER_AVATAR_URL
                new_org_name, new_org_url = self._extract_main_org(page_html)

                cached_org, cached_org_url, cached_avatar, cached_logo, cached_state = self._cache[player]

                current_logo = self._extract_org_logo_from_citizen_page(page_html, player_url)
                new_state = self._normalize_org_state(new_org_name or "", current_logo or "")
                if (
                    new_avatar != cached_avatar or
                    new_org_name != cached_org or
                    new_org_url != cached_org_url or
                    new_state != cached_state
                ):
                    # invalidate cache and re-fetch fully
                    del self._cache[player]
                else:
                    return self._cache[player]
            except Exception:
                return self._cache[player]

        player_url = build_citizen_url(player)
        request = Request(player_url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                page_html = response.read().decode("utf-8", errors="replace")
        except (URLError, TimeoutError, OSError, UnicodeError, ValueError):
            self._cache[player] = ("", "", DEFAULT_PLAYER_AVATAR_URL, "", "unknown")
            self._disk_cache[player] = {
                "schema_version": RSI_CACHE_SCHEMA_VERSION,
                "org_name": "",
                "org_url": "",
                "player_avatar_url": DEFAULT_PLAYER_AVATAR_URL,
                "org_logo_url": "",
                "org_state": "unknown",
            }
            self.cache_store.save(self._disk_cache)
            return self._cache[player]

        page_html = html.unescape(page_html)
        player_avatar_url = self._extract_player_avatar(page_html, player_url) or DEFAULT_PLAYER_AVATAR_URL
        org_name, org_url = self._extract_main_org(page_html)

        # Guard against title / mixed text pollution where the player's handle leaks into the org name.
        if org_name:
            cleaned = re.sub(r"\s+", " ", org_name).strip()
            if player.casefold() in cleaned.casefold():
                cleaned = re.sub(re.escape(player), "", cleaned, flags=re.IGNORECASE).strip(" -|/")
            if "|" in cleaned and player.casefold() in cleaned.split("|")[0].casefold():
                cleaned = cleaned.split("|", 1)[-1].strip()
            org_name = cleaned

        org_logo_url = ""
        if org_url:
            org_logo_url = self._fetch_org_logo(org_url)
        if not org_logo_url:
            org_logo_url = self._extract_org_logo_from_citizen_page(page_html, player_url)

        org_state = self._normalize_org_state(org_name or "", org_logo_url or "")
        self._cache[player] = (org_name or "", org_url, player_avatar_url, org_logo_url or DEFAULT_ORG_LOGO_URL, org_state)
        self._disk_cache[player] = {
            "schema_version": RSI_CACHE_SCHEMA_VERSION,
            "org_name": org_name or "",
            "org_url": org_url,
            "player_avatar_url": player_avatar_url,
            "org_logo_url": org_logo_url or DEFAULT_ORG_LOGO_URL,
            "org_state": org_state,
        }
        self.cache_store.save(self._disk_cache)
        return self._cache[player]

    @staticmethod
    def _extract_main_org(page_html: str) -> Tuple[str, str]:
        patterns = [
            r"<a[^>]+href=[\"']/(?:en/)?orgs/([^\"'#?]+)[\"'][^>]*>\s*([^<]+?)\s*</a>",
            r'"organization_name"\s*:\s*"([^"]+)".*?"organization_sid"\s*:\s*"([^"]+)"',
        ]
        for pattern in patterns:
            matches = re.findall(pattern, page_html, flags=re.IGNORECASE | re.DOTALL)
            for first, second in matches:
                if "organization_name" in pattern:
                    name, slug = first, second
                else:
                    slug, name = first, second
                clean_name = re.sub(r"\s+", " ", name).strip()
                clean_slug = slug.strip().strip("/")
                if clean_name and clean_slug:
                    return clean_name, f"{RSI_ORG_BASE}{clean_slug}"
        return "", ""

    @staticmethod
    def _extract_image_urls(page_html: str, base_url: str) -> List[str]:
        patterns = [
            r"(?:src|data-src|content)=[\"']([^\"']+\.(?:png|jpe?g|gif|webp)[^\"']*)[\"']",
            r"url\(([\"']?)(/[^)\"']+\.(?:png|jpe?g|gif|webp)[^)]*)\1\)",
        ]
        urls: List[str] = []
        for pattern in patterns:
            for match in re.findall(pattern, page_html, flags=re.IGNORECASE):
                candidate = match[0] if isinstance(match, tuple) else match
                if not candidate:
                    continue
                full = urljoin(base_url, candidate.strip())
                if full not in urls:
                    urls.append(full)
        return urls

    @classmethod
    def _rank_image_url(cls, url: str, preferred_keywords: List[str], avoid_keywords: List[str]) -> Tuple[int, int, int]:
        lower = url.lower()
        pref_score = sum(1 for kw in preferred_keywords if kw in lower)
        avoid_score = sum(1 for kw in avoid_keywords if kw in lower)
        media_bonus = 1 if '/media/' in lower else 0
        return (pref_score, media_bonus, -avoid_score)

    @classmethod
    def _extract_player_avatar(cls, page_html: str, base_url: str) -> str:
        image_urls = cls._extract_image_urls(page_html, base_url)
        if not image_urls:
            return DEFAULT_PLAYER_AVATAR_URL
        ranked = sorted(
            image_urls,
            key=lambda u: cls._rank_image_url(
                u,
                preferred_keywords=['avatar', 'profile', 'citizen', 'handle', 'portrait'],
                avoid_keywords=['logo', 'org', 'organization', 'syndicate', 'default/logo', '/orgs/', 'heap_infobox'],
            ),
            reverse=True,
        )
        return ranked[0]

    @classmethod
    def _extract_org_logo_from_citizen_page(cls, page_html: str, base_url: str) -> str:
        image_urls = cls._extract_image_urls(page_html, base_url)
        if not image_urls:
            return ''
        ranked = sorted(
            image_urls,
            key=lambda u: cls._rank_image_url(
                u,
                preferred_keywords=['logo', 'organization', 'org', '/orgs/', 'heap_infobox'],
                avoid_keywords=['avatar_default', 'avatar', 'citizen', 'profile', 'portrait', '/citizens/'],
            ),
            reverse=True,
        )
        best = ranked[0]
        if best == DEFAULT_PLAYER_AVATAR_URL:
            return ''
        return best

    def _fetch_org_logo(self, org_url: str) -> str:
        request = Request(org_url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                page_html = html.unescape(response.read().decode("utf-8", errors="replace"))
        except (URLError, TimeoutError, OSError):
            return DEFAULT_ORG_LOGO_URL

        image_urls = self._extract_image_urls(page_html, org_url)
        if not image_urls:
            return DEFAULT_ORG_LOGO_URL
        ranked = sorted(
            image_urls,
            key=lambda u: self._rank_image_url(
                u,
                preferred_keywords=['logo', 'organization', 'org', 'heap_infobox'],
                avoid_keywords=['avatar_default', 'avatar', 'citizen', 'profile', 'portrait'],
            ),
            reverse=True,
        )
        return ranked[0]


class RoundedFrame(tk.Canvas):
    def __init__(self, parent: tk.Widget, *, bg: str, fill: str, outline: str, radius: int = 18, border_width: int = 1, padding: int = 10, **kwargs: object):
        super().__init__(parent, bg=bg, highlightthickness=0, bd=0, relief="flat", **kwargs)
        self._bg = bg
        self._fill = fill
        self._outline = outline
        self._radius = radius
        self._border_width = border_width
        self._padding = padding
        self.inner = tk.Frame(self, bg=fill, bd=0, highlightthickness=0)
        self._window = self.create_window((padding, padding), window=self.inner, anchor="nw")
        self.bind("<Configure>", self._redraw)
        self.after(0, self._redraw)

    @staticmethod
    def _rounded_points(x1: int, y1: int, x2: int, y2: int, r: int) -> List[int]:
        return [
            x1 + r, y1,
            x2 - r, y1,
            x2, y1,
            x2, y1 + r,
            x2, y2 - r,
            x2, y2,
            x2 - r, y2,
            x1 + r, y2,
            x1, y2,
            x1, y2 - r,
            x1, y1 + r,
            x1, y1,
        ]

    def _redraw(self, _event: object = None) -> None:
        self.delete("shape")
        width = max(2, self.winfo_width())
        height = max(2, self.winfo_height())
        radius = max(4, min(self._radius, width // 2, height // 2))
        points = self._rounded_points(1, 1, width - 1, height - 1, radius)
        self.create_polygon(points, smooth=True, fill=self._fill, outline=self._outline, width=self._border_width, tags="shape")
        self.coords(self._window, self._padding, self._padding)
        self.itemconfigure(self._window, width=max(1, width - self._padding * 2), height=max(1, height - self._padding * 2))


class RoundedButton(tk.Canvas):
    def __init__(self, parent: tk.Widget, *, text: str, command, width: int = 150, height: int = 46, radius: int = 16, bg: str = "#000", fill: str = "#222", outline: str = "#555", hover_fill: str = "#333", hover_outline: str = "#777", text_color: str = "#fff"):
        super().__init__(parent, width=width, height=height, bg=bg, highlightthickness=0, bd=0, relief="flat", cursor="hand2")
        self._command = command
        self._text = text
        self._radius = radius
        self._fill = fill
        self._outline = outline
        self._hover_fill = hover_fill
        self._hover_outline = hover_outline
        self._text_color = text_color
        self._hovering = False
        self._pressed = False
        self.bind("<Configure>", self._redraw)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.after(0, self._redraw)

    def _colors(self) -> Tuple[str, str]:
        if self._hovering or self._pressed:
            return self._hover_fill, self._hover_outline
        return self._fill, self._outline

    def _redraw(self, _event: object = None) -> None:
        self.delete("all")
        width = max(2, self.winfo_width())
        height = max(2, self.winfo_height())
        radius = max(4, min(self._radius, width // 2, height // 2))
        fill, outline = self._colors()
        inset = 2 if self._pressed else 0
        points = RoundedFrame._rounded_points(1, 1 + inset, width - 1, height - 1, radius)
        self.create_polygon(points, smooth=True, fill=fill, outline=outline, width=1.5)
        self.create_text(width / 2, height / 2 + inset, text=self._text, fill=self._text_color, font=("Segoe UI", 10, "bold"))

    def _on_enter(self, _event: object) -> None:
        self._hovering = True
        self._redraw()

    def _on_leave(self, _event: object) -> None:
        self._hovering = False
        self._pressed = False
        self._redraw()

    def _on_press(self, _event: object) -> None:
        self._pressed = True
        self._redraw()

    def _on_release(self, event: object) -> None:
        hovered = 0 <= getattr(event, "x", -1) <= self.winfo_width() and 0 <= getattr(event, "y", -1) <= self.winfo_height()
        was_pressed = self._pressed
        self._pressed = False
        self._redraw()
        if hovered and was_pressed:
            self._command()


class CrimeScannerApp:
    BG = "#09090d"
    PANEL = "#14141b"
    PANEL_ALT = "#1a1a24"
    PANEL_SOFT = "#232330"
    PANEL_HOVER = "#2d2d3a"
    TEXT = "#f2f2f2"
    MUTED = "#8e8e9a"
    GOLD = "#d5ab45"
    LINK = "#7ec8ff"
    ROW_HEIGHT = 72
    AVATAR_SIZE = 34
    OTHER_FILL = "#153322"
    OTHER_BORDER = "#39c16c"
    SELF_FILL = "#3a181c"
    SELF_BORDER = "#e25b69"

    def __init__(self, settings_store: AppSettingsStore, initial_path: Optional[str] = None, initial_dedupe: Optional[int] = None):
        self.settings_store = settings_store
        self.settings = settings_store.load()
        if initial_path:
            try:
                initial_candidate = Path(initial_path)
                self.settings.game_folder_path = str(initial_candidate.parent if initial_candidate.suffix.lower() == ".log" else initial_candidate)
            except Exception:
                self.settings.game_folder_path = initial_path
        if initial_dedupe is not None:
            self.settings.dedupe_seconds = max(0, int(initial_dedupe))

        self.rsi_cache_store = RSICacheStore()
        self.stats_cache_store = StatsCacheStore()
        self.lookup = RSIProfileLookup(cache_store=self.rsi_cache_store)
        self.event_db = EventDatabase()
        # Future history search view can query self.event_db directly.
        self.image_disk_cache = ImageDiskCache()
        self.events: List[CrimeEvent] = []
        self.row_widgets: Dict[int, Dict[str, tk.Widget]] = {}
        self.player_image_cache: Dict[Tuple[str, int, int], object] = {}
        self.org_image_cache: Dict[Tuple[str, int, int], object] = {}
        self.image_lock = threading.Lock()

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("1320x760")
        self.root.minsize(1120, 660)
        self.root.configure(bg=self.BG)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.theme_var = tk.StringVar(value=self.settings.theme_name)
        self.game_folder_var = tk.StringVar(value=self.settings.game_folder_path)
        self.dedupe_var = tk.IntVar(value=self.settings.dedupe_seconds)
        self.search_var = tk.StringVar(value="")
        self.search_placeholder_active = True
        self.auto_parse_enabled = True
        self.auto_parse_job: Optional[str] = None
        self.auto_parse_interval_ms = 2000
        self.last_loaded_keys: set[Tuple[str, str, str]] = set()
        self.file_scan_state: Dict[str, Tuple[float, int]] = {}
        self.file_event_cache: Dict[str, List[CrimeEvent]] = {}
        self.org_lookup_running = False
        self.sort_column = "time"
        self.sort_reverse = True
        self.current_tab = "home"
        self._stats_cache: Optional[Dict[str, object]] = None
        self._stats_dirty = True
        self._events_signature = ""

        self._setup_theme()
        self._build_ui()
        self._prime_cached_images()
        self._run_auto_parse_cycle()


    @staticmethod
    def _is_redacted_org_logo(url: str) -> bool:
        return bool(url) and REDACTED_ORG_LOGO_NAME in url

    def _is_unknown_org(self, event: CrimeEvent) -> bool:
        return (
            not (event.organization or "").strip()
            or self._is_redacted_org_logo(event.organization_logo_url)
            or (event.organization or "").strip().casefold() == "redacted"
            or (event.organization or "").strip().casefold() == "unknown"
        )

    def _display_org_name(self, event: CrimeEvent) -> str:
        return "Unknown" if self._is_unknown_org(event) else (event.organization or "").strip()

    def _org_search_key(self, event: CrimeEvent) -> str:
        return "__unknown_org__" if self._is_unknown_org(event) else self._display_org_name(event)


    def _display_org_logo_url(self, event: CrimeEvent) -> str:
        return DEFAULT_ORG_LOGO_URL if self._is_unknown_org(event) else (event.organization_logo_url or DEFAULT_ORG_LOGO_URL)

    def colors(self) -> Dict[str, str]:
        return THEME_COLORS.get(self.theme_var.get(), THEME_COLORS[DEFAULT_THEME])

    def _setup_theme(self) -> None:
        colors = self.colors()
        self.ACCENT = colors["accent"]
        self.ACCENT_BRIGHT = colors["bright"]
        self.ACCENT_GLOW = colors["glow"]
        self.title_font = ("Segoe UI Semibold", 11)
        self.body_font = ("Segoe UI", 10)
        self.small_font = ("Segoe UI", 9)
        self.hero_font = ("Segoe UI", 21, "bold")
        if ttk is not None:
            style = ttk.Style()
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
            style.configure(
                "Dark.Vertical.TScrollbar",
                troughcolor=self.PANEL,
                background=self.PANEL_SOFT,
                bordercolor=self.ACCENT,
                arrowcolor=self.TEXT,
                lightcolor=self.PANEL_SOFT,
                darkcolor=self.PANEL_SOFT,
            )

    def _prime_cached_images(self) -> None:
        """Warm in-memory image caches from disk-backed RSI cache for faster first paint."""
        for org_name, org_url, player_avatar_url, org_logo_url, org_state in getattr(self.lookup, "_cache", {}).values():
            if player_avatar_url:
                try:
                    self._get_image(player_avatar_url, self.AVATAR_SIZE, self.AVATAR_SIZE, radius=8, cache_kind="player")
                except Exception:
                    pass
            if org_logo_url:
                try:
                    self._get_image(org_logo_url, self.AVATAR_SIZE, self.AVATAR_SIZE, radius=8, cache_kind="org")
                except Exception:
                    pass
            try:
                self._get_image(DEFAULT_LOGO_URL, 120, 120, radius=28, cache_kind="org")
            except Exception:
                pass



    def _stats_rows(self, key: str):
        grouped_events: Dict[tuple, str] = {}

        for event in self.events:
            minute_key = event.timestamp_utc.strftime("%Y-%m-%d %H:%M")
            if key == "encountered":
                group_key = (event.player.casefold(), minute_key)
                grouped_events[group_key] = event.player
            elif key == "encountered_org":
                org = self._display_org_name(event)
                if not org:
                    continue
                # Deduplicate compounded crimes from the same player within the same minute,
                # while still counting different players from the same org separately.
                group_key = (org.casefold(), event.player.casefold(), minute_key)
                grouped_events[group_key] = org
            elif key == "killed_by":
                if event.target_type != "self":
                    continue
                group_key = (event.player.casefold(), minute_key)
                grouped_events[group_key] = event.player
            elif key == "killed":
                if event.target_type != "other":
                    continue
                group_key = (event.player.casefold(), minute_key)
                grouped_events[group_key] = event.player
            elif key == "crime":
                group_key = (event.crime.casefold(), minute_key)
                grouped_events[group_key] = event.crime
            else:
                continue

        counts: Dict[str, int] = {}
        for _, label in grouped_events.items():
            counts[label] = counts.get(label, 0) + 1

        return sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold()))


    def _events_cache_signature(self) -> str:
        payload = "|".join(
            f"{e.player.casefold()}|{e.organization.casefold()}|{e.crime.casefold()}|{e.target_type}|{e.timestamp_utc.isoformat()}"
            for e in sorted(self.events, key=lambda ev: (ev.timestamp_utc, ev.player.casefold(), ev.crime.casefold(), ev.target_type))
        )
        return hashlib.sha256(payload.encode("utf-8", errors="ignore")).hexdigest()

    def _load_stats_cache_if_valid(self) -> bool:
        try:
            signature = self._events_cache_signature()
            data = self.stats_cache_store.load()
            if data.get("signature") != signature:
                return False
            if data.get("schema_version") != 4:
                return False
            stats = data.get("stats")
            if not isinstance(stats, dict):
                return False
            required_keys = {"total_events", "total_other", "total_self", "killed", "killed_by", "encountered_org"}
            if not required_keys.issubset(stats.keys()):
                return False
            self._stats_cache = stats
            self._events_signature = signature
            self._stats_dirty = False
            return True
        except Exception:
            return False

    def _save_stats_cache(self, stats: Dict[str, object]) -> None:
        try:
            signature = self._events_cache_signature()
            self.stats_cache_store.save({
                "schema_version": 4,
                "signature": signature,
                "stats": stats,
            })
            self._events_signature = signature
        except Exception:
            pass

    def _refresh_stats(self) -> None:
        if not hasattr(self, "stats_inner"):
            return
        for child in list(self.stats_inner.winfo_children()):
            child.destroy()

        if self._stats_dirty or self._stats_cache is None:
            loaded = self._load_stats_cache_if_valid()
            if loaded and isinstance(self._stats_cache, dict) and "encountered_org" not in self._stats_cache:
                loaded = False
            if not loaded:
                stats = {
                    "total_events": len(self.events),
                    "total_other": sum(1 for e in self.events if e.target_type == "other"),
                    "total_self": sum(1 for e in self.events if e.target_type == "self"),
                    "encountered": self._stats_rows("encountered"),
                    "encountered_org": self._stats_rows("encountered_org"),
                    "killed": self._stats_rows("killed"),
                    "killed_by": self._stats_rows("killed_by"),
                }
                self._stats_cache = stats
                self._stats_dirty = False
                self._save_stats_cache(stats)

        stats = self._stats_cache or {
            "total_events": 0,
            "total_other": 0,
            "total_self": 0,
            "encountered": [],
            "encountered_org": [],
            "killed": [],
            "killed_by": [],
        }

        total_events = int(stats.get("total_events", 0))
        total_other = int(stats.get("total_other", 0))
        total_self = int(stats.get("total_self", 0))

        summary = tk.Frame(self.stats_inner, bg=self.PANEL)
        summary.pack(fill="x", pady=(0, 10))

        cards = [
            ("Total Events", str(total_events)),
            ("Against Others", str(total_other)),
            ("Against You", str(total_self)),
        ]

        for idx, (title, value) in enumerate(cards):
            card = RoundedFrame(summary, bg=self.PANEL, fill=self.PANEL_ALT, outline=self.ACCENT, radius=18, padding=10, width=180, height=84)
            card.grid(row=0, column=idx, padx=(0 if idx == 0 else 8, 0), sticky="w")
            card.grid_propagate(False)
            tk.Label(card.inner, text=title, bg=self.PANEL_ALT, fg=self.GOLD, font=("Segoe UI", 9, "bold")).pack(anchor="w")
            tk.Label(card.inner, text=value, bg=self.PANEL_ALT, fg=self.TEXT, font=("Segoe UI", 18, "bold")).pack(anchor="w", pady=(8, 0))

        sections = [
            ("Most Killed", stats.get("killed", [])),
            ("Killed You Most", stats.get("killed_by", [])),
            ("Most Encountered Org", stats.get("encountered_org", [])),
        ]

        for title, rows in sections:
            rows = rows if isinstance(rows, list) else []
            total_count = sum(int(count) for _, count in rows)
            title_text = f"{title} ({total_count})"
            panel = RoundedFrame(self.stats_inner, bg=self.PANEL, fill=self.PANEL_ALT, outline=self.ACCENT_BRIGHT, radius=20, padding=12)
            panel.pack(fill="x", pady=(0, 10))
            tk.Label(panel.inner, text=title_text, bg=self.PANEL_ALT, fg=self.GOLD, font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 8))

            if not rows:
                tk.Label(panel.inner, text="No data yet.", bg=self.PANEL_ALT, fg=self.MUTED, font=("Segoe UI", 10)).pack(anchor="w")
                continue

            for rank, item in enumerate(rows[:5], start=1):
                name, count = item
                row = tk.Frame(panel.inner, bg=self.PANEL_ALT)
                row.pack(fill="x", pady=2)
                tk.Label(row, text=f"{rank}.", bg=self.PANEL_ALT, fg=self.ACCENT_GLOW, font=("Segoe UI", 10, "bold"), width=3, anchor="w").pack(side="left")
                value_label = tk.Label(row, text=name, bg=self.PANEL_ALT, fg=self.TEXT, font=("Segoe UI", 10), anchor="w", cursor="hand2")
                value_label.pack(side="left", fill="x", expand=True)
                value_label.bind("<Button-1>", lambda _e, q=name: self.apply_filter_and_show_home(q))
                count_badge = tk.Label(row, text=str(count), bg=self.PANEL_SOFT, fg=self.TEXT, font=("Segoe UI", 9, "bold"), padx=8, pady=2, cursor="hand2")
                count_badge.pack(side="right")
                count_badge.bind("<Button-1>", lambda _e, q=name: self.apply_filter_and_show_home(q))

    def show_tab(self, tab_name: str) -> None:
        self.current_tab = tab_name
        if tab_name == "home":
            self.home_tab.lift()
        else:
            self.stats_tab.lift()
            self._refresh_stats()

        if hasattr(self, "home_nav_button"):
            for name, button in (("home", self.home_nav_button), ("stats", self.stats_nav_button)):
                button._fill = self.PANEL_ALT if name == tab_name else self.PANEL_SOFT
                button._outline = self.ACCENT_GLOW if name == tab_name else self.ACCENT
                button._hover_fill = self.PANEL_HOVER
                button._hover_outline = self.ACCENT_GLOW
                button._redraw()


    def apply_filter_and_show_home(self, query: str) -> None:
        self.search_placeholder_active = False
        lowered = str(query).strip().casefold()
        if lowered == "unknown":
            self.search_var.set("__unknown_org__")
        else:
            self.search_var.set(query)
        if hasattr(self, "search_entry"):
            self.search_entry.configure(fg=self.TEXT)
            self.search_entry.focus_set()
        self.show_tab("home")
        self.refresh_events()

    def _build_ui(self) -> None:
        self.shell = RoundedFrame(self.root, bg=self.BG, fill=self.BG, outline=self.ACCENT_BRIGHT, radius=24, padding=0)
        self.shell.pack(fill="both", expand=True, padx=6, pady=6)

        self.sidebar = RoundedFrame(self.shell.inner, bg=self.BG, fill=self.PANEL, outline=self.ACCENT_BRIGHT, radius=24, padding=14, width=200)
        self.sidebar.pack(side="left", fill="y", padx=(0, 8))
        self.sidebar.pack_propagate(False)

        self.content = tk.Frame(self.shell.inner, bg=self.BG)
        self.content.pack(side="left", fill="both", expand=True)

        self._build_sidebar()

        self.top_tabs_bar = tk.Frame(self.content, bg=self.BG)
        self.top_tabs_bar.pack(fill="x", padx=(0, 6), pady=(6, 0))

        self.home_nav_button = self._make_action_button(self.top_tabs_bar, "Home", lambda: self.show_tab("home"), width=140)
        self.home_nav_button.pack(side="left", padx=(6, 6), pady=(0, 6))
        self.stats_nav_button = self._make_action_button(self.top_tabs_bar, "Stats", lambda: self.show_tab("stats"), width=140)
        self.stats_nav_button.pack(side="left", padx=(0, 6), pady=(0, 6))

        self.tabs_host = tk.Frame(self.content, bg=self.BG)
        self.tabs_host.pack(fill="both", expand=True)

        self.home_tab = tk.Frame(self.tabs_host, bg=self.BG)
        self.stats_tab = tk.Frame(self.tabs_host, bg=self.BG)
        for frame in (self.home_tab, self.stats_tab):
            frame.place(relx=0, rely=0, relwidth=1, relheight=1)

        self._build_home_content()
        self._build_stats_content()
        self.show_tab("home")

    def _load_logo_image(self) -> None:
        image = self._get_image(DEFAULT_LOGO_URL, 120, 120, radius=28, cache_kind="org")
        if image is None:
            self.logo_label.configure(text="CrimeScanner", fg=self.TEXT, bg=self.PANEL, font=("Segoe UI", 15, "bold"))
            return
        self.logo_label.configure(image=image, text="", width=120, height=120)
        self.logo_label.image = image

    def _make_action_button(self, parent: tk.Widget, text: str, command, width: int = 150) -> RoundedButton:
        return RoundedButton(
            parent,
            text=text,
            command=command,
            width=width,
            bg=self.BG,
            fill=self.PANEL_SOFT,
            outline=self.ACCENT,
            hover_fill=self.PANEL_HOVER,
            hover_outline=self.ACCENT_GLOW,
            text_color=self.TEXT,
        )

    def _panel(self, parent: tk.Widget, title: Optional[str] = None, radius: int = 22, padding: int = 14) -> RoundedFrame:
        outer = RoundedFrame(parent, bg=self.BG, fill=self.PANEL, outline=self.ACCENT_BRIGHT, radius=radius, padding=padding)
        if title:
            tk.Label(outer.inner, text=title, bg=self.PANEL, fg=self.GOLD, font=self.title_font).pack(anchor="nw", pady=(2, 10))
        return outer

    def _build_sidebar(self) -> None:
        logo_frame = tk.Frame(self.sidebar.inner, bg=self.PANEL)
        logo_frame.pack(pady=(12, 18))
        self.logo_label = tk.Label(logo_frame, bg=self.PANEL)
        self.logo_label.pack()
        self._load_logo_image()

        tk.Label(self.sidebar.inner, text="CrimeScanner", bg=self.PANEL, fg=self.TEXT, font=self.hero_font).pack(pady=(0, 18))

        self.set_folder_button = self._make_action_button(self.sidebar.inner, "Set Game Folder", self.choose_game_folder, width=160)
        self.set_folder_button.pack(pady=8)
        self._refresh_game_folder_button()

        self.sidebar_spacer = tk.Frame(self.sidebar.inner, bg=self.PANEL)
        self.sidebar_spacer.pack(fill="both", expand=True)

        self.clear_cache_button = self._make_action_button(self.sidebar.inner, "Clear Cache", self.clear_cache, width=160)
        self.clear_cache_button.pack(side="bottom", pady=(8, 6))


    def _matches_search(self, event: CrimeEvent) -> bool:
        query = self.search_var.get().strip().casefold()
        if self.search_placeholder_active or not query:
            return True
        if query == "__unknown_org__":
            return self._is_unknown_org(event)
        haystacks = [
            event.player,
            self._display_org_name(event),
            event.crime,
            event.display_time,
        ]
        return any(query in (text or "").casefold() for text in haystacks)

    def _filtered_sorted_events(self) -> List[CrimeEvent]:
        return [event for event in self._sorted_events() if self._matches_search(event)]

    def _on_search_changed(self, *_args: object) -> None:
        self._update_clear_button()
        if not self.search_placeholder_active:
            self.refresh_events()


    def _set_search_placeholder(self) -> None:
        self.search_placeholder_active = True
        self.search_entry.configure(fg=self.MUTED)
        self.search_var.set("Search ...")

    def _clear_search(self) -> None:
        self.search_placeholder_active = False
        self.search_var.set("")
        self.search_entry.configure(fg=self.TEXT)
        self.search_entry.focus_set()
        self.refresh_events()

    def _on_search_focus_in(self, _event: object = None) -> None:
        if self.search_placeholder_active:
            self.search_placeholder_active = False
            self.search_var.set("")
            self.search_entry.configure(fg=self.TEXT)

    def _on_search_focus_out(self, _event: object = None) -> None:
        if not self.search_var.get().strip():
            self._set_search_placeholder()

    def _update_clear_button(self) -> None:
        if not hasattr(self, "search_clear_button"):
            return
        show = (not self.search_placeholder_active) and bool(self.search_var.get())
        self.search_clear_button.configure(text="✕" if show else "")

    def _build_home_content(self) -> None:
        self.home_tab.grid_columnconfigure(0, weight=1)
        self.home_tab.grid_columnconfigure(1, weight=0)
        self.home_tab.grid_rowconfigure(1, weight=1)

        title_bar = tk.Frame(self.home_tab, bg=self.BG)
        title_bar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(4, 10))
        title_bar.grid_columnconfigure(0, weight=1)

        title_stack = tk.Frame(title_bar, bg=self.BG)
        title_stack.grid(row=0, column=0, sticky="w")

        search_row = tk.Frame(title_stack, bg=self.BG)
        search_row.pack(anchor="w", pady=(0, 0))

        search_container = RoundedFrame(
            search_row,
            bg=self.BG,
            fill=self.PANEL,
            outline=self.ACCENT,
            radius=14,
            padding=6,
            width=300,
            height=36,
        )
        search_container.pack(side="left")

        search_container.inner.grid_columnconfigure(0, weight=1)
        search_container.inner.grid_columnconfigure(1, weight=0)

        self.search_entry = tk.Entry(
            search_container.inner,
            textvariable=self.search_var,
            bg=self.PANEL,
            fg=self.TEXT,
            insertbackground=self.TEXT,
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=("Segoe UI", 10),
        )
        self.search_entry.grid(row=0, column=0, sticky="ew")

        self.search_clear_button = tk.Label(
            search_container.inner,
            text="",
            bg=self.PANEL,
            fg=self.MUTED,
            cursor="hand2",
            font=("Segoe UI", 10, "bold"),
            width=2,
        )
        self.search_clear_button.grid(row=0, column=1, sticky="e", padx=(6, 0))
        self.search_clear_button.bind("<Button-1>", lambda _e: self._clear_search())

        self.search_entry.bind("<FocusIn>", self._on_search_focus_in)
        self.search_entry.bind("<FocusOut>", self._on_search_focus_out)
        self.search_var.trace_add("write", self._on_search_changed)
        self._set_search_placeholder()


        self.feed_outer = self._panel(self.home_tab, title=None, radius=24, padding=12)
        self.feed_outer.grid(row=1, column=0, columnspan=2, sticky="nsew", padx=(10, 10), pady=(0, 10))
        self.feed_outer.inner.grid_rowconfigure(0, weight=1)
        self.feed_outer.inner.grid_columnconfigure(0, weight=1)

        list_host = tk.Frame(self.feed_outer.inner, bg=self.PANEL)
        list_host.pack(fill="both", expand=True)
        list_host.grid_rowconfigure(0, weight=1)
        list_host.grid_columnconfigure(0, weight=1)

        self.feed_canvas = tk.Canvas(list_host, bg=self.PANEL, highlightthickness=0, bd=0)
        self.feed_canvas.grid(row=0, column=0, sticky="nsew")
        self.feed_scroll = ttk.Scrollbar(list_host, orient="vertical", command=self.feed_canvas.yview, style="Dark.Vertical.TScrollbar") if ttk is not None else tk.Scrollbar(list_host, orient="vertical", command=self.feed_canvas.yview)
        self.feed_scroll.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        self.feed_canvas.configure(yscrollcommand=self.feed_scroll.set)
        self.feed_inner = tk.Frame(self.feed_canvas, bg=self.PANEL)
        self.feed_window = self.feed_canvas.create_window((0, 0), window=self.feed_inner, anchor="nw")
        self.feed_inner.bind("<Configure>", self._sync_feed_scrollregion)
        self.feed_canvas.bind("<Configure>", self._sync_feed_width)
        self._bind_mousewheel_global()

        self.empty_label = tk.Label(self.feed_canvas, text="No logged events yet. Set the game folder and turn auto-parse on.", bg=self.PANEL, fg=self.MUTED, font=self.body_font, justify="center")


    def _build_stats_content(self) -> None:
        self.stats_outer = self._panel(self.stats_tab, title=None, radius=24, padding=12)
        self.stats_outer.pack(fill="both", expand=True, padx=10, pady=(4, 10))

        stats_host = tk.Frame(self.stats_outer.inner, bg=self.PANEL)
        stats_host.pack(fill="both", expand=True)

        self.stats_canvas = tk.Canvas(stats_host, bg=self.PANEL, highlightthickness=0, bd=0)
        self.stats_scroll = ttk.Scrollbar(stats_host, orient="vertical", command=self.stats_canvas.yview, style="Dark.Vertical.TScrollbar") if ttk is not None else tk.Scrollbar(stats_host, orient="vertical", command=self.stats_canvas.yview)
        self.stats_canvas.configure(yscrollcommand=self.stats_scroll.set)
        self.stats_scroll.pack(side="right", fill="y")
        self.stats_canvas.pack(side="left", fill="both", expand=True)

        self.stats_inner = tk.Frame(self.stats_canvas, bg=self.PANEL)
        self.stats_window = self.stats_canvas.create_window((0, 0), window=self.stats_inner, anchor="nw")
        self.stats_inner.bind("<Configure>", lambda _e: self.stats_canvas.configure(scrollregion=self.stats_canvas.bbox("all")))
        self.stats_canvas.bind("<Configure>", lambda e: self.stats_canvas.itemconfigure(self.stats_window, width=getattr(e, "width", self.stats_canvas.winfo_width())))

    def _sync_feed_scrollregion(self, _event: object = None) -> None:
        self.feed_canvas.configure(scrollregion=self.feed_canvas.bbox("all"))

    def _sync_feed_width(self, event: object) -> None:
        self.feed_canvas.itemconfigure(self.feed_window, width=getattr(event, "width", self.feed_canvas.winfo_width()))


    def _bind_mousewheel_global(self) -> None:
        self.root.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self.root.bind_all("<Button-4>", self._on_mousewheel_linux, add="+")
        self.root.bind_all("<Button-5>", self._on_mousewheel_linux, add="+")

    def _pointer_over_scroll_area(self):
        try:
            widget = self.root.winfo_containing(self.root.winfo_pointerx(), self.root.winfo_pointery())
        except Exception:
            return None
        while widget is not None:
            if widget is self.feed_canvas or widget is self.feed_inner or widget is self.feed_scroll:
                return "home"
            if hasattr(self, "stats_canvas") and (widget is self.stats_canvas or widget is self.stats_inner or widget is self.stats_scroll):
                return "stats"
            widget = widget.master
        return None

    def _on_mousewheel(self, event: object) -> str | None:
        area = self._pointer_over_scroll_area()
        if area is None:
            return None
        delta = getattr(event, "delta", 0)
        if delta:
            step = -1 if delta > 0 else 1
            target = self.feed_canvas if area == "home" else self.stats_canvas
            target.yview_scroll(step, "units")
            return "break"
        return None

    def _on_mousewheel_linux(self, event: object) -> str | None:
        area = self._pointer_over_scroll_area()
        if area is None:
            return None
        num = getattr(event, "num", 0)
        target = self.feed_canvas if area == "home" else self.stats_canvas
        if num == 4:
            target.yview_scroll(-1, "units")
            return "break"
        if num == 5:
            target.yview_scroll(1, "units")
            return "break"
        return None

    def _refetch_visible_metadata(self) -> None:
        if not self.events:
            return
        self._start_org_lookup()

    def clear_cache(self) -> None:
        try:
            # Clear persisted cache files
            self.rsi_cache_store.clear()
            self.image_disk_cache.clear()
            try:
                self.stats_cache_store.path.unlink(missing_ok=True)
            except Exception:
                pass

            # Clear in-memory cache structures
            self.lookup._cache.clear()
            if hasattr(self.lookup, "_disk_cache"):
                self.lookup._disk_cache.clear()
            self.player_image_cache.clear()
            self.org_image_cache.clear()
            self.file_scan_state.clear()
            self.file_event_cache.clear()
            self._stats_cache = None
            self._stats_dirty = True

            # Reset loaded-state bookkeeping so fresh data rebuilds cleanly
            self.last_loaded_keys.clear()

            # Refresh button state and silently repopulate from current sources
            self._refresh_game_folder_button()
            self.refresh_events()
            self._refresh_stats()
            self._run_auto_parse_cycle()
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Could not clear cache.\n\n{exc}")


    def _compact_runtime_caches(self) -> None:
        max_cache_items = 512
        if len(self.player_image_cache) > max_cache_items:
            self.player_image_cache = dict(list(self.player_image_cache.items())[-max_cache_items:])
        if len(self.org_image_cache) > max_cache_items:
            self.org_image_cache = dict(list(self.org_image_cache.items())[-max_cache_items:])

    def _file_signature(self, path_obj: Path) -> Optional[Tuple[float, int]]:
        try:
            stat = path_obj.stat()
            return (stat.st_mtime, stat.st_size)
        except Exception:
            return None

    def _parse_file_cached(self, parser: GameLogParser, path_obj: Path) -> List[CrimeEvent]:
        cache_key = str(path_obj)
        signature = self._file_signature(path_obj)
        if signature is None:
            self.file_scan_state.pop(cache_key, None)
            self.file_event_cache.pop(cache_key, None)
            return []

        if self.file_scan_state.get(cache_key) == signature and cache_key in self.file_event_cache:
            return self.file_event_cache[cache_key]

        events = parser.parse(path_obj)
        self.file_scan_state[cache_key] = signature
        self.file_event_cache[cache_key] = events
        return events

    def _purge_missing_file_caches(self, active_paths: List[Path]) -> None:
        active = {str(path_obj) for path_obj in active_paths}
        for key in list(self.file_event_cache.keys()):
            if key not in active:
                self.file_event_cache.pop(key, None)
                self.file_scan_state.pop(key, None)

    def get_log_path(self) -> Optional[Path]:
        folder_text = self.game_folder_var.get().strip().strip('"')
        if not folder_text:
            return None
        folder_path = Path(folder_text)
        if folder_path.is_file() and folder_path.name.lower() == "game.log":
            return folder_path
        return folder_path / "Game.log"

    def get_backup_dir(self) -> Optional[Path]:
        folder_text = self.game_folder_var.get().strip().strip('"')
        if not folder_text:
            return None
        folder_path = Path(folder_text)
        if folder_path.is_file():
            folder_path = folder_path.parent
        return folder_path / "logbackups"


    def _refresh_game_folder_button(self) -> None:
        if hasattr(self, "set_folder_button"):
            has_path = bool(self.game_folder_var.get().strip())
            self.set_folder_button._text = "Change Game Folder" if has_path else "Set Game Folder"
            self.set_folder_button._redraw()

    def choose_game_folder(self) -> None:
        initial_dir = self.game_folder_var.get().strip() or str(Path.home())
        chosen = filedialog.askdirectory(title="Select Star Citizen game folder", initialdir=initial_dir)
        if chosen:
            self.game_folder_var.set(chosen)
            self.settings.game_folder_path = chosen
            self.save_settings()
            self._refresh_game_folder_button()
            self._run_auto_parse_cycle()

    def toggle_auto_parse(self) -> None:
        self._run_auto_parse_cycle()

    def _update_auto_parse_button(self) -> None:
        return

    def _stop_auto_parse(self, status_message: Optional[str] = None) -> None:
        if self.auto_parse_job is not None:
            try:
                self.root.after_cancel(self.auto_parse_job)
            except Exception:
                pass
            self.auto_parse_job = None

    def _schedule_auto_parse(self) -> None:
        self.auto_parse_job = self.root.after(self.auto_parse_interval_ms, self._run_auto_parse_cycle)

    def _run_auto_parse_cycle(self) -> None:
        self.auto_parse_job = None
        log_path = self.get_log_path()
        if log_path is None:
            self._schedule_auto_parse()
            return
        if not log_path.exists():
            self._schedule_auto_parse()
            return

        try:
            parser = GameLogParser(dedupe_seconds=int(self.dedupe_var.get()))
            parsed_events = []
            seen_full: set[Tuple[str, str, str, str]] = set()
            active_paths: List[Path] = []

            def add_events_from(path_obj: Path) -> None:
                nonlocal parsed_events, seen_full
                try:
                    active_paths.append(path_obj)
                    for event in self._parse_file_cached(parser, path_obj):
                        full_key = (
                            event.player.casefold(),
                            event.crime.casefold(),
                            event.target_type,
                            event.timestamp_utc.isoformat(),
                        )
                        if full_key not in seen_full:
                            seen_full.add(full_key)
                            parsed_events.append(event)
                except Exception:
                    pass

            backup_dir = self.get_backup_dir()
            if backup_dir is not None and backup_dir.exists():
                for backup_file in sorted(backup_dir.glob("*.log")):
                    add_events_from(backup_file)

            add_events_from(log_path)
            self._purge_missing_file_caches(active_paths)
        except Exception:
            self._schedule_auto_parse()
            return

        parsed_keys = {(e.player.casefold(), e.crime.casefold(), e.timestamp_utc.isoformat()) for e in parsed_events}

        if parsed_keys != self.last_loaded_keys:
            # Keep parser output neutral; unknown org handling stays reversible
            # through cache enrichment and live lookup normalization.
            self._apply_cached_metadata(parsed_events)
            self.events = parsed_events
            self.event_db.upsert_events(parsed_events)
            self._stats_dirty = True
            self.refresh_events()
            self._refresh_stats()
            self.last_loaded_keys = parsed_keys
            self._start_org_lookup()

        self.settings.game_folder_path = self.game_folder_var.get().strip()
        self.settings.dedupe_seconds = int(self.dedupe_var.get())
        self.save_settings()
        self._compact_runtime_caches()
        self._schedule_auto_parse()


    def _apply_cached_metadata(self, events: List[CrimeEvent]) -> None:
        cache = getattr(self.lookup, "_cache", {})
        if not isinstance(cache, dict):
            return
        for event in events:
            cached = cache.get(event.player)
            if not cached:
                continue
            org_name, org_url, player_avatar_url, org_logo_url, org_state = cached
            if player_avatar_url:
                event.player_avatar_url = player_avatar_url
            if org_state == "unknown":
                event.organization = "Unknown"
                event.organization_url = ""
                event.organization_logo_url = DEFAULT_ORG_LOGO_URL
            else:
                event.organization = org_name
                event.organization_logo_url = org_logo_url or DEFAULT_ORG_LOGO_URL
                event.organization_url = org_url or ""

    def _sort_key(self, event: CrimeEvent):
        if self.sort_column == "player":
            return event.player.casefold()
        if self.sort_column == "org":
            return (event.organization or "").casefold()
        if self.sort_column == "crime":
            return event.crime.casefold()
        return event.timestamp_utc

    def _sorted_events(self) -> List[CrimeEvent]:
        return sorted(self.events, key=self._sort_key, reverse=self.sort_reverse)

    def sort_by(self, column: str) -> None:
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = True if column == "time" else False
        self.refresh_events()

    def _make_avatar_widget(self, parent: tk.Widget, image_url: str, fallback_url: str, size: int, radius: int, role: str = "player") -> tk.Label:
        holder = tk.Label(parent, width=size, height=size, bg=self.PANEL_SOFT, bd=0, highlightthickness=1, highlightbackground=self.ACCENT)
        holder._image_role = role
        holder._img_url = ""
        return holder

    def refresh_events(self) -> None:
        for child in list(self.feed_inner.winfo_children()):
            child.destroy()
        self.row_widgets.clear()

        filtered_events = self._filtered_sorted_events()
        if not self.events:
            self.empty_label.configure(text="No logged events yet. Set the game folder and turn auto-parse on.")
            self.empty_label.pack(pady=30)
            return
        if not filtered_events:
            self.empty_label.configure(text="No matching results for the current search.")
            self.empty_label.pack(pady=30)
            return
        self.empty_label.pack_forget()

        col_sizes = (260, 300, 240, 180)

        header = tk.Frame(self.feed_inner, bg=self.PANEL)
        header.pack(fill="x", padx=4, pady=(0, 6))

        for col, size in enumerate(col_sizes):
            header.grid_columnconfigure(col, minsize=size)

        def make_header(col: int, text: str, key: str, padx=(4, 4)):
            label_text = text
            if self.sort_column == key:
                label_text += " ▼" if self.sort_reverse else " ▲"
            lbl = tk.Label(
                header,
                text=label_text,
                bg=self.PANEL,
                fg=self.GOLD,
                font=("Segoe UI", 9, "bold"),
                cursor="hand2",
                anchor="w",
            )
            lbl.grid(row=0, column=col, sticky="w", padx=padx)
            lbl.bind("<Button-1>", lambda _e, c=key: self.sort_by(c))

        make_header(0, "PLAYER", "player", padx=(12, 4))
        make_header(1, "ORG", "org")
        make_header(2, "CRIME", "crime")
        make_header(3, "TIME", "time", padx=(4, 12))

        for event in filtered_events:
            fill = self.OTHER_FILL if event.target_type == "other" else self.SELF_FILL
            border = self.OTHER_BORDER if event.target_type == "other" else self.SELF_BORDER

            row_wrap = tk.Frame(self.feed_inner, bg=self.PANEL)
            row_wrap.pack(fill="x", expand=False, padx=2, pady=3)

            row_canvas = tk.Canvas(row_wrap, bg=self.PANEL, highlightthickness=0, bd=0, height=self.ROW_HEIGHT)
            row_canvas.pack(fill="x", expand=True)

            total_width = sum(col_sizes) + 32
            width = max(total_width, self.feed_canvas.winfo_width() - 24)
            r = 12
            points = [
                1 + r, 1,
                width - 1 - r, 1,
                width - 1, 1,
                width - 1, 1 + r,
                width - 1, self.ROW_HEIGHT - 1 - r,
                width - 1, self.ROW_HEIGHT - 1,
                width - 1 - r, self.ROW_HEIGHT - 1,
                1 + r, self.ROW_HEIGHT - 1,
                1, self.ROW_HEIGHT - 1,
                1, self.ROW_HEIGHT - 1 - r,
                1, 1 + r,
                1, 1,
            ]
            row_canvas.create_polygon(points, smooth=True, fill=fill, outline=border, width=1.4)

            row = tk.Frame(row_canvas, bg=fill, height=self.ROW_HEIGHT - 10)
            row.pack_propagate(False)
            row_canvas.create_window(8, 5, anchor="nw", window=row, width=width - 16, height=self.ROW_HEIGHT - 10)

            for col, size in enumerate(col_sizes):
                row.grid_columnconfigure(col, minsize=size)
            row.grid_rowconfigure(0, weight=1)

            player_cell = tk.Frame(row, bg=fill, width=col_sizes[0], height=self.ROW_HEIGHT - 10)
            org_cell = tk.Frame(row, bg=fill, width=col_sizes[1], height=self.ROW_HEIGHT - 10)
            crime_cell = tk.Frame(row, bg=fill, width=col_sizes[2], height=self.ROW_HEIGHT - 10)
            time_cell = tk.Frame(row, bg=fill, width=col_sizes[3], height=self.ROW_HEIGHT - 10)
            for cell in (player_cell, org_cell, crime_cell, time_cell):
                cell.grid_propagate(False)

            player_cell.grid(row=0, column=0, sticky="nsew", padx=(8, 4))
            org_cell.grid(row=0, column=1, sticky="nsew", padx=4)
            crime_cell.grid(row=0, column=2, sticky="nsew", padx=4)
            time_cell.grid(row=0, column=3, sticky="nsew", padx=(4, 8))

            player_inner = tk.Frame(player_cell, bg=fill)
            player_inner.place(relx=0.0, rely=0.5, anchor="w")
            player_avatar = self._make_avatar_widget(player_inner, event.player_avatar_url, DEFAULT_PLAYER_AVATAR_URL, self.AVATAR_SIZE, 8, role="player")
            player_avatar.pack(side="left", padx=(0, 8))
            player_link = tk.Label(
                player_inner,
                text=event.player,
                bg=fill,
                fg=self.ACCENT_GLOW,
                cursor="arrow",
                font=("Segoe UI Semibold", 10),
                anchor="w",
            )
            player_link.pack(side="left", anchor="w")

            org_avatar = None
            org_label = None
            org_inner = tk.Frame(org_cell, bg=fill)
            org_inner.place(relx=0.0, rely=0.5, anchor="w")
            org_avatar = self._make_avatar_widget(org_inner, self._display_org_logo_url(event), DEFAULT_ORG_LOGO_URL, self.AVATAR_SIZE, 8, role="org")
            org_avatar.pack(side="left", padx=(0, 8))
            org_label = tk.Label(
                org_inner,
                text=self._display_org_name(event),
                bg=fill,
                fg=self.ACCENT_GLOW,
                cursor="arrow",
                font=("Segoe UI Semibold", 10),
                anchor="w",
                justify="left",
                wraplength=250,
            )
            org_label.pack(side="left", anchor="w")
            org_empty = None

            tk.Label(
                crime_cell,
                text=event.crime,
                bg=fill,
                fg=self.TEXT,
                font=("Segoe UI Semibold", 10),
                anchor="w",
                justify="left",
                wraplength=220,
            ).place(relx=0.0, rely=0.5, anchor="w")

            tk.Label(
                time_cell,
                text=event.display_time,
                bg=fill,
                fg=self.MUTED,
                font=("Segoe UI", 9),
                anchor="w",
                justify="left",
                wraplength=160,
            ).place(relx=0.0, rely=0.5, anchor="w")

            self.row_widgets[id(event)] = {
                "row": row,
                "org": org_label,
                "player_label": player_link,
                "player_avatar": player_avatar,
                "org_avatar": org_avatar,
                "org_col": org_cell,
                "org_body": org_inner,
                "org_empty": org_empty,
                "fill": fill,
            }
            self.root.after_idle(lambda e=event: self._enhance_row_after_render(e))

    def _start_org_lookup(self) -> None:
        if self.org_lookup_running:
            return
        self.org_lookup_running = True

        def worker() -> None:
            any_changed = False
            try:
                for idx, event in enumerate(self.events):
                    org_name, org_url, player_avatar_url, org_logo_url, org_state = self.lookup.lookup_player_details(event.player)
                    if org_state == "unknown":
                        org_name = "Unknown"
                        org_url = ""
                        org_logo_url = DEFAULT_ORG_LOGO_URL

                    changed = False
                    if player_avatar_url and player_avatar_url != event.player_avatar_url:
                        event.player_avatar_url = player_avatar_url
                        changed = True
                    if org_name and org_name != event.organization:
                        event.organization = org_name
                        changed = True
                    if org_url != event.organization_url:
                        event.organization_url = org_url
                        changed = True
                    if org_logo_url and org_logo_url != event.organization_logo_url:
                        event.organization_logo_url = org_logo_url
                        changed = True

                    if changed:
                        any_changed = True
                        self.event_db.update_event_metadata(event)
                        self.root.after(0, lambda i=idx, e=event: self._apply_org_update(i, e))
            finally:
                self.org_lookup_running = False

            if any_changed:
                self._stats_dirty = True
                self.root.after(0, self._refresh_stats)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_org_update(self, idx: int, event: CrimeEvent) -> None:
        widgets = self.row_widgets.get(id(event))
        if not widgets:
            return
        org_label = widgets.get("org")
        org_avatar = widgets.get("org_avatar")
        org_body = widgets.get("org_body")
        org_empty = widgets.get("org_empty")
        fill = widgets.get("fill", self.PANEL_ALT)

        if org_body is None:
            org_body = tk.Frame(widgets["org_col"], bg=fill)
            org_body.place(relx=0.0, rely=0.5, anchor="w")
            widgets["org_body"] = org_body
        if org_label is None and org_body is not None:
            org_avatar = self._make_avatar_widget(org_body, self._display_org_logo_url(event), DEFAULT_ORG_LOGO_URL, self.AVATAR_SIZE, 8, role="org")
            org_avatar.pack(side="left", padx=(0, 8))
            org_label = tk.Label(
                org_body,
                text=self._display_org_name(event),
                bg=fill,
                fg=self.ACCENT_GLOW,
                cursor="arrow",
                font=("Segoe UI Semibold", 10),
                anchor="w",
                justify="left",
                wraplength=250,
            )
            org_label.pack(side="left", anchor="w")
            widgets["org"] = org_label
            widgets["org_avatar"] = org_avatar
        if org_label is not None:
            org_label.configure(text=self._display_org_name(event), bg=fill)
            self._enable_link_label(org_label, "" if self._is_unknown_org(event) else event.organization_url)
        player_label = widgets.get("player_label")
        if player_label is not None:
            self._enable_link_label(player_label, event.player_url)
        self._queue_image_update(widgets["player_avatar"], event.player_avatar_url, DEFAULT_PLAYER_AVATAR_URL, self.AVATAR_SIZE, 8)
        if org_avatar is not None:
            self._queue_image_update(org_avatar, self._display_org_logo_url(event), DEFAULT_ORG_LOGO_URL, self.AVATAR_SIZE, 8)

    def _get_image(self, url: str, width: int, height: int, radius: int = 10, cache_kind: str = "player"):
        if Image is None or ImageTk is None or ImageDraw is None:
            return None
        cache_key = (url or "", width, height)
        cache = self.player_image_cache if cache_kind == "player" else self.org_image_cache
        with self.image_lock:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached
        try:
            data = None
            if url:
                data = self.image_disk_cache.read_bytes(url)
            if data is None:
                request = Request(url, headers={"User-Agent": USER_AGENT})
                with urlopen(request, timeout=6) as response:
                    data = response.read()
                if url and data:
                    self.image_disk_cache.write_bytes(url, data)
            image = Image.open(BytesIO(data)).convert("RGBA")
            image = image.resize((width, height))
            mask = Image.new("L", (width, height), 0)
            draw = ImageDraw.Draw(mask)
            draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=radius, fill=255)
            rounded = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            rounded.paste(image, (0, 0), mask=mask)
            photo = ImageTk.PhotoImage(rounded)
            with self.image_lock:
                cache[cache_key] = photo
            return photo
        except Exception:
            return None

    def _enable_link_label(self, label: tk.Label, url: str) -> None:
        label.unbind("<Button-1>")
        if url:
            label.configure(cursor="hand2", fg=self.ACCENT_GLOW)
            label.bind("<Button-1>", lambda _e, link=url: self.open_url(link))
        else:
            label.configure(cursor="arrow")

    def _enhance_row_after_render(self, event: CrimeEvent) -> None:
        widgets = self.row_widgets.get(id(event))
        if not widgets:
            return
        player_label = widgets.get("player_label")
        if player_label is not None:
            self._enable_link_label(player_label, event.player_url)
        player_avatar = widgets.get("player_avatar")
        if player_avatar is not None:
            self._queue_image_update(player_avatar, event.player_avatar_url, DEFAULT_PLAYER_AVATAR_URL, self.AVATAR_SIZE, 8)
        org_label = widgets.get("org")
        if org_label is not None:
            self._enable_link_label(org_label, "" if self._is_unknown_org(event) else event.organization_url)
        org_avatar = widgets.get("org_avatar")
        if org_avatar is not None:
            self._queue_image_update(org_avatar, self._display_org_logo_url(event), DEFAULT_ORG_LOGO_URL, self.AVATAR_SIZE, 8)

    def _queue_image_update(self, label: tk.Label, image_url: str, fallback_url: str, width: int, radius: int) -> None:
        expected_url = image_url or fallback_url
        role = getattr(label, "_image_role", "player")
        current_url = getattr(label, "_img_url", None)
        if current_url == expected_url and getattr(label, "image", None):
            return

        label._img_url = expected_url
        cache = self.player_image_cache if role == "player" else self.org_image_cache
        cached_photo = cache.get((expected_url or "", width, width))
        if cached_photo is not None:
            self._apply_label_image(label, cached_photo)
            return

        def worker() -> None:
            photo = self._get_image(expected_url, width, width, radius=radius, cache_kind=role)
            if photo is None and fallback_url and fallback_url != image_url:
                photo = self._get_image(fallback_url, width, width, radius=radius, cache_kind=role)

            if photo is not None:
                def apply_if_still_valid() -> None:
                    current_label_url = getattr(label, "_img_url", None)
                    current_role = getattr(label, "_image_role", None)
                    if current_label_url == expected_url and current_role == role:
                        self._apply_label_image(label, photo)
                self.root.after(0, apply_if_still_valid)

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def _apply_label_image(label: tk.Label, photo: object) -> None:
        label.configure(image=photo, text="")
        label.image = photo

    def export_csv(self) -> None:
        if not self.events:
            messagebox.showinfo(APP_NAME, "There is no parsed data to export yet.")
            return
        destination = filedialog.asksaveasfilename(title="Export CSV", defaultextension=".csv", filetypes=[("CSV files", "*.csv")], initialfile="crime_table.csv")
        if not destination:
            return
        with open(destination, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["Player", "Crime", "Time", "Organization", "Player URL", "Organization URL", "Player Avatar URL", "Organization Logo URL"])
            for event in self.events:
                writer.writerow([event.player, event.crime, event.display_time, event.organization, event.player_url, event.organization_url, event.player_avatar_url, event.organization_logo_url])
        messagebox.showinfo(APP_NAME, "CSV exported successfully.")


    @staticmethod
    def open_url(url: str) -> None:
        if url:
            webbrowser.open(url)

    def save_settings(self) -> None:
        self.settings.theme_name = self.theme_var.get()
        self.settings.dedupe_seconds = int(self.dedupe_var.get())
        self.settings.game_folder_path = self.game_folder_var.get().strip()
        self.settings_store.save(self.settings)

    def on_close(self) -> None:
        if self.auto_parse_job is not None:
            try:
                self.root.after_cancel(self.auto_parse_job)
            except Exception:
                pass
        self.save_settings()
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Parse Star Citizen game.log crime entries in a desktop GUI.")
    parser.add_argument("log_path", nargs="?", help="Optional path to game.log")
    parser.add_argument("--window-seconds", type=int, default=None, help="Duplicate suppression window in seconds")
    args = parser.parse_args()

    app = CrimeScannerApp(
        settings_store=AppSettingsStore(),
        initial_path=args.log_path or None,
        initial_dedupe=args.window_seconds,
    )
    app.run()


if __name__ == "__main__":
    main()
