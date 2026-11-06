#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_HOMESERVER = "https://chat.moekyun.me"
DEFAULT_PACKS_DIR = "./web/packs"
DEFAULT_RECOVER_LIMIT = 2000
PAGE_SIZE = 100

STATE_EVENT_PONIES = "im.ponies.room_emotes"
STATE_EVENT_MATRIX_V119 = "m.room.image_pack"
RECOVERED_PACK_ID = "recovered"
RECOVERED_PACK_TITLE = "Recovered Stickers"

logger = logging.getLogger("sync_packs")


def clean_url_value(value: str) -> str:
    """Clean potential Markdown link formatting like [https://...](https://...)."""
    value = value.strip()
    md_match = re.match(r"^\[([^\]]+)\]\(([^)]+)\)$", value)
    if md_match:
        value = md_match.group(2).strip()
    return value.rstrip("/")


def load_dotenv(dotenv_path: Path | None = None) -> None:
    """Load key-value pairs from a .env file into os.environ if not already set."""
    candidates = (
        [dotenv_path]
        if dotenv_path
        else [Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"]
    )
    for path in candidates:
        if path and path.is_file():
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :].strip()
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                elif " #" in val:
                    val = val.split(" #", 1)[0].strip()
                if key and key not in os.environ:
                    os.environ[key] = val
            break


class MatrixClient:
    """Lightweight Matrix Client-Server v3 HTTP client."""

    def __init__(self, homeserver: str, access_token: str, dry_run: bool = False) -> None:
        self.homeserver = clean_url_value(homeserver)
        self.access_token = access_token.strip()
        self.dry_run = dry_run

    def _request(
        self,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.homeserver}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"

        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json",
            "User-Agent": "maunium-stickerpicker-sync/1.0",
        }
        data: bytes | None = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise RuntimeError(
                f"Matrix API {method} {path} failed with HTTP {exc.code}: {err_body or exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Matrix API {method} {path} connection error: {exc.reason}"
            ) from exc

    def get_room_messages(
        self,
        room_id: str,
        from_token: str | None = None,
        limit: int = PAGE_SIZE,
        direction: str = "b",
    ) -> dict[str, Any]:
        encoded_room = urllib.parse.quote(room_id, safe="")
        query: dict[str, Any] = {"dir": direction, "limit": limit}
        if from_token:
            query["from"] = from_token
        return self._request("GET", f"/_matrix/client/v3/rooms/{encoded_room}/messages", query=query)

    def get_room_state(self, room_id: str) -> list[dict[str, Any]]:
        encoded_room = urllib.parse.quote(room_id, safe="")
        res = self._request("GET", f"/_matrix/client/v3/rooms/{encoded_room}/state")
        if not isinstance(res, list):
            raise RuntimeError(f"Unexpected state response format for room {room_id}")
        return res

    def put_room_state(
        self,
        room_id: str,
        event_type: str,
        state_key: str,
        content: dict[str, Any],
    ) -> dict[str, Any]:
        encoded_room = urllib.parse.quote(room_id, safe="")
        encoded_type = urllib.parse.quote(event_type, safe="")
        encoded_key = urllib.parse.quote(state_key, safe="")
        path = f"/_matrix/client/v3/rooms/{encoded_room}/state/{encoded_type}/{encoded_key}"
        if self.dry_run:
            logger.info("[DRY-RUN] Would PUT %s", path)
            return {}
        logger.info("PUT %s", path)
        return self._request("PUT", path, body=content)


def derive_shortcode(raw_id: str | None, body: str | None, mxc_url: str, used: set[str]) -> str:
    """Derive a clean, unique shortcode key for a sticker."""
    candidate = ""
    if raw_id and not raw_id.startswith("sha256:"):
        candidate = raw_id.strip()
    elif body:
        candidate = body.strip()

    # Sanitize shortcode: replace whitespace/invalid chars with underscores
    candidate = re.sub(r"[^a-zA-Z0-9_.-]+", "_", candidate).strip("_")
    if not candidate:
        media_id = mxc_url.rsplit("/", 1)[-1] if "/" in mxc_url else mxc_url
        candidate = re.sub(r"[^a-zA-Z0-9_.-]+", "_", media_id).strip("_") or "sticker"

    unique = candidate
    counter = 2
    while unique in used:
        unique = f"{candidate}_{counter}"
        counter += 1
    used.add(unique)
    return unique


