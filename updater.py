"""Check for and download UTT updates from GitHub releases."""
from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path
from urllib.error import HTTPError

API_URL = "https://api.github.com/repos/Aughhhhhhh/UTT/releases/latest"
USER_AGENT = "UTT-Updater"
CHUNK_SIZE = 64 * 1024


class UpdateError(Exception):
    pass


class UpdateCancelled(UpdateError):
    pass


def api_url() -> str:
    """Release API endpoint; UTT_UPDATE_URL overrides it for testing."""
    return os.environ.get("UTT_UPDATE_URL", API_URL)


def parse_version(text: str) -> tuple:
    """'v1.1.9' -> (1, 1, 9). Non-numeric chunks become 0."""
    parts = []
    for chunk in text.strip().lstrip("vV").replace("-", ".").split("."):
        digits = "".join(char for char in chunk if char.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


def fetch_latest(timeout: float = 10.0):
    """Return the newest release dict, or None when the repo has no release.

    Raises on network errors; only a missing release (HTTP 404/422) or an
    empty payload yields None.
    """
    request = urllib.request.Request(
        api_url(),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        if error.code in (404, 422):
            return None
        raise
    tag = payload.get("tag_name") or payload.get("name")
    if not tag:
        return None
    assets = payload.get("assets") or []
    return {
        "version": str(tag),
        "name": payload.get("name") or str(tag),
        "body": payload.get("body") or "",
        "html_url": payload.get("html_url") or "",
        "assets": [
            {
                "name": asset.get("name", ""),
                "url": asset.get("browser_download_url", ""),
                "size": int(asset.get("size") or 0),
            }
            for asset in assets
            if asset.get("browser_download_url")
        ],
    }


def find_installer_asset(release) -> dict | None:
    """Prefer the UTT-Setup-*.exe asset; fall back to any .exe asset."""
    assets = release.get("assets") or []
    preferred = [
        asset for asset in assets
        if asset["name"].lower().startswith("utt-setup")
        and asset["name"].lower().endswith(".exe")
    ]
    if preferred:
        return preferred[0]
    fallback = [
        asset for asset in assets if asset["name"].lower().endswith(".exe")
    ]
    return fallback[0] if fallback else None


def download_file(url, dest, expected_size=None, timeout=60.0, progress=None) -> Path:
    """Stream url to dest; progress(done, total) returning False cancels."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    destination = Path(dest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    done = 0
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            total = expected_size or response.length
            with open(destination, "wb") as handle:
                while True:
                    chunk = response.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    handle.write(chunk)
                    done += len(chunk)
                    if progress is not None and progress(done, total) is False:
                        raise UpdateCancelled("Download cancelled.")
    except Exception:
        try:
            destination.unlink()
        except OSError:
            pass
        raise
    actual = destination.stat().st_size
    if expected_size and actual != expected_size:
        destination.unlink()
        raise UpdateError(
            f"Download size mismatch: expected {expected_size}, got {actual}."
        )
    return destination
