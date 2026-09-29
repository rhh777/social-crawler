import fcntl
import hashlib
import json
import os
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from social_crawler.adapters.xhs.sites import is_xhs_platform, site_for
from social_crawler.domain.models import CollectionError
from social_crawler.environments.adspower import (
    DEFAULT_ADSPOWER_API_URL,
    validate_adspower_api_url,
    validate_adspower_profile_id,
)
from social_crawler.environments.browser_runtime import (
    CURL_CFFI_IMPERSONATE,
    DEFAULT_UA,
)
from social_crawler.environments.environment_snapshot import (
    effective_user_agent,
    load_environment_snapshot,
)
from social_crawler.environments.kameleo import (
    DEFAULT_KAMELEO_API_URL,
    validate_kameleo_api_url,
    validate_kameleo_profile_id,
)
from social_crawler.environments.managed_browser import (
    is_managed_browser_provider,
    managed_browser_supports_platform,
)

BROWSER_VIEWPORT = {"width": 1440, "height": 1000}


class EnvironmentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_ref: str = "primary"
    cookie_file: str = ""
    profile_dir: str = "profiles/xhs-primary"
    user_agent: str | None = None
    browser_channel: str | None = None
    browser_provider: Literal["chromium", "adspower", "kameleo"] = "chromium"
    adspower_profile_id: str | None = None
    adspower_api_url: str = Field(
        default_factory=lambda: os.environ.get("CRAWLER_ADSPOWER_API_URL")
        or DEFAULT_ADSPOWER_API_URL
    )
    adspower_start_timeout: int = Field(
        default_factory=lambda: os.environ.get("CRAWLER_ADSPOWER_START_TIMEOUT") or 90,
        validate_default=True, ge=5, le=900,
    )
    kameleo_profile_id: str | None = None
    kameleo_api_url: str = Field(
        default_factory=lambda: os.environ.get("CRAWLER_KAMELEO_API_URL")
        or DEFAULT_KAMELEO_API_URL
    )
    kameleo_start_timeout: int = Field(
        default_factory=lambda: os.environ.get("CRAWLER_KAMELEO_START_TIMEOUT") or 90,
        validate_default=True, ge=5, le=900,
    )
    headless: bool = False
    proxy_id: str | None = None
    proxy_env: str | None = None
    proxy_file: str | None = None
    expected_user_id: str | None = None
    session_version: str = "1"
    binding_version: str = "1"
    impersonate: str = CURL_CFFI_IMPERSONATE
    douyin_webid: str = ""
    auto_session_recovery: bool = True
    consistency_policy: Literal["off", "warn", "strict"] = "warn"
    locale: str | None = None
    timezone_id: str | None = None

    @model_validator(mode="after")
    def validate_browser_provider(self):
        self.adspower_api_url = validate_adspower_api_url(self.adspower_api_url)
        self.kameleo_api_url = validate_kameleo_api_url(self.kameleo_api_url)
        if self.adspower_profile_id:
            self.adspower_profile_id = validate_adspower_profile_id(
                self.adspower_profile_id
            )
        if self.kameleo_profile_id:
            self.kameleo_profile_id = validate_kameleo_profile_id(
                self.kameleo_profile_id
            )
        if self.browser_provider == "adspower" and not self.adspower_profile_id:
            raise ValueError("AdsPower provider requires adspower_profile_id")
        if self.browser_provider == "kameleo" and not self.kameleo_profile_id:
            raise ValueError("Kameleo provider requires kameleo_profile_id")
        return self


