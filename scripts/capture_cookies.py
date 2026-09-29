#!/usr/bin/env python3
import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import urlsplit

from social_crawler.config import load_config
from social_crawler.environments.cookie_capture import PLATFORMS, capture_platform_cookies
from social_crawler.environments.session import exclusive, load_proxy


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        description="打开可见浏览器，登录后将小红书/抖音 Cookie 安全写入 data/cookies。"
    )
    command.add_argument(
        "--platform", choices=["xhs", "douyin", "both"], default="both"
    )
    command.add_argument("--output-dir", type=Path, default=Path("data/cookies"))
    command.add_argument(
        "--profile-root", type=Path, default=Path("profiles/cookie-login")
    )
    command.add_argument("--timeout", type=float, default=600, help="每个平台等待登录的秒数")
    command.add_argument(
        "--channel", help="可选浏览器通道，例如 chrome；默认使用 Playwright Chromium"
    )
    command.add_argument("--xhs-proxy-env", help="保存小红书代理 URL 的环境变量名")
    command.add_argument("--douyin-proxy-env", help="保存抖音代理 URL 的环境变量名")
    command.add_argument(
        "--proxy-file",
        type=Path,
        default=Path("data/proxy.url"),
        help="两个平台共用的代理文件；默认 data/proxy.url",
    )
    return command


async def run(args: argparse.Namespace) -> int:
    names = ["xhs", "douyin"] if args.platform == "both" else [args.platform]
    failures = []
    for name in names:
        proxy_env = getattr(args, f"{name}_proxy_env")
        try:
            proxy_file = None if proxy_env else str(args.proxy_file)
            proxy = urlsplit(load_proxy(proxy_env=proxy_env, proxy_file=proxy_file))
            keys = [f"profile:{(args.profile_root / name).resolve()}"]
            if proxy.hostname:
                keys.append(f"proxy:{proxy.hostname}:{proxy.port}")
            with exclusive(keys):
                await capture_platform_cookies(
                    PLATFORMS[name],
                    output=args.output_dir / f"{name}.json",
                    profile=args.profile_root / name,
                    timeout=args.timeout,
                    mode="reauth",
                    channel=args.channel,
                    proxy_env=proxy_env,
                    proxy_file=proxy_file,
                )
        except Exception as exc:
            failures.append(name)
            print(f"[{PLATFORMS[name].display_name}] 获取失败：{exc}", file=sys.stderr)
    if failures:
        print("未完成的平台：" + ", ".join(failures), file=sys.stderr)
        return 2
    print("Cookie 获取完成。采集器仍会在在线探针中验证登录态是否有效。")
    return 0


def main(argv=None) -> int:
    load_config()
    args = parser().parse_args(argv)
    if args.timeout <= 0:
        parser().error("--timeout 必须大于 0")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("已取消 Cookie 获取。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
