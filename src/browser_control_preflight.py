"""Read-only compatibility checks for the dedicated browser-control endpoint."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass


BROWSER_CONTROL_PORT = int(os.environ.get("BROWSER_CONTROL_PORT", "9223"))
DEFAULT_ENDPOINT = f"http://127.0.0.1:{BROWSER_CONTROL_PORT}/json/version"
RESTART_COMMAND = "./scripts/browser-control-stop.sh && ./scripts/browser-control-launch.sh"


@dataclass(frozen=True)
class BrowserControlStatus:
    ready: bool
    message: str
    browser_version: str | None = None
    driver_version: str | None = None


def chromedriver_path() -> str | None:
    """Return the driver executable whose version the preflight checks."""
    return shutil.which("chromedriver")


def _major(version_text: str | None) -> int | None:
    if not version_text:
        return None
    match = re.search(r"(?:Chrome(?:Driver)?/|ChromeDriver\s+)?(\d+)\.", version_text)
    return int(match.group(1)) if match else None


def inspect_browser_control(
    timeout: float = 1.5,
    endpoint: str = DEFAULT_ENDPOINT,
    chromedriver: str | None = None,
) -> BrowserControlStatus:
    """Check endpoint availability and the attached browser/driver major pair."""
    try:
        with urllib.request.urlopen(endpoint, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return BrowserControlStatus(False, f"Controlled browser is unavailable on port {BROWSER_CONTROL_PORT}: {exc}")

    browser_version = str(data.get("Browser") or "")
    if not data.get("webSocketDebuggerUrl"):
        return BrowserControlStatus(False, "Controlled browser endpoint has no debugger WebSocket.", browser_version)

    driver_path = chromedriver or chromedriver_path()
    if not driver_path:
        return BrowserControlStatus(False, "ChromeDriver is not available on PATH.", browser_version)
    try:
        result = subprocess.run(
            [driver_path, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return BrowserControlStatus(False, f"ChromeDriver version could not be read: {exc}", browser_version)

    driver_version = (result.stdout or result.stderr).strip()
    browser_major = _major(browser_version)
    driver_major = _major(driver_version)
    if browser_major is None or driver_major is None:
        return BrowserControlStatus(
            False,
            f"Could not identify browser/driver versions ({browser_version!r}, {driver_version!r}).",
            browser_version,
            driver_version,
        )
    if browser_major != driver_major:
        return BrowserControlStatus(
            False,
            (
                f"Controlled browser/ChromeDriver major mismatch: browser {browser_major}, driver {driver_major}. "
                f"Restart the dedicated browser with: {RESTART_COMMAND}"
            ),
            browser_version,
            driver_version,
        )
    return BrowserControlStatus(
        True,
        f"Controlled browser ready (browser/driver {browser_major}).",
        browser_version,
        driver_version,
    )