class Session:
    def __init__(
        self,
        config: EnvironmentConfig,
        platform: str,
        *,
        offline: bool = False,
        profile_auth: bool = False,
    ):
        if is_managed_browser_provider(
            config.browser_provider
        ) and not managed_browser_supports_platform(config.browser_provider, platform):
            provider = "AdsPower" if config.browser_provider == "adspower" else "Kameleo"
            raise ValueError(f"{provider} browser provider does not support {platform}")
        if profile_auth and config.browser_provider not in {"adspower", "kameleo"}:
            raise ValueError("Profile-managed authentication requires a managed browser")
        if not offline and not config.expected_user_id:
            # The receipt is a local identity binding, never a credential source.
            from social_crawler.environments.session_recovery import read_recovery_receipt

            if user_id := read_recovery_receipt(config.profile_dir).get("user_id"):
                config = config.model_copy(update={"expected_user_id": user_id})
        self.config = config
        self.platform = platform
        self.profile_auth = profile_auth
        self.configured_proxy = load_proxy(
            proxy_env=config.proxy_env, proxy_file=config.proxy_file, apply_relay=False
        )
        self.proxy = load_proxy(proxy_env=config.proxy_env, proxy_file=config.proxy_file)
        if offline:
            self.environment_snapshot, self.environment_snapshot_error = None, "offline"
        else:
            self.environment_snapshot, self.environment_snapshot_error = load_environment_snapshot(
                config.profile_dir
            )
        self.cookies = (
            []
            if offline or profile_auth
            else load_cookies(Path(config.cookie_file), platform)
        )
        if not offline and not profile_auth:
            names = {c["name"] for c in self.cookies}
            required = (
                site_for(platform).local_session_cookies
                if is_xhs_platform(platform)
                else {"sessionid", "sessionid_ss"}
            )
            if not names.intersection(required):
                raise ValueError(f"Cookie file lacks a {platform} login-session cookie")
        fingerprint = json.dumps(
            {
                "platform": platform,
                "environment": config.model_dump(exclude={"cookie_file"}),
                "auth_source": "browser_profile" if profile_auth else "cookie_file",
                "cookies": self.cookies,
                "proxy": self.proxy,
                "environment_snapshot": self.environment_snapshot,
            },
            sort_keys=True,
        )
        self.binding = hashlib.sha256(fingerprint.encode()).hexdigest()
        self.cookie_digest = self._cookie_digest() if not offline and not profile_auth else None

    def _cookie_digest(self):
        return hashlib.sha256(Path(self.config.cookie_file).read_bytes()).hexdigest()

    def check_unchanged(self):
        if self.cookie_digest:
            try:
                changed = self._cookie_digest() != self.cookie_digest
            except OSError:
                changed = True
            if changed:
                raise CollectionError(
                    "session_changed", "Cookie file changed; resume with a new context"
                )

    @property
    def cookie_header(self):
        return "; ".join(f"{c['name']}={c['value']}" for c in self.cookies)

    @property
    def effective_user_agent(self):
        return effective_user_agent(
            self.config.user_agent,
            self.environment_snapshot,
            DEFAULT_UA,
        )

    @property
    def browser_locale(self):
        if self.config.locale:
            return self.config.locale
        navigator = (self.environment_snapshot or {}).get("navigator") or {}
        if language := str(navigator.get("language") or "").strip():
            return language
        return "zh-CN" if self.platform == "xhs" else None

    @property
    def browser_timezone(self):
        if self.config.timezone_id:
            return self.config.timezone_id
        if timezone := str((self.environment_snapshot or {}).get("timezone") or "").strip():
            return timezone
        return "Asia/Shanghai" if self.platform == "xhs" else None

    def cookie_value(self, name):
        return next((c["value"] for c in self.cookies if c["name"] == name), "")


def _proxy_via_host_relay(value: str) -> str:
    relay_host = os.environ.get("CRAWLER_PROXY_RELAY_HOST", "").strip()
    route_map = os.environ.get("CRAWLER_PROXY_RELAY_MAP", "").strip()
    if not value or not relay_host or not route_map:
        return value
    parsed = urlsplit(value)
    if not parsed.hostname or not parsed.port:
        return value
    endpoint = f"{parsed.hostname.lower()}:{parsed.port}"
    relay_port = None
    for item in route_map.split(","):
        try:
            upstream, port_text = item.strip().rsplit("=", 1)
            port = int(port_text)
        except (ValueError, AttributeError):
            raise ValueError("CRAWLER_PROXY_RELAY_MAP contains an invalid route") from None
        if not 1 <= port <= 65535 or ":" not in upstream:
            raise ValueError("CRAWLER_PROXY_RELAY_MAP contains an invalid route")
        if upstream.lower() == endpoint:
            relay_port = port
    if relay_port is None:
        return value
    relay_address = f"[{relay_host}]" if ":" in relay_host else relay_host
    credentials = parsed.netloc.rsplit("@", 1)[0] + "@" if "@" in parsed.netloc else ""
    # These crawling proxies must resolve platform domains at the upstream. Local
    # DNS mode is known to be reset by some residential SOCKS providers.
    scheme = "socks5h" if parsed.scheme == "socks5" else parsed.scheme
    return urlunsplit(
        (scheme, f"{credentials}{relay_address}:{relay_port}", parsed.path, parsed.query, parsed.fragment)
    )


