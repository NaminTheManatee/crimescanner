import html
import json
import sqlite3
import hashlib
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.error import URLError
from urllib.parse import urljoin, quote
from urllib.request import Request, urlopen

APP_NAME = "CrimeScanner"
DEFAULT_THEME = "Red"
DEFAULT_DEDUPE_SECONDS = 60
DEFAULT_SETTINGS_FILE = "crime_logger_game_log_settings.json"
DEFAULT_RSI_CACHE_FILE = "crime_scanner_rsi_cache.json"
DEFAULT_IMAGE_CACHE_DIR = "crime_scanner_image_cache"
DEFAULT_STATS_CACHE_FILE = "crime_scanner_stats_cache.json"
DEFAULT_DB_FILE = "crime_scanner_history.sqlite3"
RSI_CACHE_SCHEMA_VERSION = 9
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CrimeScanner/1.0"
RSI_CITIZEN_BASE = "https://robertsspaceindustries.com/en/citizens/"
RSI_ORG_BASE = "https://robertsspaceindustries.com/en/orgs/"
DEFAULT_LOGO_URL = "https://cdn.robertsspaceindustries.com/static/images/organization/defaults/logo/syndicate.jpg"
DEFAULT_PLAYER_AVATAR_URL = "https://cdn.robertsspaceindustries.com/static/images/account/avatar_default_big.jpg"
DEFAULT_ORG_LOGO_URL = "https://cdn.robertsspaceindustries.com/static/images/account/avatar_default_big.jpg"
REDACTED_ORG_LOGO_NAME = "59da34b90674408651c0a9373bff19c6dd65da524605b874f914bd576bdae2b1.png"

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

TIMESTAMP_RE = re.compile(r'^<(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)>', re.IGNORECASE)
CRIME_TEXT_RE = re.compile(r'Crime\s+Committed:\s*(?P<crime>.+?)\s*$', re.IGNORECASE)
AGAINST_PLAYER_RE = re.compile(r'against\s+(?P<player>.+?):\s*"\s*(?:\[(?P<id>\d+)\])?', re.IGNORECASE)
SELF_CRIME_RE = re.compile(r'"?(?P<player>[A-Za-z0-9_\-][A-Za-z0-9_\- ]*?)\s+committed\s+(?P<crime>.+?)\s+against\s+you\.?\s*$', re.IGNORECASE)


def build_citizen_url(player: str) -> str:
    return f"{RSI_CITIZEN_BASE}{quote(player.strip(), safe='')}"


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

    def to_json(self) -> Dict[str, str]:
        return {
            "player": self.player,
            "crime": self.crime,
            "timestampUtc": self.timestamp_utc.isoformat(),
            "displayTime": self.display_time,
            "targetType": self.target_type,
            "playerUrl": self.player_url,
            "playerAvatarUrl": self.player_avatar_url or DEFAULT_PLAYER_AVATAR_URL,
            "organization": self.organization,
            "organizationUrl": self.organization_url,
            "organizationLogoUrl": self.organization_logo_url or DEFAULT_ORG_LOGO_URL,
        }


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

            # v2 intentionally resets old development/test game paths once. Earlier
            # builds stored settings in Electron's userData folder and could make a
            # reinstall look like a fresh install while still showing an old local
            # path such as D:\StarCitizen\LIVE. After the user selects a folder
            # again, save() writes settings_schema_version=2 and the path persists.
            if int(data.get("settings_schema_version", 0) or 0) < 2:
                game_folder_path = ""
                legacy_log_path = ""

            if not game_folder_path and legacy_log_path:
                legacy_path = Path(legacy_log_path)
                game_folder_path = str(legacy_path.parent if legacy_path.suffix.lower() == ".log" else legacy_path)
            return AppSettings(
                theme_name=str(data.get("theme_name", DEFAULT_THEME)),
                dedupe_seconds=max(0, int(data.get("dedupe_seconds", DEFAULT_DEDUPE_SECONDS))),
                game_folder_path=game_folder_path,
            )
        except Exception:
            return AppSettings()

    def save(self, settings: AppSettings) -> None:
        payload = asdict(settings)
        payload["settings_schema_version"] = 2
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


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


