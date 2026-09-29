from __future__ import annotations

import re
from urllib.parse import urlsplit

import httpx

from social_crawler.domain.models import CollectionError

DEFAULT_ADSPOWER_API_URL = "http://127.0.0.1:50325"
PROFILE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
# A container has no microphone, camera or speaker; an empty device list does
# not match the macOS fingerprint, so expose one fake device of each kind.
LAUNCH_ARGS = ("--use-fake-device-for-media-stream",)
# Under Xvfb there is no GPU, so headful Chrome disables WebGL entirely unless
# it is told to use software GL (the profile masks the renderer string). The
# window fits inside the 1920x1080 Xvfb screen in deploy/k8s/base/app.yaml, which
# matches the profile's spoofed screen; AdsPower otherwise opens 780x580.
HEADFUL_LAUNCH_ARGS = LAUNCH_ARGS + (
    "--use-gl=angle",
    "--use-angle=swiftshader",
    "--enable-unsafe-swiftshader",
    "--window-size=1440,900",
    "--window-position=0,0",
)
INACTIVE_STATUSES = {"", "0", "false", "inactive", "none", "closed"}


def _loopback_url(value: str, *, websocket: bool = False) -> str:
    parsed = urlsplit(value)
    schemes = {"ws"} if websocket else {"http"}
    if (
        parsed.scheme not in schemes
        or parsed.hostname not in LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (not websocket and parsed.path not in {"", "/"})
    ):
        raise ValueError("AdsPower endpoints must use an unauthenticated loopback URL")
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        raise ValueError("AdsPower endpoints must include a port")
    if websocket and not parsed.path:
        raise ValueError("AdsPower CDP endpoint must include a WebSocket path")
    return value.rstrip("/") if not websocket else value


def validate_adspower_api_url(value: str) -> str:
    return _loopback_url(value)


def validate_adspower_profile_id(value: str) -> str:
    value = value.strip()
    if not PROFILE_ID.fullmatch(value):
        raise ValueError("AdsPower profile ID is invalid")
    return value


def adspower_resource_key(profile_id: str) -> str:
    return "adspower-profile:" + validate_adspower_profile_id(profile_id)


class AdsPowerClient:
    """Small, fail-closed client for the loopback-only AdsPower Local API."""

    def __init__(
        self,
        base_url: str = DEFAULT_ADSPOWER_API_URL,
        *,
        timeout: float = 90,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = validate_adspower_api_url(base_url)
        self.timeout = timeout
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout),
            trust_env=False,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _call(
        self, method: str, path: str, *, json: dict | None = None, params: dict | None = None
    ) -> dict:
        try:
            response = await self.client.request(method, path, json=json, params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CollectionError(
                "network_failure", "AdsPower Local API is unavailable"
            ) from exc
        if not isinstance(payload, dict) or payload.get("code") != 0:
            message = str(payload.get("msg", "")) if isinstance(payload, dict) else ""
            if "being used by" in message or "not allowed to open" in message:
                # The message names the other AdsPower user; never persist it.
                raise CollectionError(
                    "environment_in_use", "AdsPower profile is open on another device"
                )
            raise CollectionError(
                "network_failure", "AdsPower Local API rejected the browser operation"
            )
        return payload

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        data = (await self._call(method, path, **kwargs)).get("data")
        return data if isinstance(data, dict) else {}

    async def _ensure_available(self, profile_id: str) -> None:
        # Another device keeps its own session and possibly its own egress. Two
        # live copies of one account are a risk signal, so refuse to join them.
        try:
            payload = await self._call(
                "POST", "/api/v1/browser/cloud-active", json={"user_ids": profile_id}
            )
        except CollectionError as exc:
            if exc.kind == "environment_in_use":
                raise
            payload = {}  # Advisory only: the start call rejects foreign owners too.
        rows = payload.get("data")
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or row.get("user_id", profile_id) != profile_id:
                continue
            # Observed rows carry only user_id and the owner's account; treat a
            # listed profile as occupied unless it explicitly reports inactive.
            if "status" not in row or (
                str(row["status"]).strip().lower() not in INACTIVE_STATUSES
            ):
                raise CollectionError(
                    "environment_in_use", "AdsPower profile is open on another device"
                )
        # The profile lock guarantees no live local owner; an active local
        # browser is left over from an interrupted worker.
        local = await self._request(
            "GET", "/api/v2/browser-profile/active", params={"profile_id": profile_id}
        )
        if str(local.get("status", "")).lower() == "active":
            await self.stop_profile(profile_id)

    async def check_status(self) -> None:
        await self._request("GET", "/status")

    async def list_profiles(self) -> list[dict]:
        """Return a safe, complete profile inventory without exposing credentials."""
        profiles = []
        page = 1
        while True:
            data = await self._request(
                "POST",
                "/api/v2/browser-profile/list",
                json={"page": page, "limit": 200},
            )
            rows = data.get("list")
            if not isinstance(rows, list):
                raise CollectionError(
                    "network_failure", "AdsPower returned an invalid profile list"
                )
            for row in rows:
                if not isinstance(row, dict):
                    continue
                profile_id = row.get("profile_id") or row.get("user_id")
                try:
                    profile_id = validate_adspower_profile_id(profile_id)
                except (AttributeError, ValueError):
                    continue
                name = str(row.get("name") or row.get("profile_name") or "未命名环境")
                state = str(row.get("status") or row.get("active") or "").lower()
                details = []
                if profile_no := row.get("profile_no") or row.get("serial_number"):
                    details.append(f"No. {profile_no}")
                if group := row.get("group_name"):
                    details.append(str(group))
                profiles.append(
                    {
                        "id": profile_id,
                        "name": name[:100],
                        "state": state[:40],
                        "detail": " · ".join(details)[:160],
                        "compatible": True,
                    }
                )
            try:
                total_pages = int(data.get("total_pages") or 1)
            except (TypeError, ValueError):
                total_pages = 1
            if page >= max(1, total_pages):
                break
            page += 1
        return profiles

    async def start_profile(self, profile_id: str, *, headless: bool, display: str | None = None) -> str:
        profile_id = validate_adspower_profile_id(profile_id)
        await self._ensure_available(profile_id)
        body = {
            "profile_id": profile_id,
            "headless": "1" if headless else "0",
            "last_opened_tabs": "0",
            "launch_args": list(LAUNCH_ARGS if headless else HEADFUL_LAUNCH_ARGS),
        }
        if display is not None:
            if not re.fullmatch(r":[0-9]+", display):
                raise ValueError("Invalid X11 display")
            body["launch_args"].extend([f"--display={display}", "--start-maximized"])
        data = await self._request("POST", "/api/v2/browser-profile/start", json=body)
        ws = data.get("ws")
        endpoint = ws.get("puppeteer") if isinstance(ws, dict) else None
        if not isinstance(endpoint, str):
            raise CollectionError(
                "network_failure", "AdsPower did not return a CDP endpoint"
            )
        try:
            return _loopback_url(endpoint, websocket=True)
        except ValueError as exc:
            raise CollectionError(
                "network_failure", "AdsPower returned an unsafe CDP endpoint"
            ) from exc

    async def stop_profile(self, profile_id: str) -> None:
        await self._request(
            "POST",
            "/api/v2/browser-profile/stop",
            json={"profile_id": validate_adspower_profile_id(profile_id)},
        )
