"""No platform traffic: synthetic identities, private temporary state, real Chromium isolation."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from social_crawler.domain.models import CollectionError
from social_crawler.environments import cookie_capture as capture
from social_crawler.environments.session import EnvironmentConfig, Session, exclusive
from social_crawler.environments.session_recovery import (
    bind_profile,
    clear_platform_auth,
    identity_from_payload,
    read_recovery_receipt,
    recovery_key,
    verified_storage_state,
)


def cookie(platform="xhs", name=None, value="new"):
    defaults = {
        "xhs": ("web_session", ".xiaohongshu.com"),
        "rednote": ("web_session", ".rednote.com"),
        "douyin": ("sessionid", ".douyin.com"),
    }
    default_name, domain = defaults[platform]
    return {
        "name": name or default_name,
        "value": value,
        "domain": domain,
        "path": "/",
        "secure": True,
        "httpOnly": True,
    }


@pytest.mark.parametrize("platform", ["xhs", "rednote", "douyin"])
@pytest.mark.parametrize(
    "current, supplied, expected",
    [
        ("same", "same", None),
        ("new", "old", "auth_expired"),
    ],
)
async def test_bind_preserves_profile(platform, current, supplied, expected):
    context = SimpleNamespace(
        cookies=AsyncMock(return_value=[cookie(platform, value=current)]),
        clear_cookies=AsyncMock(),
        add_cookies=AsyncMock(),
    )
    session = SimpleNamespace(platform=platform, cookies=[cookie(platform, value=supplied)])
    if expected:
        with pytest.raises(CollectionError) as failure:
            await bind_profile(context, session)
        assert failure.value.kind == expected
    else:
        assert not await bind_profile(context, session)
    context.clear_cookies.assert_not_awaited()
    context.add_cookies.assert_not_awaited()


async def test_existing_guest_profile_is_not_seeded():
    context = SimpleNamespace(
        cookies=AsyncMock(return_value=[cookie(name="a1")]), add_cookies=AsyncMock()
    )
    with pytest.raises(CollectionError, match="Existing profile"):
        await bind_profile(context, SimpleNamespace(platform="xhs", cookies=[cookie()]))
    context.add_cookies.assert_not_awaited()


async def test_managed_profile_auth_uses_browser_session_without_cookie_comparison():
    context = SimpleNamespace(
        cookies=AsyncMock(return_value=[cookie("xhs", value="profile-login")]),
        add_cookies=AsyncMock(),
    )
    session = SimpleNamespace(
        platform="xhs",
        cookies=[],
        profile_auth=True,
    )

    assert not await bind_profile(context, session)
    context.add_cookies.assert_not_awaited()


async def test_managed_profile_auth_rejects_a_logged_out_browser_profile():
    context = SimpleNamespace(cookies=AsyncMock(return_value=[]), add_cookies=AsyncMock())
    session = SimpleNamespace(platform="xhs", cookies=[], profile_auth=True)

    with pytest.raises(CollectionError) as failure:
        await bind_profile(context, session)
    assert failure.value.kind == "auth_expired"
    context.add_cookies.assert_not_awaited()


@pytest.mark.parametrize("platform", ["xhs", "rednote", "douyin"])
async def test_empty_profile_can_import_explicit_file(platform):
    context = SimpleNamespace(cookies=AsyncMock(return_value=[]), add_cookies=AsyncMock())
    supplied = [cookie(platform)]
    assert await bind_profile(context, SimpleNamespace(platform=platform, cookies=supplied))
    context.add_cookies.assert_awaited_once_with(supplied)


@pytest.mark.parametrize(
    "payload, error",
    [
        ({"success": True, "data": {"guest": True, "user_id": "visitor"}}, "auth_expired"),
        ({"success": True, "data": {"user": {"guest": True, "id": "visitor"}}}, "auth_expired"),
        ({"success": True, "data": {}}, "identity_unverified"),
        ({"success": True, "data": {"user_id": "wrong"}}, "identity_mismatch"),
        ({"code": 0, "msg": "安全验证"}, "verification_required"),
    ],
)
def test_xhs_authority_rejects_guests_mismatch_and_challenges(payload, error):
    with pytest.raises(CollectionError) as failure:
        identity_from_payload("xhs", payload, "expected")
    assert failure.value.kind == error


@pytest.mark.parametrize(
    "platform,payload",
    [
        ("xhs", {"data": {"user_id": "expected", "nickname": "nick"}}),
        ("rednote", {"data": {"user_id": "expected", "nickname": "nick"}}),
        ("douyin", {"status_code": 0, "user": {"uid": "expected", "sec_uid": "sec"}}),
    ],
)
def test_authoritative_expected_identity(platform, payload):
    assert identity_from_payload(platform, payload, "expected")["user_id"] == "expected"
    with pytest.raises(CollectionError, match="identity_mismatch"):
        identity_from_payload(platform, payload, "different")


@pytest.mark.parametrize("missing", ["UIFID", "xmst", "sessionid"])
async def test_douyin_export_requires_complete_security_material(missing):
    state = {
        "cookies": [cookie("douyin", name=n) for n in ("UIFID", "sessionid") if n != missing],
        "origins": [
            {
                "origin": "https://www.douyin.com",
                "localStorage": [] if missing == "xmst" else [{"name": "xmst", "value": "new"}],
            }
        ],
    }
    with pytest.raises(CollectionError):
        await verified_storage_state(
            SimpleNamespace(storage_state=AsyncMock(return_value=state)), "douyin"
        )


@pytest.fixture
def capture_context(monkeypatch):
    page = SimpleNamespace(
        goto=AsyncMock(),
        title=AsyncMock(return_value="home"),
        frames=[],
        url="https://www.xiaohongshu.com/",
        evaluate=AsyncMock(return_value="UA"),
    )
    context = SimpleNamespace(
        pages=[page],
        browser=SimpleNamespace(version="150"),
        close=AsyncMock(),
        clear_cookies=AsyncMock(),
        cookies=AsyncMock(return_value=[cookie()]),
        storage_state=AsyncMock(return_value={"cookies": [cookie()], "origins": []}),
    )
    launch = AsyncMock(return_value=context)

    @asynccontextmanager
    async def playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))

    monkeypatch.setattr(capture, "async_playwright", playwright)
    monkeypatch.setattr(capture, "verify_profile", AsyncMock(return_value={"user_id": "expected"}))
    monkeypatch.setattr(
        capture, "capture_browser_environment", AsyncMock(return_value={"test": True})
    )
    monkeypatch.setattr(capture, "write_environment_snapshot", lambda *_: None)
    return context, launch


async def test_recover_exports_only_after_two_verified_checks(tmp_path, capture_context):
    output = tmp_path / "cookies.json"
    output.write_text(json.dumps([cookie(value="old")]))
    context, launch = capture_context
    admit = AsyncMock()
    assert (
        await capture.capture_platform_cookies(
            capture.PLATFORMS["xhs"],
            output=output,
            profile=tmp_path / "profile",
            timeout=3,
            admit=admit,
            expected_user_id="expected",
        )
        == 1
    )
    context.clear_cookies.assert_not_awaited()
    assert json.loads(output.read_text())["cookies"][0]["value"] == "new"
    receipt = read_recovery_receipt(tmp_path / "profile")
    assert receipt["user_id"] == "expected"
    assert receipt["old_digest"] != receipt["new_digest"]
    assert capture.verify_profile.await_count == 2
    assert capture.verify_profile.call_args.kwargs["expected_user_id"] == "expected"
    context.close.assert_awaited_once()
    Session(EnvironmentConfig(cookie_file=str(output)), "xhs")


@pytest.mark.parametrize(
    "error",
    [
        "auth_expired",
        "identity_mismatch",
        "identity_unverified",
        "verification_required",
        "access_denied",
    ],
)
async def test_failed_recovery_does_not_overwrite(tmp_path, capture_context, monkeypatch, error):
    output = tmp_path / "cookies.json"
    output.write_text("old file")
    monkeypatch.setattr(capture, "verify_profile", AsyncMock(side_effect=CollectionError(error)))
    with pytest.raises(CollectionError) as failure:
        await capture.capture_platform_cookies(
            capture.PLATFORMS["xhs"],
            output=output,
            profile=tmp_path / "profile",
            timeout=3,
            headless=True,
        )
    assert failure.value.kind == error
    assert output.read_text() == "old file"
    capture_context[0].clear_cookies.assert_not_awaited()
    capture_context[0].close.assert_awaited_once()


async def test_second_identity_failure_keeps_old_file(tmp_path, capture_context, monkeypatch):
    output = tmp_path / "cookies.json"
    output.write_text("old file")
    monkeypatch.setattr(
        capture,
        "verify_profile",
        AsyncMock(
            side_effect=[
                {"user_id": "expected"},
                CollectionError("identity_mismatch"),
            ]
        ),
    )
    with pytest.raises(CollectionError):
        await capture.capture_platform_cookies(
            capture.PLATFORMS["xhs"], output=output, profile=tmp_path / "profile", timeout=3
        )
    assert output.read_text() == "old file"


async def test_recover_missing_file_and_pins_previous_identity(tmp_path, capture_context):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "session-recovery.json").write_text('{"user_id":"expected"}')
    await capture.capture_platform_cookies(
        capture.PLATFORMS["xhs"], output=tmp_path / "new.json", profile=profile, timeout=3
    )
    assert capture.verify_profile.call_args_list[0].kwargs["expected_user_id"] == "expected"
    assert (tmp_path / "new.json").exists()


def test_atomic_write_failure_preserves_old_file(tmp_path, monkeypatch):
    path = tmp_path / "cookies.json"
    path.write_text("old")
    monkeypatch.setattr(capture.os, "replace", lambda *_: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        capture.write_cookie_file(path, [cookie()])
    assert path.read_text() == "old"
    assert list(tmp_path.glob("*.tmp")) == []


def test_recovery_key_excludes_profile_cache(tmp_path):
    env = EnvironmentConfig(cookie_file=str(tmp_path / "cookies"), profile_dir=str(tmp_path))
    original = recovery_key(env)
    (tmp_path / "Cache").write_text("browser navigation changed this")
    assert recovery_key(env) == original
    (tmp_path / "cookies").write_text("updated")
    assert recovery_key(env) != original
    before_version = recovery_key(env)
    env.session_version = "2"
    assert recovery_key(env) != before_version


def test_resource_lock_cannot_be_reentered(tmp_path):
    with exclusive(["profile:recovery"], lock_dir=tmp_path):
        with pytest.raises(CollectionError, match="Another worker"):
            with exclusive(["profile:recovery"], lock_dir=tmp_path):
                pytest.fail("shared profile lock admitted two owners")


@pytest.mark.browser
async def test_real_chromium_targeted_reauth_preserves_device_and_other_domains():
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            await context.add_cookies(
                [
                    cookie(),
                    cookie(name="a1"),
                    cookie("douyin"),
                    cookie("douyin", name="UIFID"),
                    {
                        "name": "web_session",
                        "value": "foreign",
                        "domain": ".example.com",
                        "path": "/",
                    },
                ]
            )
            await clear_platform_auth(context, "xhs")
            values = {(c["domain"], c["name"]) for c in await context.cookies()}
            assert (".xiaohongshu.com", "web_session") not in values
            assert (".xiaohongshu.com", "a1") in values
            assert (".douyin.com", "sessionid") in values
            assert (".example.com", "web_session") in values
            await clear_platform_auth(context, "douyin")
            values = {(c["domain"], c["name"]) for c in await context.cookies()}
            assert (".douyin.com", "sessionid") not in values
            assert (".douyin.com", "UIFID") in values
        finally:
            await browser.close()


async def test_reauth_clears_only_targeted_auth_before_authority(
    tmp_path,
    capture_context,
    monkeypatch,
):
    monkeypatch.setattr(capture, "initial_login_cookie_values", AsyncMock(return_value={}))
    monkeypatch.setattr(capture, "wait_for_login_cookie", AsyncMock(return_value=[cookie()]))
    monkeypatch.setattr(capture.asyncio, "sleep", AsyncMock())
    context, launch = capture_context
    await capture.capture_platform_cookies(
        capture.PLATFORMS["xhs"],
        output=tmp_path / "new.json",
        profile=tmp_path / "profile",
        timeout=3,
        mode="reauth",
    )
    calls = context.clear_cookies.call_args_list
    assert {call.kwargs["name"] for call in calls} == {"web_session", "web_session_sec", "id_token"}
    for call in calls:
        assert call.kwargs["domain"].search(".xiaohongshu.com")
        assert not call.kwargs["domain"].search("evilxiaohongshu.com")
    assert launch.call_args.kwargs["headless"] is False
    assert capture.verify_profile.await_count == 2


async def test_strict_environment_failure_cannot_replace_file(tmp_path, capture_context):
    output = tmp_path / "old.json"
    output.write_text("old")
    session = SimpleNamespace(
        config=SimpleNamespace(consistency_policy="strict"), environment_snapshot=None
    )
    with pytest.raises(CollectionError) as failure:
        await capture.capture_platform_cookies(
            capture.PLATFORMS["xhs"],
            output=output,
            profile=tmp_path / "profile",
            timeout=3,
            environment_session=session,
        )
    assert failure.value.kind == "environment_snapshot_missing"
    assert output.read_text() == "old"


async def test_external_file_change_during_verification_is_preserved(
    tmp_path,
    capture_context,
    monkeypatch,
):
    output = tmp_path / "old.json"
    output.write_text("old")

    async def verify(*args, **kwargs):
        output.write_text("external edit")
        return {"user_id": "expected"}

    monkeypatch.setattr(capture, "verify_profile", verify)
    with pytest.raises(CollectionError) as failure:
        await capture.capture_platform_cookies(
            capture.PLATFORMS["xhs"], output=output, profile=tmp_path / "profile", timeout=3
        )
    assert failure.value.kind == "session_changed"
    assert output.read_text() == "external edit"


async def test_quota_failure_happens_before_browser_launch(tmp_path, capture_context):
    with pytest.raises(CollectionError, match="quota_exhausted"):
        await capture.capture_platform_cookies(
            capture.PLATFORMS["xhs"],
            output=tmp_path / "new.json",
            profile=tmp_path / "profile",
            timeout=3,
            admit=AsyncMock(side_effect=CollectionError("quota_exhausted")),
        )
    capture_context[1].assert_not_awaited()


@pytest.mark.parametrize(
    "status,payload,expected",
    [
        (200, {"data": {"user_id": "expected"}}, None),
        (200, {"data": {"guest": True, "user_id": "visitor"}}, "auth_expired"),
        (200, {"data": {"user_id": "wrong"}}, "identity_mismatch"),
        (403, {}, "access_denied"),
        (471, {}, "verification_required"),
    ],
)
async def test_profile_probe_uses_only_profile_cookies_and_self_endpoint(
    monkeypatch,
    status,
    payload,
    expected,
):
    from unittest.mock import Mock

    from social_crawler.adapters.xhs.http import XHSSigner
    from social_crawler.environments.session_recovery import verify_profile

    signer = Mock(return_value={"x-s": "synthetic"})
    monkeypatch.setattr(XHSSigner, "headers", signer)
    locator = SimpleNamespace(first=SimpleNamespace(is_visible=AsyncMock(return_value=False)))
    page = SimpleNamespace(
        title=AsyncMock(return_value="home"),
        frames=[],
        url="https://www.xiaohongshu.com/",
        locator=lambda _: locator,
        evaluate=AsyncMock(return_value="UA"),
    )
    response = SimpleNamespace(
        status=status, json=AsyncMock(return_value=payload), dispose=AsyncMock()
    )
    context = SimpleNamespace(
        cookies=AsyncMock(return_value=[cookie(value="profile-current")]),
        request=SimpleNamespace(get=AsyncMock(return_value=response)),
    )
    budget = AsyncMock()
    if expected:
        with pytest.raises(CollectionError) as failure:
            await verify_profile(context, page, "xhs", expected_user_id="expected", admit=budget)
        assert failure.value.kind == expected
    else:
        assert (
            await verify_profile(context, page, "xhs", expected_user_id="expected", admit=budget)
        )["user_id"] == "expected"
    assert signer.call_args.args[2] == "web_session=profile-current"
    assert context.request.get.call_args.args[0].endswith("/api/sns/web/v2/user/me")
    assert context.request.get.call_args.kwargs["max_redirects"] == 0
    response.dispose.assert_awaited_once()
    budget.assert_awaited_once_with("search")