class StatsCacheStore:
    def __init__(self, path: str = DEFAULT_STATS_CACHE_FILE):
        self.path = Path(path)

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

    def clear(self) -> None:
        if self.cache_dir.exists():
            for child in self.cache_dir.iterdir():
                if child.is_file():
                    try:
                        child.unlink()
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
            connection.execute("""
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
            """)
            connection.execute("CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp_utc)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_events_player ON events(player)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_events_org ON events(organization)")

    def upsert_events(self, events: List[CrimeEvent]) -> None:
        rows = [(
            e.player, e.crime, e.target_type, e.timestamp_utc.isoformat(), e.display_time,
            e.player_url or "", e.player_avatar_url or DEFAULT_PLAYER_AVATAR_URL,
            e.organization or "", e.organization_url or "", e.organization_logo_url or "",
        ) for e in events]
        if not rows:
            return
        with self._connect() as connection:
            connection.executemany("""
                INSERT INTO events (
                    player, crime, target_type, timestamp_utc, display_time,
                    player_url, player_avatar_url, organization, organization_url, organization_logo_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(player, crime, target_type, timestamp_utc) DO UPDATE SET
                    display_time=excluded.display_time,
                    player_url=excluded.player_url,
                    player_avatar_url=excluded.player_avatar_url,
                    organization=excluded.organization,
                    organization_url=excluded.organization_url,
                    organization_logo_url=excluded.organization_logo_url
            """, rows)


class GameLogParser:
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

    @staticmethod
    def _is_valid_player_name(player: str) -> bool:
        value = (player or "").strip()
        lowered = value.casefold()
        if not value:
            return False
        # Star Citizen can occasionally emit NPC/test entity handles. Do not log
        # these as players, because they are not RSI citizen handles.
        blocked_prefixes = (
            "pu_human",
            "pu_",
            "npc_",
            "ai_",
            "test_",
        )
        if lowered.startswith(blocked_prefixes):
            return False
        if "human-test" in lowered or "pu_human-test" in lowered:
            return False
        if " " in value:
            return False
        return bool(re.fullmatch(r"[A-Za-z0-9_\-]{2,64}", value))

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
            if " committed " in line and " against you" in line:
                ts = self._extract_timestamp(line)
                match = SELF_CRIME_RE.search(line)
                if ts is not None and match:
                    player = self._clean_spaces(match.group("player"))
                    if not self._is_valid_player_name(player):
                        i += 1
                        continue
                    crime = self._clean_spaces(match.group("crime"))
                    if self._should_keep(seen, player, crime, "self", ts):
                        results.append(CrimeEvent(player=player, crime=crime, timestamp_utc=ts, target_type="self", player_url=build_citizen_url(player)))
                    i += 1
                    continue
            if i + 1 < len(lines) and "Crime Committed:" in line:
                next_line = lines[i + 1].rstrip("\n")
                ts = self._extract_timestamp(line)
                crime_match = CRIME_TEXT_RE.search(line)
                player_match = AGAINST_PLAYER_RE.search(next_line)
                if ts is not None and crime_match and player_match:
                    crime = self._clean_spaces(crime_match.group("crime"))
                    player = self._clean_spaces(player_match.group("player"))
                    if not self._is_valid_player_name(player):
                        i += 2
                        continue
                    if self._should_keep(seen, player, crime, "other", ts):
                        results.append(CrimeEvent(player=player, crime=crime, timestamp_utc=ts, target_type="other", player_url=build_citizen_url(player)))
                    i += 2
                    continue
            i += 1
        return results