def normalize_maunium_info(raw_info: dict[str, Any] | None, mxc_url: str) -> dict[str, Any]:
    """Build a Maunium-compatible sticker `info` dictionary."""
    raw_info = raw_info or {}
    info: dict[str, Any] = {}
    for key in ("w", "h", "mimetype", "size"):
        if key in raw_info and raw_info[key] is not None:
            info[key] = raw_info[key]
    info["thumbnail_url"] = raw_info.get("thumbnail_url") or mxc_url
    if "thumbnail_info" in raw_info and isinstance(raw_info["thumbnail_info"], dict):
        info["thumbnail_info"] = raw_info["thumbnail_info"]
    return info


def normalize_msc2545_info(raw_info: dict[str, Any] | None) -> dict[str, Any]:
    """Build an MSC2545-compatible image `info` dictionary."""
    raw_info = raw_info or {}
    info: dict[str, Any] = {}
    for key in ("w", "h", "mimetype", "size", "thumbnail_url", "thumbnail_info"):
        if key in raw_info and raw_info[key] is not None:
            if key == "thumbnail_url":
                continue
            if key == "thumbnail_info":
                continue
            info[key] = raw_info[key]
    return info


def maunium_to_msc2545(pack_data: dict[str, Any]) -> dict[str, Any]:
    """Convert a Maunium pack JSON object into MSC2545 state event content."""
    pack_id = str(pack_data.get("id") or "default")
    title = str(pack_data.get("title") or pack_id)
    stickers = pack_data.get("stickers") or []

    images: dict[str, Any] = {}
    used_shortcodes: set[str] = set()

    for sticker in stickers:
        if not isinstance(sticker, dict):
            continue
        url = sticker.get("url")
        if not url or not isinstance(url, str):
            continue
        raw_id = sticker.get("id")
        body = sticker.get("body") or raw_id or ""
        shortcode = derive_shortcode(
            str(raw_id) if raw_id else None,
            str(body) if body else None,
            url,
            used_shortcodes,
        )
        images[shortcode] = {
            "url": url,
            "body": str(body) if body else shortcode,
            "info": normalize_msc2545_info(sticker.get("info")),
            "usage": ["sticker", "emoticon"],
        }

    return {
        "pack": {
            "display_name": title,
        },
        "images": images,
    }


def msc2545_to_maunium(state_key: str, content: dict[str, Any]) -> dict[str, Any]:
    """Convert MSC2545 state event content into Maunium pack JSON schema."""
    pack_id = state_key if state_key else "default"
    pack_meta = content.get("pack") if isinstance(content.get("pack"), dict) else {}
    title = (
        pack_meta.get("display_name")
        or pack_meta.get("name")
        or content.get("display_name")
        or pack_id
    )

    images = content.get("images") if isinstance(content.get("images"), dict) else {}
    stickers: list[dict[str, Any]] = []

    for shortcode, img in images.items():
        if not isinstance(img, dict):
            continue
        url = img.get("url")
        if not url or not isinstance(url, str):
            continue
        body = img.get("body") or shortcode
        raw_info = img.get("info") if isinstance(img.get("info"), dict) else {}
        stickers.append(
            {
                "id": str(shortcode),
                "body": str(body),
                "url": url,
                "info": normalize_maunium_info(raw_info, url),
            }
        )

    return {
        "title": str(title),
        "id": str(pack_id),
        "stickers": stickers,
    }