def load_proxy(
    *,
    proxy_env: str | None = None,
    proxy_file: str | None = None,
    apply_relay: bool = True,
) -> str:
    if proxy_env and proxy_file:
        raise ValueError("Configure only one of proxy_env and proxy_file")
    if proxy_env:
        value = os.environ.get(proxy_env, "").strip()
        if not value:
            raise ValueError(
                "Configured proxy environment variable is empty; direct fallback refused"
            )
        value = validate_proxy_url(value)
        return _proxy_via_host_relay(value) if apply_relay else value
    if proxy_file:
        path = Path(proxy_file)
        if not path.is_file():
            raise ValueError("Configured proxy file is missing; direct fallback refused")
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError("Configured proxy file is empty; direct fallback refused")
        value = validate_proxy_url(value)
        return _proxy_via_host_relay(value) if apply_relay else value
    return ""


def validate_proxy_url(value: str) -> str:
    if "\\@" in value or "&nbsp;" in value.lower() or "&#" in value:
        raise ValueError("Proxy URL contains escaped or HTML-encoded characters")
    if any(character.isspace() for character in value):
        raise ValueError("Proxy URL must not contain whitespace")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https", "socks5", "socks5h"}:
        raise ValueError("Proxy URL uses an unsupported scheme")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("Proxy URL contains an invalid port") from None
    if not parsed.hostname or port is None:
        raise ValueError("Proxy URL must include a hostname and port")
    return value


def load_cookies(path: Path, platform: str) -> list[dict]:
    if not path.is_file():
        raise ValueError("Cookie file is missing; pass --cookies with a local file path")
    text = path.read_text().strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = [
            {"name": p.split("=", 1)[0].strip(), "value": p.split("=", 1)[1].strip()}
            for p in text.removeprefix("Cookie:").removeprefix("cookie:").split(";")
            if "=" in p
        ]
    if isinstance(data, dict) and "cookies" in data:
        data = data["cookies"]
    elif isinstance(data, dict):
        data = [{"name": k, "value": v} for k, v in data.items()]
    if not isinstance(data, list):
        raise ValueError(
            "Cookie file must contain a Cookie header, mapping, or browser cookie array"
        )
    domain = site_for(platform).cookie_domain if is_xhs_platform(platform) else "douyin.com"
    result = []
    for entry in data:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("name"), str)
            or not isinstance(entry.get("value"), str)
        ):
            raise ValueError("Invalid cookie entry")
        d = entry.get("domain", "." + domain)
        if d.lstrip(".") != domain and not d.lstrip(".").endswith("." + domain):
            continue
        expiry = float(entry.get("expires") or -1)
        if expiry > 0 and expiry < time.time():
            continue
        clean = {
            "name": entry["name"],
            "value": entry["value"],
            "domain": d,
            "path": entry.get("path", "/"),
            "secure": entry.get("secure", True),
            "httpOnly": entry.get("httpOnly", False),
        }
        if entry.get("sameSite") in {"Strict", "Lax", "None"}:
            clean["sameSite"] = entry["sameSite"]
        if expiry > 0:
            clean["expires"] = expiry
        result.append(clean)
    return result


def filter_storage_origins(origins, platform: str) -> list[dict]:
    """Keep valid Playwright localStorage entries for the selected platform only."""

    if origins is None:
        return []
    if not isinstance(origins, list):
        raise ValueError("Browser storage origins must be an array")
    domain = site_for(platform).cookie_domain if is_xhs_platform(platform) else "douyin.com"
    result = []
    for entry in origins:
        if not isinstance(entry, dict) or not isinstance(entry.get("origin"), str):
            raise ValueError("Invalid browser storage origin")
        hostname = (urlsplit(entry["origin"]).hostname or "").lower()
        if hostname != domain and not hostname.endswith("." + domain):
            continue
        values = entry.get("localStorage", [])
        if not isinstance(values, list):
            raise ValueError("Browser localStorage must be an array")
        local_storage = []
        for value in values:
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("name"), str)
                or not isinstance(value.get("value"), str)
            ):
                raise ValueError("Invalid browser localStorage entry")
            local_storage.append({"name": value["name"], "value": value["value"]})
        result.append({"origin": entry["origin"], "localStorage": local_storage})
    return result


def load_storage_origins(path: Path, platform: str) -> list[dict]:
    """Load the localStorage portion of a Playwright storage-state file, if present."""

    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict) or "origins" not in data:
        return []
    return filter_storage_origins(data["origins"], platform)


@contextmanager
def exclusive(keys: list[str], lock_dir: Path | None = None):
    """OS locks die with the process; never reclaim a live browser by timeout."""
    folder = lock_dir or Path.home() / ".cache/social-crawler/locks"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with ExitStack() as stack:
        for key in sorted(set(keys)):
            path = folder / (hashlib.sha256(key.encode()).hexdigest() + ".lock")
            handle = stack.enter_context(path.open("a+"))
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise CollectionError(
                    "environment_busy", "Another worker owns this database/account/profile"
                ) from exc
        yield