class RSIProfileLookup:
    def __init__(self, timeout_seconds: float = 6.0, cache_store: Optional[RSICacheStore] = None):
        self.timeout_seconds = timeout_seconds
        self.cache_store = cache_store or RSICacheStore()
        self._disk_cache = self.cache_store.load()
        self._cache: Dict[str, Tuple[str, str, str, str, str]] = {}
        for key, value in self._disk_cache.items():
            if isinstance(value, dict) and int(value.get("schema_version", 0) or 0) == RSI_CACHE_SCHEMA_VERSION:
                self._cache[key] = (
                    str(value.get("org_name", "")),
                    str(value.get("org_url", "")),
                    str(value.get("player_avatar_url", DEFAULT_PLAYER_AVATAR_URL)),
                    str(value.get("org_logo_url", "")),
                    str(value.get("org_state", "known")),
                )

    @staticmethod
    def _normalize_org_state(org_name: str, org_logo_url: str) -> str:
        return "known" if (org_name or "").strip() else "unknown"

    def lookup_player_details(self, player: str) -> Tuple[str, str, str, str, str]:
        player = (player or "").strip()
        if not player:
            return "", "", DEFAULT_PLAYER_AVATAR_URL, "", "unknown"

        cached = self._cache.get(player)
        if cached:
            org_name, org_url, avatar, logo, state = cached
            # Keep any complete cache entry. Do not re-fetch and risk replacing good org data
            # with Unknown during a routine background parse.
            if avatar and avatar != DEFAULT_PLAYER_AVATAR_URL and (org_name or org_url or logo):
                return cached

        player_url = build_citizen_url(player)
        request = Request(player_url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                page_html = html.unescape(response.read().decode("utf-8", errors="replace"))
        except (URLError, TimeoutError, OSError, UnicodeError, ValueError):
            # Network/RSI failures should never downgrade previously-enriched metadata.
            if cached:
                return cached
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

        avatar = self._extract_player_avatar(page_html, player_url) or DEFAULT_PLAYER_AVATAR_URL
        org_name, org_url = self._extract_main_org(page_html)
        if org_name:
            cleaned = re.sub(r"\s+", " ", html.unescape(org_name)).strip()
            if player.casefold() in cleaned.casefold():
                cleaned = re.sub(re.escape(player), "", cleaned, flags=re.IGNORECASE).strip(" -|/")
            org_name = cleaned

        org_logo = self._extract_org_logo_from_citizen_page(page_html, player_url)
        if (not org_logo or org_logo == DEFAULT_ORG_LOGO_URL) and org_url:
            org_logo = self._fetch_org_logo(org_url)

        state = self._normalize_org_state(org_name or "", org_logo or "")
        resolved = (org_name or "", org_url or "", avatar or DEFAULT_PLAYER_AVATAR_URL, org_logo or DEFAULT_ORG_LOGO_URL, state)
        if cached:
            c_org, c_url, c_avatar, c_logo, c_state = cached
            # Merge fetch results conservatively. A later parse may fail to find org details;
            # preserve the known org/name/logo/link instead of overwriting with Unknown.
            if not org_name and c_org:
                org_name, org_url, org_logo, state = c_org, c_url, c_logo, c_state
            if (not avatar or avatar == DEFAULT_PLAYER_AVATAR_URL) and c_avatar:
                avatar = c_avatar
            resolved = (org_name or "", org_url or "", avatar or DEFAULT_PLAYER_AVATAR_URL, org_logo or DEFAULT_ORG_LOGO_URL, state)
        self._cache[player] = resolved
        self._disk_cache[player] = {
            "schema_version": RSI_CACHE_SCHEMA_VERSION,
            "org_name": resolved[0],
            "org_url": resolved[1],
            "player_avatar_url": resolved[2],
            "org_logo_url": resolved[3],
            "org_state": resolved[4],
        }
        self.cache_store.save(self._disk_cache)
        return self._cache[player]

    @staticmethod
    def _strip_tags(text: str) -> str:
        text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", html.unescape(text)).strip()

    @staticmethod
    def _attr(tag: str, attr: str) -> str:
        match = re.search(rf"\b{attr}\s*=\s*([\"'])(.*?)\1", tag, flags=re.IGNORECASE | re.DOTALL)
        return html.unescape(match.group(2)).strip() if match else ""

    @staticmethod
    def _clean_candidate_text(text: str) -> str:
        text = html.unescape(str(text or ""))
        text = re.sub(r"\\/", "/", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip(" \t\r\n-|•:")

    @staticmethod
    def _valid_org_name(name: str) -> bool:
        if not name:
            return False
        lowered = name.casefold()
        bad = {
            "organization", "main organization", "main org", "view organization",
            "spectrum", "badge", "badges", "rank", "profile", "citizen dossier",
            "no main org found in public records",
        }
        if lowered in bad:
            return False
        if len(name) < 2 or len(name) > 100:
            return False
        return True

    @staticmethod
    def _slice_after_marker(page_html: str, markers: Tuple[str, ...], end_markers: Tuple[str, ...], span: int = 35000) -> str:
        lower = page_html.lower()
        starts = [lower.find(marker) for marker in markers if lower.find(marker) >= 0]
        if not starts:
            return page_html[:span]
        start = min(starts)
        ends = [lower.find(marker, start + 1) for marker in end_markers]
        ends = [idx for idx in ends if idx > start]
        end = min(ends) if ends else min(len(page_html), start + span)
        return page_html[start:end]

    @classmethod
    def _extract_main_org(cls, page_html: str) -> Tuple[str, str]:
        main_block = cls._slice_after_marker(
            page_html,
            ("main organization", "main org"),
            ("organization rank", "enlisted", "location", "fluency", "bio", "website"),
            span=30000,
        )
        if re.search(r"no\s+main\s+org\s+found", cls._strip_tags(main_block), flags=re.IGNORECASE):
            return "", ""

        anchor_re = re.compile(
            r"<a\b[^>]*href\s*=\s*([\"'])(?P<href>[^\"']*/(?:en/)?orgs/(?P<slug>[^\"'#?/]+)[^\"']*)\1[^>]*>(?P<body>[\s\S]*?)</a>",
            flags=re.IGNORECASE,
        )
        candidates: List[Tuple[int, str, str]] = []
        for match in anchor_re.finditer(main_block):
            tag = match.group(0)
            context = main_block[max(0, match.start() - 800):match.end() + 800].lower()
            if any(token in context for token in ("badge", "badges", "rank icon", "reputation", "award", "medal")):
                continue
            slug = match.group("slug").strip().strip("/")
            name = cls._strip_tags(match.group("body"))
            if not name:
                name = cls._attr(tag, "title") or cls._attr(tag, "alt")
            name = cls._clean_candidate_text(name)
            if cls._valid_org_name(name) and slug:
                score = 100 if "main organization" in context or "main org" in context else 50
                candidates.append((score, name, f"{RSI_ORG_BASE}{slug}"))
        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            return candidates[0][1], candidates[0][2]

        json_patterns = [
            (r'"organization_name"\s*:\s*"([^"]+)"[\s\S]{0,5000}?"organization_sid"\s*:\s*"([^"]+)"', "name_slug"),
            (r'"organization_sid"\s*:\s*"([^"]+)"[\s\S]{0,5000}?"organization_name"\s*:\s*"([^"]+)"', "slug_name"),
            (r'"mainOrganization"[\s\S]{0,7000}?"name"\s*:\s*"([^"]+)"[\s\S]{0,7000}?"sid"\s*:\s*"([^"]+)"', "name_slug"),
            (r'"mainOrganization"[\s\S]{0,7000}?"sid"\s*:\s*"([^"]+)"[\s\S]{0,7000}?"name"\s*:\s*"([^"]+)"', "slug_name"),
        ]
        for pattern, order in json_patterns:
            match = re.search(pattern, page_html, flags=re.IGNORECASE)
            if not match:
                continue
            first, second = match.group(1), match.group(2)
            name, slug = (first, second) if order == "name_slug" else (second, first)
            name = cls._clean_candidate_text(name)
            slug = slug.strip().strip("/")
            if cls._valid_org_name(name) and slug:
                return name, f"{RSI_ORG_BASE}{slug}"
        return "", ""

    @staticmethod
    def _attr(tag: str, attr: str) -> str:
        match = re.search(rf"\b{attr}\s*=\s*([\"'])(.*?)\1", tag, flags=re.IGNORECASE | re.DOTALL)
        return html.unescape(match.group(2)).strip() if match else ""

    @staticmethod
    def _normalize_image_url(url: str, base_url: str) -> str:
        return urljoin(base_url, html.unescape(str(url or "").strip()))

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
    def _image_tags(cls, html_text: str, base_url: str) -> List[Tuple[str, str, str]]:
        tags: List[Tuple[str, str, str]] = []
        for match in re.finditer(r"<img\b[^>]*>", html_text, flags=re.IGNORECASE | re.DOTALL):
            tag = match.group(0)
            src = cls._attr(tag, "src") or cls._attr(tag, "data-src") or cls._attr(tag, "data-lazy") or cls._attr(tag, "content")
            if not src:
                continue
            url = cls._normalize_image_url(src, base_url)
            before = html_text[max(0, match.start() - 1000):match.start()]
            after = html_text[match.end():match.end() + 600]
            tags.append((url, tag.lower(), f"{before} {tag} {after}".lower()))
        return tags

    @staticmethod
    def _is_placeholder_image(url: str) -> bool:
        lower = (url or "").lower()
        return (not lower) or "blank" in lower or REDACTED_ORG_LOGO_NAME in lower

    @staticmethod
    def _is_bad_rank_badge_image(url: str, context: str = "") -> bool:
        lower = f"{url} {context}".lower()
        return any(token in lower for token in (
            "heap_thumb", "badge", "badges", "rank", "reputation", "award", "medal", "icon-badge"
        ))

    @classmethod
    def _extract_player_avatar(cls, page_html: str, base_url: str) -> str:
        # Use the original rank-based strategy from the Tkinter version, with extra
        # rejection for badge/rank assets that caused bad Electron rows.
        image_urls = cls._extract_image_urls(page_html, base_url)
        if not image_urls:
            return DEFAULT_PLAYER_AVATAR_URL
        ranked = sorted(
            image_urls,
            key=lambda u: cls._rank_image_url(
                u,
                preferred_keywords=['avatar', 'profile', 'citizen', 'handle', 'portrait', 'account/avatar'],
                avoid_keywords=['logo', 'org', 'organization', 'syndicate', 'default/logo', '/orgs/', 'badge', 'badges', 'rank', 'reputation'],
            ),
            reverse=True,
        )
        for candidate in ranked:
            lower = candidate.lower()
            if any(token in lower for token in ('badge', 'badges', 'rank', 'reputation', 'heap_thumb')):
                continue
            return candidate
        return DEFAULT_PLAYER_AVATAR_URL

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
                avoid_keywords=['avatar_default', 'avatar', 'citizen', 'profile', 'portrait', '/citizens/', 'badge', 'badges', 'rank', 'reputation'],
            ),
            reverse=True,
        )
        for candidate in ranked:
            lower = candidate.lower()
            if candidate == DEFAULT_PLAYER_AVATAR_URL:
                continue
            if any(token in lower for token in ('badge', 'badges', 'rank', 'reputation', 'heap_thumb')):
                continue
            return candidate
        return ''

    @classmethod
    def _rank_image_url(cls, url: str, preferred_keywords: List[str], avoid_keywords: List[str]) -> Tuple[int, int, int, int]:
        lower = url.lower()
        return (
            sum(1 for kw in preferred_keywords if kw in lower),
            1 if '/media/' in lower else 0,
            1 if 'heap_infobox' in lower else 0,
            -sum(1 for kw in avoid_keywords if kw in lower),
        )

    def _fetch_org_logo(self, org_url: str) -> str:
        if not org_url:
            return DEFAULT_ORG_LOGO_URL
        request = Request(org_url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
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
                avoid_keywords=['avatar_default', 'avatar', 'citizen', 'profile', 'portrait', 'badge', 'badges', 'rank', 'reputation'],
            ),
            reverse=True,
        )
        for candidate in ranked:
            lower = candidate.lower()
            if any(token in lower for token in ('badge', 'badges', 'rank', 'reputation', 'heap_thumb')):
                continue
            return candidate
        return DEFAULT_ORG_LOGO_URL