def recover_historical_stickers(
    client: MatrixClient,
    room_ids: list[str],
    primary_room_id: str,
    packs_dir: Path,
    max_events_per_room: int = DEFAULT_RECOVER_LIMIT,
    existing_ponies_packs: dict[str, dict[str, Any]] | None = None,
    existing_v119_packs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Paginate backwards through room history to recover m.sticker events."""
    recovered_by_url: dict[str, dict[str, Any]] = {}
    used_shortcodes: set[str] = set()

    # Load existing recovered.json if present so we preserve previously recovered stickers
    recovered_path = packs_dir / f"{RECOVERED_PACK_ID}.json"
    if recovered_path.is_file():
        try:
            existing_data = json.loads(recovered_path.read_text(encoding="utf-8"))
            for sticker in existing_data.get("stickers", []):
                url = sticker.get("url")
                if url and isinstance(url, str) and url not in recovered_by_url:
                    shortcode = derive_shortcode(
                        sticker.get("id"),
                        sticker.get("body"),
                        url,
                        used_shortcodes,
                    )
                    recovered_by_url[url] = {
                        "id": shortcode,
                        "body": sticker.get("body") or shortcode,
                        "url": url,
                        "info": normalize_maunium_info(sticker.get("info"), url),
                    }
        except Exception as exc:
            logger.warning("Could not parse existing %s: %s", recovered_path, exc)

    for room_id in room_ids:
        logger.info(
            "Recovering historical stickers from room %s (limit=%d)...",
            room_id,
            max_events_per_room,
        )
        from_token: str | None = None
        events_scanned = 0

        while events_scanned < max_events_per_room:
            batch_limit = min(PAGE_SIZE, max_events_per_room - events_scanned)
            resp = client.get_room_messages(
                room_id=room_id,
                from_token=from_token,
                limit=batch_limit,
                direction="b",
            )
            chunk = resp.get("chunk") or []
            if not chunk:
                break

            events_scanned += len(chunk)
            for event in chunk:
                if not isinstance(event, dict) or event.get("type") != "m.sticker":
                    continue
                content = event.get("content")
                if not isinstance(content, dict):
                    continue
                url = content.get("url")
                if not url or not isinstance(url, str) or not url.startswith("mxc://"):
                    continue
                if url in recovered_by_url:
                    continue

                body = content.get("body") or ""
                raw_info = content.get("info") if isinstance(content.get("info"), dict) else {}
                shortcode = derive_shortcode(None, str(body), url, used_shortcodes)

                recovered_by_url[url] = {
                    "id": shortcode,
                    "body": str(body) if body else shortcode,
                    "url": url,
                    "info": normalize_maunium_info(raw_info, url),
                }

            next_token = resp.get("end")
            if not next_token or next_token == from_token:
                break
            from_token = next_token

        logger.info(
            "Scanned %d events in %s (total unique stickers recovered: %d)",
            events_scanned,
            room_id,
            len(recovered_by_url),
        )

    if not recovered_by_url:
        logger.info("No historical stickers found across rooms.")
        return None

    recovered_pack = {
        "title": RECOVERED_PACK_TITLE,
        "id": RECOVERED_PACK_ID,
        "stickers": list(recovered_by_url.values()),
    }

    write_json_file(recovered_path, recovered_pack, dry_run=client.dry_run)

    msc_content = maunium_to_msc2545(recovered_pack)
    if existing_ponies_packs is not None and RECOVERED_PACK_ID not in existing_ponies_packs:
        client.put_room_state(
            primary_room_id,
            STATE_EVENT_PONIES,
            RECOVERED_PACK_ID,
            msc_content,
        )
        existing_ponies_packs[RECOVERED_PACK_ID] = msc_content

    if existing_v119_packs is not None and RECOVERED_PACK_ID not in existing_v119_packs:
        client.put_room_state(
            primary_room_id,
            STATE_EVENT_MATRIX_V119,
            RECOVERED_PACK_ID,
            msc_content,
        )
        existing_v119_packs[RECOVERED_PACK_ID] = msc_content

    return recovered_pack


def fetch_room_emote_packs(
    client: MatrixClient, primary_room_id: str
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Fetch all im.ponies.room_emotes and m.room.image_pack state events from a room."""
    state_events = client.get_room_state(primary_room_id)
    ponies_packs: dict[str, dict[str, Any]] = {}
    v119_packs: dict[str, dict[str, Any]] = {}

    for event in state_events:
        if not isinstance(event, dict):
            continue
        ev_type = event.get("type")
        if ev_type not in (STATE_EVENT_PONIES, STATE_EVENT_MATRIX_V119):
            continue
        state_key = str(event.get("state_key") or "")
        content = event.get("content")
        if not isinstance(content, dict) or not content.get("images"):
            continue
        if ev_type == STATE_EVENT_PONIES:
            ponies_packs[state_key] = content
        elif ev_type == STATE_EVENT_MATRIX_V119:
            v119_packs[state_key] = content

    return ponies_packs, v119_packs


def load_local_packs(packs_dir: Path) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Read web/packs/index.json (and any pack JSON files) from packs_dir."""
    index_path = packs_dir / "index.json"
    ordered_filenames: list[str] = []
    local_packs: dict[str, dict[str, Any]] = {}

    if index_path.is_file():
        try:
            index_data = json.loads(index_path.read_text(encoding="utf-8"))
            for pack_entry in index_data.get("packs", []):
                if isinstance(pack_entry, str) and pack_entry.endswith(".json"):
                    if pack_entry not in ordered_filenames:
                        ordered_filenames.append(pack_entry)
        except Exception as exc:
            logger.warning("Failed to parse %s: %s", index_path, exc)

    # Also discover any other local pack files in packs_dir (e.g., recovered.json)
    if packs_dir.is_dir():
        for file_path in sorted(packs_dir.glob("*.json")):
            if file_path.name == "index.json":
                continue
            if file_path.name not in ordered_filenames:
                ordered_filenames.append(file_path.name)

    for filename in ordered_filenames:
        pack_path = packs_dir / filename
        if not pack_path.is_file():
            continue
        try:
            data = json.loads(pack_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "stickers" in data:
                pack_id = str(data.get("id") or pack_path.stem)
                data["id"] = pack_id
                local_packs[pack_id] = data
        except Exception as exc:
            logger.warning("Skipping invalid pack file %s: %s", pack_path, exc)

    return ordered_filenames, local_packs


def write_json_file(path: Path, data: dict[str, Any], dry_run: bool = False) -> None:
    """Write formatted JSON to disk (unless dry_run is enabled)."""
    if dry_run:
        logger.info("[DRY-RUN] Would write %s", path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    logger.info("Wrote %s", path)


def state_key_to_pack_id(state_key: str) -> str:
    """Map a Matrix state_key to a safe pack_id / filename stem."""
    if not state_key:
        return "default"
    sanitized = re.sub(r"[^a-zA-Z0-9_.-]+", "_", state_key).strip(".")
    return sanitized or "default"


def sync_packs(
    client: MatrixClient,
    primary_room_id: str,
    room_ids: list[str],
    packs_dir: Path,
    homeserver: str,
    recover: bool = False,
    recover_limit: int = DEFAULT_RECOVER_LIMIT,
) -> None:
    """Execute recovery (optional) and two-way sync between local JSON and Matrix state."""
    ponies_packs, v119_packs = fetch_room_emote_packs(client, primary_room_id)
    logger.info(
        "Fetched state from primary room %s: %d %s pack(s), %d %s pack(s)",
        primary_room_id,
        len(ponies_packs),
        STATE_EVENT_PONIES,
        len(v119_packs),
        STATE_EVENT_MATRIX_V119,
    )

    recovered_in_this_run = False
    if recover:
        recovered_pack = recover_historical_stickers(
            client=client,
            room_ids=room_ids,
            primary_room_id=primary_room_id,
            packs_dir=packs_dir,
            max_events_per_room=recover_limit,
            existing_ponies_packs=ponies_packs,
            existing_v119_packs=v119_packs,
        )
        recovered_in_this_run = recovered_pack is not None

    ordered_filenames, local_packs = load_local_packs(packs_dir)
    if recovered_in_this_run and f"{RECOVERED_PACK_ID}.json" not in ordered_filenames:
        ordered_filenames.append(f"{RECOVERED_PACK_ID}.json")

    # Step 1: Local -> Matrix Room State
    # If a local Maunium pack does not exist in the room's im.ponies.room_emotes state,
    # convert its stickers array into MSC2545 format and PUT to both event types.
    existing_pack_ids_in_ponies = {
        state_key_to_pack_id(k): k for k in ponies_packs.keys()
    }
    for pack_id, pack_data in local_packs.items():
        if pack_id not in ponies_packs and pack_id not in existing_pack_ids_in_ponies:
            msc_content = maunium_to_msc2545(pack_data)
            logger.info(
                "Uploading local pack '%s' (%d stickers) to room %s state...",
                pack_id,
                len(msc_content.get("images", {})),
                primary_room_id,
            )
            client.put_room_state(
                primary_room_id,
                STATE_EVENT_PONIES,
                pack_id,
                msc_content,
            )
            client.put_room_state(
                primary_room_id,
                STATE_EVENT_MATRIX_V119,
                pack_id,
                msc_content,
            )
            ponies_packs[pack_id] = msc_content
            v119_packs[pack_id] = msc_content

    # Step 2: Matrix Room State -> Local Maunium JSON
    # For every im.ponies.room_emotes pack in the room:
    # - Ensure an identical m.room.image_pack state event exists in the room.
    # - Convert the pack into Maunium's JSON schema and write it to web/packs/{state_key}.json.
    final_pack_files: list[str] = []

    for filename in ordered_filenames:
        if (packs_dir / filename).is_file() and filename not in final_pack_files:
            final_pack_files.append(filename)

    for state_key, content in ponies_packs.items():
        if v119_packs.get(state_key) != content:
            logger.info(
                "Mirroring %s (state_key='%s') to %s in room %s",
                STATE_EVENT_PONIES,
                state_key,
                STATE_EVENT_MATRIX_V119,
                primary_room_id,
            )
            client.put_room_state(
                primary_room_id,
                STATE_EVENT_MATRIX_V119,
                state_key,
                content,
            )
            v119_packs[state_key] = content

        pack_id = state_key_to_pack_id(state_key)
        pack_filename = f"{pack_id}.json"
        if not (recovered_in_this_run and pack_id == RECOVERED_PACK_ID):
            maunium_pack = msc2545_to_maunium(pack_id, content)
            pack_path = packs_dir / pack_filename
            write_json_file(pack_path, maunium_pack, dry_run=client.dry_run)
        if pack_filename not in final_pack_files:
            final_pack_files.append(pack_filename)

    # Also mirror any m.room.image_pack state events that weren't in im.ponies.room_emotes
    for state_key, content in v119_packs.items():
        if state_key not in ponies_packs:
            logger.info(
                "Mirroring %s (state_key='%s') to %s in room %s",
                STATE_EVENT_MATRIX_V119,
                state_key,
                STATE_EVENT_PONIES,
                primary_room_id,
            )
            client.put_room_state(
                primary_room_id,
                STATE_EVENT_PONIES,
                state_key,
                content,
            )
            pack_id = state_key_to_pack_id(state_key)
            maunium_pack = msc2545_to_maunium(pack_id, content)
            pack_filename = f"{pack_id}.json"
            pack_path = packs_dir / pack_filename
            write_json_file(pack_path, maunium_pack, dry_run=client.dry_run)
            if pack_filename not in final_pack_files:
                final_pack_files.append(pack_filename)

    # Step 3: Update web/packs/index.json
    index_data = {
        "packs": final_pack_files,
        "homeserver_url": clean_url_value(homeserver),
    }
    write_json_file(packs_dir / "index.json", index_data, dry_run=client.dry_run)


def parse_room_ids(raw_rooms: str | None, override_room_id: str | None) -> list[str]:
    """Parse comma-separated room IDs and apply --room-id override if given."""
    rooms: list[str] = []
    if override_room_id:
        for part in override_room_id.split(","):
            part = part.strip()
            if part and part not in rooms:
                rooms.append(part)
    if raw_rooms:
        for part in raw_rooms.split(","):
            part = part.strip()
            if part and part not in rooms:
                rooms.append(part)
    return rooms


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Recover historical Matrix stickers and two-way sync MSC2545 / Matrix v1.19 "
            "sticker packs with Maunium stickerpicker static JSON files."
        )
    )
    parser.add_argument(
        "--recover",
        action="store_true",
        help="Paginate backwards through room history to recover m.sticker events.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying local JSON files or Matrix room state.",
    )
    parser.add_argument(
        "--room-id",
        dest="room_id",
        default=None,
        help="Primary Matrix room ID (or comma-separated room IDs). Overrides/prepends MATRIX_ROOM_IDS.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_RECOVER_LIMIT,
        help=f"Maximum historical events to scan per room during --recover (default: {DEFAULT_RECOVER_LIMIT}).",
    )
    parser.add_argument(
        "--packs-dir",
        default=None,
        help=f"Path to Maunium web/packs directory (default: env PACKS_DIR or {DEFAULT_PACKS_DIR}).",
    )
    parser.add_argument(
        "--homeserver",
        default=None,
        help=f"Matrix homeserver base URL (default: env MATRIX_HOMESERVER or {DEFAULT_HOMESERVER}).",
    )
    parser.add_argument(
        "--access-token",
        default=None,
        help="Matrix access token (default: env MATRIX_ACCESS_TOKEN).",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Optional path to a .env file.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    load_dotenv(Path(args.env_file) if args.env_file else None)

    homeserver = clean_url_value(
        args.homeserver or os.environ.get("MATRIX_HOMESERVER") or DEFAULT_HOMESERVER
    )
    access_token = (args.access_token or os.environ.get("MATRIX_ACCESS_TOKEN") or "").strip()
    packs_dir = Path(args.packs_dir or os.environ.get("PACKS_DIR") or DEFAULT_PACKS_DIR)
    room_ids = parse_room_ids(os.environ.get("MATRIX_ROOM_IDS"), args.room_id)

    if not access_token:
        logger.error("Missing Matrix access token. Set MATRIX_ACCESS_TOKEN in .env or pass --access-token.")
        return 1

    if not room_ids:
        logger.error("No Matrix room IDs configured. Set MATRIX_ROOM_IDS in .env or pass --room-id.")
        return 1

    primary_room_id = room_ids[0]
    client = MatrixClient(
        homeserver=homeserver,
        access_token=access_token,
        dry_run=args.dry_run,
    )

    try:
        sync_packs(
            client=client,
            primary_room_id=primary_room_id,
            room_ids=room_ids,
            packs_dir=packs_dir,
            homeserver=homeserver,
            recover=args.recover,
            recover_limit=args.limit,
        )
    except Exception as exc:
        logger.error("Sync failed: %s", exc)
        return 1

    logger.info("Sticker pack sync complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
