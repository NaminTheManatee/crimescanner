import json
import os
import shutil
import sys

try:
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Set

from crimescanner_core import (
    AppSettingsStore, AppSettings, CrimeEvent, DEFAULT_DEDUPE_SECONDS, DEFAULT_ORG_LOGO_URL, DEFAULT_PLAYER_AVATAR_URL,
    EventDatabase, GameLogParser, ImageDiskCache, RSIProfileLookup, RSICacheStore, StatsCacheStore,
    build_stats, display_org_name
)


class CrimeScannerService:
    def __init__(self) -> None:
        self.settings_store = AppSettingsStore()
        self.settings = self.settings_store.load()
        self.lookup = RSIProfileLookup(cache_store=RSICacheStore())
        self.event_db = EventDatabase()
        self.image_cache = ImageDiskCache()
        self.stats_cache = StatsCacheStore()
        self.events: List[CrimeEvent] = []
        self.file_scan_state: Dict[str, Tuple[float, int]] = {}
        self.file_event_cache: Dict[str, List[CrimeEvent]] = {}
        self.last_signature = ""
        self.last_seen_keys: Set[Tuple[str, str, str, str]] = set()
        self.initial_enrich_done = False

    def _log_path(self, folder_text: Optional[str] = None) -> Optional[Path]:
        folder_text = (folder_text if folder_text is not None else self.settings.game_folder_path).strip().strip('"')
        if not folder_text:
            return None
        path = Path(folder_text)
        if path.is_file() and path.name.lower() == "game.log":
            return path
        return path / "Game.log"

    def _backup_dir(self, folder_text: Optional[str] = None) -> Optional[Path]:
        folder_text = (folder_text if folder_text is not None else self.settings.game_folder_path).strip().strip('"')
        if not folder_text:
            return None
        path = Path(folder_text)
        if path.is_file():
            path = path.parent
        return path / "logbackups"

    def _file_signature(self, path: Path):
        try:
            st = path.stat()
            return (st.st_mtime, st.st_size)
        except Exception:
            return None

    def _parse_file_cached(self, parser: GameLogParser, path: Path) -> List[CrimeEvent]:
        key = str(path)
        sig = self._file_signature(path)
        if sig is None:
            self.file_scan_state.pop(key, None)
            self.file_event_cache.pop(key, None)
            return []
        if self.file_scan_state.get(key) == sig and key in self.file_event_cache:
            return self.file_event_cache[key]
        events = parser.parse(path)
        self.file_scan_state[key] = sig
        self.file_event_cache[key] = events
        return events

    def _apply_cached_metadata(self, events: List[CrimeEvent]) -> None:
        for event in events:
            cached = self.lookup._cache.get(event.player)
            if not cached:
                continue
            org_name, org_url, player_avatar_url, org_logo_url, org_state = cached
            if player_avatar_url and player_avatar_url != DEFAULT_PLAYER_AVATAR_URL:
                event.player_avatar_url = player_avatar_url
            # Never allow an Unknown cache result to overwrite a known org/logo/link.
            if org_state != "unknown" and (org_name or org_url or org_logo_url):
                event.organization = org_name
                event.organization_url = org_url
                event.organization_logo_url = org_logo_url or DEFAULT_ORG_LOGO_URL

    @staticmethod
    def _event_key(event: CrimeEvent) -> Tuple[str, str, str, str]:
        return (event.player.casefold(), event.crime.casefold(), event.target_type, event.timestamp_utc.isoformat())

    def _enrich_some_metadata(self, events: List[CrimeEvent], limit: Optional[int] = 20, force_players: Optional[Set[str]] = None) -> int:
        enriched = 0
        force_players = force_players or set()
        for event in events:
            org_label = (event.organization or '').strip().casefold()
            needs_avatar = (not event.player_avatar_url) or event.player_avatar_url == DEFAULT_PLAYER_AVATAR_URL
            needs_org = (not org_label) or org_label in {'unknown', 'redacted'}
            needs_logo = (not event.organization_logo_url) or event.organization_logo_url == DEFAULT_ORG_LOGO_URL
            forced = event.player.casefold() in force_players
            if not forced and not (needs_avatar or needs_org or needs_logo):
                continue
            try:
                old_org = event.organization
                old_org_url = event.organization_url
                old_org_logo = event.organization_logo_url
                old_avatar = event.player_avatar_url
                org_name, org_url, player_avatar_url, org_logo_url, org_state = self.lookup.lookup_player_details(event.player)
                if player_avatar_url and player_avatar_url != DEFAULT_PLAYER_AVATAR_URL:
                    event.player_avatar_url = player_avatar_url
                elif old_avatar:
                    event.player_avatar_url = old_avatar
                # Only replace org data with a known result. Unknown fetches are transient and
                # must not erase org name/link/logo discovered during the startup parse.
                if org_state != "unknown" and (org_name or org_url or org_logo_url):
                    event.organization = org_name
                    event.organization_url = org_url
                    event.organization_logo_url = org_logo_url or DEFAULT_ORG_LOGO_URL
                else:
                    event.organization = old_org
                    event.organization_url = old_org_url
                    event.organization_logo_url = old_org_logo
                enriched += 1
            except Exception:
                pass
            if limit is not None and enriched >= limit:
                break
        return enriched

    def _signature(self, events: List[CrimeEvent]) -> str:
        return "|".join(f"{e.player.casefold()}|{e.crime.casefold()}|{e.target_type}|{e.timestamp_utc.isoformat()}|{e.organization}" for e in events)

    def settings_payload(self):
        return {
            "themeName": self.settings.theme_name,
            "dedupeSeconds": self.settings.dedupe_seconds,
            "gameFolderPath": self.settings.game_folder_path,
        }

    def set_settings(self, payload):
        if "themeName" in payload:
            self.settings.theme_name = str(payload["themeName"])
        if "dedupeSeconds" in payload:
            self.settings.dedupe_seconds = max(0, int(payload["dedupeSeconds"]))
        if "gameFolderPath" in payload:
            self.settings.game_folder_path = str(payload["gameFolderPath"])
        self.settings_store.save(self.settings)
        return self.settings_payload()

    def parse_now(self, enrich: bool = False, initial: bool = False, enrich_limit: Optional[int] = None):
        log_path = self._log_path()
        if log_path is None:
            return self.state("No game folder selected.")
        if not log_path.exists():
            return self.state(f"Game.log not found at {log_path}")

        parser = GameLogParser(dedupe_seconds=int(self.settings.dedupe_seconds or DEFAULT_DEDUPE_SECONDS))
        parsed: List[CrimeEvent] = []
        seen = set()
        active = []

        def add(path: Path):
            active.append(str(path))
            for event in self._parse_file_cached(parser, path):
                key = self._event_key(event)
                if key not in seen:
                    seen.add(key)
                    parsed.append(event)

        backup_dir = self._backup_dir()
        if backup_dir and backup_dir.exists():
            for backup_file in sorted(backup_dir.glob("*.log")):
                add(backup_file)
        add(log_path)

        for key in list(self.file_event_cache.keys()):
            if key not in active:
                self.file_event_cache.pop(key, None)
                self.file_scan_state.pop(key, None)

        previous_keys = set(self.last_seen_keys)
        current_keys = {self._event_key(event) for event in parsed}
        new_keys = current_keys - previous_keys
        new_players = {key[0] for key in new_keys}

        self._apply_cached_metadata(parsed)

        if enrich:
            if initial or not self.initial_enrich_done:
                # Startup pass: spend more time so existing parsed history is usable and cached.
                self._enrich_some_metadata(parsed, limit=enrich_limit if enrich_limit is not None else 250)
                self.initial_enrich_done = True
            elif new_players:
                # Routine pass: only enrich new players/events, plus a small top-off for missing cached data.
                self._enrich_some_metadata(parsed, limit=enrich_limit if enrich_limit is not None else 40, force_players=new_players)
            else:
                self._enrich_some_metadata(parsed, limit=10)

        parsed.sort(key=lambda e: e.timestamp_utc, reverse=True)
        self.events = parsed
        self.last_seen_keys = current_keys
        self.event_db.upsert_events(parsed)
        return self.state("")

    def enrich_metadata(self):
        changed = self._enrich_some_metadata(self.events, limit=100)
        if changed:
            self.event_db.upsert_events(self.events)
        return self.state(f"Enriched {changed} player profile(s).")

    def clear_cache(self):
        RSICacheStore().clear()
        self.image_cache.clear()
        self.stats_cache.clear()
        self.lookup._cache.clear()
        self.lookup._disk_cache.clear()
        self.file_scan_state.clear()
        self.file_event_cache.clear()
        return self.state("Cache cleared.")

    def state(self, status: str = ""):
        return {
            "settings": self.settings_payload(),
            "events": [e.to_json() for e in self.events],
            "stats": build_stats(self.events),
            "status": status,
        }


def respond(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def main():
    service = CrimeScannerService()
    respond({"type": "ready", "payload": service.state("Backend ready.")})
    for line in sys.stdin:
        message_id = None
        try:
            message = json.loads(line)
            message_id = message.get("id")
            command = message.get("command")
            payload = message.get("payload") or {}
            if command == "getState":
                result = service.state()
            elif command == "setSettings":
                service.set_settings(payload)
                result = service.state("Settings saved.")
            elif command == "parseNow":
                result = service.parse_now(enrich=bool(payload.get("enrich", False)), initial=bool(payload.get("initial", False)), enrich_limit=payload.get("enrichLimit"))
            elif command == "enrichMetadata":
                result = service.enrich_metadata()
            elif command == "clearCache":
                result = service.clear_cache()
            else:
                result = {"error": f"Unknown command: {command}"}
            respond({"type": "response", "id": message_id, "payload": result})
        except Exception as exc:
            respond({"type": "response", "id": message_id, "payload": {"error": str(exc)}})


if __name__ == "__main__":
    main()
