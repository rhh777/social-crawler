from __future__ import annotations

import re
import uuid
from urllib.parse import urlsplit, urlunsplit

import httpx

from social_crawler.domain.models import CollectionError

DEFAULT_KAMELEO_API_URL = "http://127.0.0.1:5050"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
SUPPORTED_LIFETIME_STATES = {"created", "terminated"}
BUSY_LIFETIME_STATES = {"locked", "loading", "running", "starting", "terminating"}
BUSY_ERROR_CODES = {
    "profile_already_running",
    "profile_locked",
    "profile_running",
    "profile_syncing",
}
QUOTA_ERROR_CODES = {
    "profile_minutes_limit_reached",
    "running_mobile_profiles_limit_reached",
    "running_profiles_limit_reached",
}


def validate_kameleo_api_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Kameleo endpoints must use an unauthenticated loopback URL")
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None:
        raise ValueError("Kameleo endpoints must include a port")
    return value.rstrip("/")


def validate_kameleo_profile_id(value: str) -> str:
    try:
        profile_id = str(uuid.UUID(value.strip()))
    except (AttributeError, ValueError):
        raise ValueError("Kameleo profile ID must be a UUID") from None
    return profile_id


def kameleo_resource_key(profile_id: str) -> str:
    return "kameleo-profile:" + validate_kameleo_profile_id(profile_id)


class KameleoClient:
    """Minimal async client for a loopback-only Kameleo Chroma runtime."""

    def __init__(
        self,
        base_url: str = DEFAULT_KAMELEO_API_URL,
        *,
        timeout: float = 90,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = validate_kameleo_api_url(base_url)
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

    @staticmethod
    def _error(payload: object) -> CollectionError:
        code = str(payload.get("errorCode", "")) if isinstance(payload, dict) else ""
        if code in BUSY_ERROR_CODES:
            return CollectionError(
                "environment_in_use", "Kameleo profile is open or locked elsewhere"
            )
        if code in QUOTA_ERROR_CODES:
            return CollectionError("quota_exhausted", "Kameleo browser quota is exhausted")
        if code == "proxy_connection_issue":
            return CollectionError("proxy_unavailable", "Kameleo profile proxy is unavailable")
        if code == "rate_limit_exceeded":
            return CollectionError("rate_limit", "Kameleo Local API rate limit was reached")
        if code == "no_headless_capability":
            return CollectionError("access_denied", "Kameleo headless mode is unavailable")
        return CollectionError(
            "network_failure", "Kameleo Local API rejected the browser operation"
        )

    async def _call(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        ignored_error_codes: frozenset[str] = frozenset(),
    ) -> dict:
        payload = await self._call_payload(
            method,
            path,
            json=json,
            ignored_error_codes=ignored_error_codes,
        )
        if not isinstance(payload, dict):
            raise CollectionError(
                "network_failure", "Kameleo Local API returned an invalid response"
            )
        return payload

    async def _call_payload(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        ignored_error_codes: frozenset[str] = frozenset(),
    ) -> object:
        try:
            response = await self.client.request(method, path, json=json)
            try:
                payload = response.json()
            except ValueError:
                payload = None
        except httpx.HTTPError as exc:
            raise CollectionError(
                "network_failure", "Kameleo Local API is unavailable"
            ) from exc
        if response.is_error:
            code = str(payload.get("errorCode", "")) if isinstance(payload, dict) else ""
            if code in ignored_error_codes:
                return {}
            raise self._error(payload)
        return payload

    async def check_status(self) -> None:
        await self._call("GET", "/general/user-info")

    async def list_profiles(self) -> list[dict]:
        """Return safe profile metadata from the active Kameleo workspace."""
        payload = await self._call_payload("GET", "/profiles")
        if not isinstance(payload, list):
            raise CollectionError(
                "network_failure", "Kameleo Local API returned an invalid profile list"
            )
        profiles = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            try:
                profile_id = validate_kameleo_profile_id(row.get("id"))
            except (AttributeError, ValueError):
                continue
            fingerprint = row.get("fingerprint")
            fingerprint = fingerprint if isinstance(fingerprint, dict) else {}
            browser = row.get("browser") or fingerprint.get("browser")
            product = browser.get("product") if isinstance(browser, dict) else browser
            device = row.get("device") or fingerprint.get("device")
            device_type = device.get("type") if isinstance(device, dict) else device
            status = row.get("status")
            state = (
                status.get("lifetimeState")
                if isinstance(status, dict)
                else row.get("lifetimeState") or status
            )
            storage = row.get("storage")
            storage = storage.get("value") if isinstance(storage, dict) else storage
            details = [
                value
                for value in (
                    str(product).title() if product else "",
                    str(device_type).title() if device_type else "",
                    str(storage).title() if storage else "",
                )
                if value
            ]
            profiles.append(
                {
                    "id": profile_id,
                    "name": str(row.get("name") or "未命名环境")[:100],
                    "state": str(state or "").lower()[:40],
                    "detail": " · ".join(details)[:160],
                    "compatible": (
                        str(product or "").lower() == "chrome"
                        and str(device_type or "").lower() == "desktop"
                    ),
                }
            )
        return profiles

    async def _validate_profile(self, profile_id: str) -> None:
        profile = await self._call("GET", f"/profiles/{profile_id}")
        fingerprint = profile.get("fingerprint")
        fingerprint = fingerprint if isinstance(fingerprint, dict) else {}
        browser = profile.get("browser") or fingerprint.get("browser")
        product = browser.get("product") if isinstance(browser, dict) else browser
        device = profile.get("device") or fingerprint.get("device")
        device_type = device.get("type") if isinstance(device, dict) else device
        if str(product or "").lower() != "chrome" or str(device_type or "").lower() != "desktop":
            raise CollectionError(
                "network_failure",
                "Kameleo provider currently requires a desktop Chrome profile",
            )

    async def _ensure_available(self, profile_id: str) -> None:
        status = await self._call("GET", f"/profiles/{profile_id}/status")
        state = str(status.get("lifetimeState", "")).strip().lower()
        if state in BUSY_LIFETIME_STATES:
            raise CollectionError(
                "environment_in_use", "Kameleo profile is open or locked elsewhere"
            )
        if state not in SUPPORTED_LIFETIME_STATES:
            raise CollectionError(
                "network_failure", "Kameleo profile has an unsupported lifecycle state"
            )

    async def start_profile(
        self, profile_id: str, *, headless: bool, display: str | None = None
    ) -> str:
        profile_id = validate_kameleo_profile_id(profile_id)
        await self._validate_profile(profile_id)
        await self._ensure_available(profile_id)
        arguments = ["--headless"] if headless else []
        if display is not None:
            if not re.fullmatch(r":[0-9]+", display):
                raise ValueError("Invalid X11 display")
            arguments.extend([f"--display={display}", "--start-maximized"])
        await self._call(
            "POST",
            f"/profiles/{profile_id}/start",
            json={"arguments": arguments},
        )
        parsed = urlsplit(self.base_url)
        return urlunsplit(("ws", parsed.netloc, f"/playwright/{profile_id}", "", ""))

    async def stop_profile(self, profile_id: str) -> None:
        profile_id = validate_kameleo_profile_id(profile_id)
        await self._call(
            "POST",
            f"/profiles/{profile_id}/stop",
            ignored_error_codes=frozenset({"profile_not_running"}),
        )
