"""
Webex SR robin sync prototype.

Reads new messages from source rooms, extracts SR numbers, and posts a tagged
copy into a target room. Configuration is loaded from environment variables so
the script can run without editing secrets into the file.

Required environment variables:
    WEBEX_AUTH_TOKEN      Webex bearer token.
    WEBEX_TARGET_ROOM     Target room ID where tagged messages are posted.
    WEBEX_SOURCE_ROOMS    Comma-separated source room IDs to watch.

Optional environment variables:
    WEBEX_CHECK_INTERVAL  Seconds between checks. Default: 5.
    WEBEX_RUN_HOURS       How long the service runs. Default: 8.
    WEBEX_EMAIL_DOMAIN    Domain used for nicknames. Default: cisco.com.
    WEBEX_DRY_RUN         Set to 1/true/yes to print posts instead of sending.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests


WEBEX_API_BASE = "https://webexapis.com/v1"
SR_PATTERN = re.compile(r"\b(7\d{8})\b")
ASSIGNMENT_PATTERN = re.compile(r"Added SR (\d+) for ([A-Za-z0-9_.-]+)", re.IGNORECASE)


@dataclass(frozen=True)
class Config:
    auth_token: str
    target_room: str
    source_rooms: list[str]
    check_interval: int = 5
    run_hours: float = 8
    email_domain: str = "cisco.com"
    dry_run: bool = False

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.auth_token}",
            "Content-Type": "application/json",
        }


class WebexRobinSync:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.session = requests.Session()
        self.processed_message_ids: set[str] = set()
        self.team_members: list[str] = []
        self.sr_assignments: dict[str, str] = {}
        self.script_start_time = datetime.now(timezone.utc)

    def get_person_id(self, email: str) -> str | None:
        try:
            response = self.session.get(
                f"{WEBEX_API_BASE}/people",
                headers=self.config.headers,
                params={"email": email},
                timeout=10,
            )
            response.raise_for_status()
            items = response.json().get("items", [])
            return items[0]["id"] if items else None
        except requests.RequestException as exc:
            print(f"  -> Could not resolve {email}: {exc}")
            return None

    def load_team_from_target(self) -> None:
        """Load the latest robin list and SR assignments from the target room."""
        try:
            response = self.session.get(
                f"{WEBEX_API_BASE}/messages",
                headers=self.config.headers,
                params={"roomId": self.config.target_room, "max": 200},
                timeout=10,
            )
            response.raise_for_status()
            messages = response.json().get("items", [])
        except requests.RequestException as exc:
            print(f"Error fetching from target: {exc}")
            return

        team_found = False
        new_sr_count = 0

        for message in messages:
            text = message.get("text", "") or ""
            markdown = message.get("markdown", "") or ""
            message_time = parse_webex_time(message.get("created", ""))

            if not team_found:
                team_found = self._try_load_robin_from_message(text, markdown)

            if message_time and message_time >= self.script_start_time:
                combined = f"{text} {markdown}"
                match = ASSIGNMENT_PATTERN.search(combined)
                if match:
                    sr_number, engineer = match.groups()
                    if sr_number not in self.sr_assignments:
                        self.sr_assignments[sr_number] = engineer
                        new_sr_count += 1

        if new_sr_count > 0 or not hasattr(self, "_first_target_load_done"):
            preview = ", ".join(self.team_members[:5]) if self.team_members else "none"
            print(
                f"[{timestamp()}] Robin: {preview} | "
                f"SRs tracked: {len(self.sr_assignments)}"
            )
            self._first_target_load_done = True

    def initialize_tracker(self) -> None:
        """Mark recent source messages as already seen so startup does not spam."""
        for room_id in self.config.source_rooms:
            try:
                response = self.session.get(
                    f"{WEBEX_API_BASE}/messages",
                    headers=self.config.headers,
                    params={"roomId": room_id, "max": 10},
                    timeout=10,
                )
                response.raise_for_status()
                for message in response.json().get("items", []):
                    message_id = message.get("id")
                    if message_id:
                        self.processed_message_ids.add(message_id)
            except requests.RequestException as exc:
                print(f"  -> Could not initialize room {room_id}: {exc}")

    def sync_once(self) -> None:
        if not self.team_members:
            print(f"[{timestamp()}] No robin list found yet; waiting.")
            return

        for room_id in self.config.source_rooms:
            self._sync_room(room_id)

    def run(self) -> None:
        print(f"Script started at: {self.script_start_time.isoformat()}")
        print(
            f"Checking every {self.config.check_interval} seconds. "
            f"Run limit: {self.config.run_hours:g} hour(s)."
        )
        if self.config.dry_run:
            print("Dry run enabled. Messages will be printed, not posted.")

        self.load_team_from_target()
        self.initialize_tracker()
        start_time = time.time()
        run_seconds = self.config.run_hours * 3600
        print("Sync service started.\n")

        try:
            while time.time() - start_time < run_seconds:
                self.load_team_from_target()
                self.sync_once()
                time.sleep(self.config.check_interval)
        except KeyboardInterrupt:
            print("\nStopped.")

    def _try_load_robin_from_message(self, text: str, markdown: str) -> bool:
        for source in (text, markdown):
            marker = find_robin_marker(source)
            if not marker:
                continue

            after_marker = source.split(marker, 1)[1]
            list_text = after_marker.splitlines()[0].strip()
            cleaned_names = clean_robin_list(list_text)
            if cleaned_names:
                self.team_members = cleaned_names
                return True
        return False

    def _sync_room(self, room_id: str) -> None:
        try:
            response = self.session.get(
                f"{WEBEX_API_BASE}/messages",
                headers=self.config.headers,
                params={"roomId": room_id, "max": 10},
                timeout=10,
            )
            response.raise_for_status()
            messages = response.json().get("items", [])
        except requests.RequestException as exc:
            print(f"  -> Error in room {room_id}: {exc}")
            return

        for message in reversed(messages):
            message_id = message.get("id")
            if not message_id or message_id in self.processed_message_ids:
                continue

            try:
                self._process_message(message)
            finally:
                self.processed_message_ids.add(message_id)

    def _process_message(self, message: dict[str, Any]) -> None:
        text = message.get("text", "") or ""
        if not should_forward_message(text):
            return

        message_id = message["id"]
        sr_number = extract_sr_number(text)
        existing_engineer = self.sr_assignments.get(sr_number) if sr_number else None

        if existing_engineer:
            to_tag = [existing_engineer]
            recall_note = f"RECALL: Already assigned to {existing_engineer}\n\n"
            print(f"[{timestamp()}] SR {sr_number} recalled to {existing_engineer}")
        else:
            to_tag = self.team_members[:2]
            recall_note = ""
            print(f"[{timestamp()}] New SR {sr_number or 'N/A'} tagged: {to_tag}")

        mentions, person_ids = self._build_mentions(to_tag)
        payload = {
            "roomId": self.config.target_room,
            "markdown": (
                f"{' '.join(mentions)}\n\n"
                f"{recall_note}"
                f"Tagged: {', '.join(to_tag)}\n\n"
                f"{text}\n\n"
                f"[OrigID: {message_id}] [SR: {sr_number or 'N/A'}]"
            ),
            "mentionedPeople": person_ids,
        }
        self._post_message(payload)

        if sr_number and not existing_engineer and to_tag:
            self.sr_assignments[sr_number] = to_tag[0]

    def _build_mentions(self, nicknames: list[str]) -> tuple[list[str], list[str]]:
        mentions: list[str] = []
        person_ids: list[str] = []

        for nickname in nicknames:
            email = f"{nickname}@{self.config.email_domain}"
            person_id = self.get_person_id(email)
            if person_id:
                mentions.append(f"<@personId:{person_id}|{nickname}>")
                person_ids.append(person_id)
            else:
                mentions.append(f"@{nickname}")

        return mentions, person_ids

    def _post_message(self, payload: dict[str, Any]) -> None:
        if self.config.dry_run:
            print(f"DRY RUN post payload: {payload}")
            return

        try:
            response = self.session.post(
                f"{WEBEX_API_BASE}/messages",
                headers=self.config.headers,
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            print(f"  -> Failed to post message: {exc}")


def load_config() -> Config:
    auth_token = os.getenv("WEBEX_AUTH_TOKEN", "").strip()
    target_room = os.getenv("WEBEX_TARGET_ROOM", "").strip()
    source_rooms = parse_csv_env("WEBEX_SOURCE_ROOMS")

    missing = []
    if not auth_token:
        missing.append("WEBEX_AUTH_TOKEN")
    if not target_room:
        missing.append("WEBEX_TARGET_ROOM")
    if not source_rooms:
        missing.append("WEBEX_SOURCE_ROOMS")
    if missing:
        raise SystemExit(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + "\nExample PowerShell setup:\n"
            + "$env:WEBEX_AUTH_TOKEN='your-token'\n"
            + "$env:WEBEX_TARGET_ROOM='target-room-id'\n"
            + "$env:WEBEX_SOURCE_ROOMS='source-room-1,source-room-2'\n"
            + "$env:WEBEX_DRY_RUN='1'\n"
            + "python masv.py"
        )

    return Config(
        auth_token=auth_token,
        target_room=target_room,
        source_rooms=source_rooms,
        check_interval=parse_int_env("WEBEX_CHECK_INTERVAL", 5),
        run_hours=parse_float_env("WEBEX_RUN_HOURS", 8),
        email_domain=os.getenv("WEBEX_EMAIL_DOMAIN", "cisco.com").strip() or "cisco.com",
        dry_run=parse_bool_env("WEBEX_DRY_RUN"),
    )


def parse_csv_env(name: str) -> list[str]:
    return [item.strip() for item in os.getenv(name, "").split(",") if item.strip()]


def parse_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    try:
        return int(raw_value)
    except ValueError:
        raise SystemExit(f"{name} must be an integer, got {raw_value!r}") from None


def parse_float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    try:
        return float(raw_value)
    except ValueError:
        raise SystemExit(f"{name} must be a number, got {raw_value!r}") from None


def parse_bool_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "y", "on"}


def extract_sr_number(text: str) -> str | None:
    match = SR_PATTERN.search(text)
    return match.group(1) if match else None


def parse_webex_time(time_str: str) -> datetime | None:
    try:
        return datetime.fromisoformat(time_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def find_robin_marker(text: str) -> str | None:
    for marker in ("New robin:", "Next robin:"):
        if marker in text:
            return marker
    return None


def clean_robin_list(list_text: str) -> list[str]:
    """Extract nickname-like values from a comma-separated robin list."""
    cleaned_names: list[str] = []

    for raw_name in list_text.split(","):
        clean = re.sub(r"[\[\]()*_`~]", "", raw_name).strip()
        clean = re.sub(r"\(\d+\)", "", clean).strip()
        clean = clean.split()[0] if clean else ""

        if clean and clean.isalnum():
            cleaned_names.append(clean)

    return cleaned_names


def should_forward_message(text: str) -> bool:
    if not text:
        return False

    lowered = text.lower()
    return not (
        text.startswith("[Forwarded from")
        or "see card data" in lowered
        or "[ complete ]" in lowered
    )


def timestamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def main() -> None:
    config = load_config()
    WebexRobinSync(config).run()


if __name__ == "__main__":
    main()
