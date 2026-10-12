#!/usr/bin/env python3
"""Outlook Fluent API 注册 CLI。"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

from outlook_auto_register.config.constants import OUTLOOK_EMAIL_DOMAINS
from outlook_auto_register.service.registration.register_service import register_one, save_account


def main() -> int:
    parser = argparse.ArgumentParser(description="Outlook 新版 Fluent API 协议注册")
    parser.add_argument("--prefix", help="邮箱前缀（默认随机）")
    parser.add_argument("--domain", default="@outlook.com", help="邮箱后缀")
    parser.add_argument("--country", default="US", help="国家代码（建议与代理地区一致，默认 US）")
    parser.add_argument("--proxy", help="HTTP 代理，支持 http://user:pass@host:port 或 host:port:user:pass")
    parser.add_argument(
        "--px-mode",
        choices=["solver", "local", "offcaptcha"],
        default="solver",
        help="PX 解法：solver=captcha.run/EzCaptcha/CapSolver；"
        "local=本地 SwiftShader；offcaptcha=offcaptcha.com PX invisible+press",
    )
    parser.add_argument("--skip-login", action="store_true", help="仅注册，不完成后续 OAuth 登录（则无 refresh_token）")
    parser.add_argument(
        "--no-mail-token",
        action="store_true",
        help="注册后不换取 refresh_token（默认会取，用于生成四段格式）",
    )
    parser.add_argument("--output", default="accounts", help="账号保存目录")
    parser.add_argument("--count", type=int, default=1, help="批量注册数量（>1 走并发批量）")
    parser.add_argument("--concurrency", type=int, default=2, help="批量并发度（默认 2，保守值防同 IP/爆发式批量信号；1 最稳）")
    parser.add_argument("--jitter-min", type=float, default=None, help="相邻账号注册启动最小间隔秒（默认 3，防爆发式注册；0 关闭）")
    parser.add_argument("--jitter-max", type=float, default=None, help="相邻账号注册启动最大间隔秒（默认 8）")
    parser.add_argument("-v", "--verbose", action="store_true", help="详细日志")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.domain not in OUTLOOK_EMAIL_DOMAINS:
        print(f"警告: 后缀 {args.domain} 不在推荐列表，仍尝试注册")

    if args.px_mode == "local":
        import os

        os.environ["PX_SOLVER"] = "swiftshader"
    elif args.px_mode == "offcaptcha":
        import os

        os.environ["PX_SOLVER"] = "offcaptcha"

    print("=== Outlook Fluent API 注册 ===")
    print(f"PX 模式: {args.px_mode}")
    if args.proxy:
        print(f"代理: {args.proxy}")

    if args.count > 1:
        from outlook_auto_register.service.registration.batch_service import register_batch_iter

        print(f"批量模式: count={args.count} concurrency={args.concurrency}")
        cli_batch = datetime.now().strftime("CLI-%m%d-%H%M")
        for ev in register_batch_iter(
            args.count,
            concurrency=args.concurrency,
            email_prefix=args.prefix,
            email_domain=args.domain,
            country=args.country,
            proxy=args.proxy,
            px_mode=args.px_mode,
            skip_post_login=args.skip_login,
            fetch_mail_token=not args.no_mail_token,
            output_dir=args.output,
            batch_id=cli_batch,
            batch_label=cli_batch,
            jitter_min=args.jitter_min,
            jitter_max=args.jitter_max,
        ):
            if ev["type"] == "result":
                tag = "OK" if ev["success"] else "FAIL"
                print(
                    f"  [{tag}] #{ev['index']} {ev['email']} "
                    f"refresh={ev['refresh_token_present']} login={ev['login_token_present']} "
                    f"{ev['elapsed']}s {ev.get('error', '')[:60]}"
                )
                if ev.get("combo"):
                    print(f"       {ev['combo_dual'] or ev['combo']}")
            elif ev["type"] == "done":
                print(
                    f"\n批量完成: {ev['ok']}/{ev['total']} 成功, 总耗时 {ev['elapsed']}s, "
                    f"单号均耗时 {ev['avg_per_account']}s"
                )
                print(f"各阶段平均耗时(s): {ev['avg_stage_timings']}")
        return 0

    result = register_one(
        email_prefix=args.prefix,
        email_domain=args.domain,
        country=args.country,
        proxy=args.proxy,
        px_mode=args.px_mode,
        skip_post_login=args.skip_login,
        fetch_mail_token=not args.no_mail_token,
    )

    if result.success:
        cli_batch = datetime.now().strftime("CLI-%m%d-%H%M")
        path = save_account(result, args.output, batch_id=cli_batch, batch_label=cli_batch)
        print("\n注册成功!")
        print(f"  邮箱: {result.email}")
        print(f"  密码: {result.password}")
        if result.refresh_token:
            print(f"  refresh_token: {result.refresh_token[:40]}...（完整见文件）")
        else:
            print("  refresh_token: (未获取；如加了 --skip-login 则不会取)")
        print(f"  保存: {path}")
        print("\n四段格式（email----password----client_id----refresh_token）:")
        print(result.to_combo())
        if result.login_refresh_token:
            print("\n六段格式（+ 登录授权令牌 login_client_id----login_refresh）:")
            print(result.to_combo(dual=True))
        return 0

    print(f"\n注册失败: {result.error}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