def is_unknown_org(event: CrimeEvent) -> bool:
    org = (event.organization or "").strip().casefold()
    return not org or REDACTED_ORG_LOGO_NAME in (event.organization_logo_url or "") or org in {"redacted", "unknown"}


def display_org_name(event: CrimeEvent) -> str:
    return "Unknown" if is_unknown_org(event) else (event.organization or "").strip()


def stats_rows(events: List[CrimeEvent], key: str):
    grouped: Dict[tuple, str] = {}
    for event in events:
        minute_key = event.timestamp_utc.strftime("%Y-%m-%d %H:%M")
        if key == "encountered":
            grouped[(event.player.casefold(), minute_key)] = event.player
        elif key == "encountered_org":
            org = display_org_name(event)
            grouped[(org.casefold(), event.player.casefold(), minute_key)] = org
        elif key == "killed_by" and event.target_type == "self":
            grouped[(event.player.casefold(), minute_key)] = event.player
        elif key == "killed" and event.target_type == "other":
            grouped[(event.player.casefold(), minute_key)] = event.player
        elif key == "crime":
            grouped[(event.crime.casefold(), minute_key)] = event.crime
    counts: Dict[str, int] = {}
    for label in grouped.values():
        counts[label] = counts.get(label, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold()))


def build_stats(events: List[CrimeEvent]) -> Dict[str, object]:
    return {
        "totalEvents": len(events),
        "totalOther": sum(1 for e in events if e.target_type == "other"),
        "totalSelf": sum(1 for e in events if e.target_type == "self"),
        "encountered": stats_rows(events, "encountered"),
        "encounteredOrg": stats_rows(events, "encountered_org"),
        "killed": stats_rows(events, "killed"),
        "killedBy": stats_rows(events, "killed_by"),
    }
