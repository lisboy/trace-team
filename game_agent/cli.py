"""命令行工具（打包版与源码通用）。

团队版有账号密码：管理员忘记密码时，可在服务器本机用本工具重置（无需旧密码）：
    Trace.exe --reset-password            # 重置 admin
    Trace.exe --reset-password alice      # 重置指定账号
重置后密码回到 666666，并在下次登录时强制修改。

``--info`` 不开服务也能查数据目录 / 数据库 / 端口。
"""
from __future__ import annotations

import argparse
import sys


def _safe_print(text: str = "") -> None:
    """Windows 控制台编码各异，打印失败也绝不能让命令崩掉。"""
    try:
        print(text)
    except Exception:  # noqa: BLE001
        try:
            print(text.encode("ascii", "replace").decode("ascii"))
        except Exception:  # noqa: BLE001
            pass


def _print_info() -> int:
    from game_agent import config as cfg

    db_file = getattr(cfg, "DB_PATH", None)
    _safe_print("循迹（Trace）配置")
    _safe_print(f"  数据目录：{cfg.DATA_DIR}")
    if db_file:
        _safe_print(f"  数据库  ：SQLite {db_file}")
    else:
        _safe_print("  数据库  ：MySQL（连接串见 .env 的 DATABASE_URL）")
    _safe_print(f"  项目目录：{cfg.PROJECTS_ROOT}")
    _safe_print(f"  监听    ：{cfg.HOST}:{cfg.PORT}")
    _safe_print("  管理员  ：首次初始化为 admin（初始密码 666666，首登必须修改）")
    return 0


def _reset_password(username: str) -> int:
    import asyncio
    from datetime import datetime, timezone

    if sys.platform == "win32":
        # 与 main.py 保持一致：aiomysql 在 Proactor 事件循环下不兼容
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    from game_agent.auth.passwords import DEFAULT_PASSWORD, hash_password

    async def run() -> tuple[bool, list[str]]:
        from sqlalchemy import select, update

        from game_agent.db.migrate import init_db
        from game_agent.db.models import AuthSession, User
        from game_agent.db.session import SessionLocal

        await init_db()
        async with SessionLocal() as s:
            user = await s.scalar(select(User).where(User.username == username))
            if user is None:
                names = (await s.execute(select(User.username))).scalars().all()
                return False, list(names)
            user.password_hash = hash_password(DEFAULT_PASSWORD)
            user.must_change_password = True
            user.failed_logins = 0
            user.locked_until = None
            await s.execute(update(AuthSession).where(
                AuthSession.user_id == user.id,
                AuthSession.revoked_at.is_(None),
            ).values(revoked_at=datetime.now(timezone.utc)))
            await s.commit()
            return True, []

    ok, names = asyncio.run(run())
    if ok:
        _safe_print(f"已将账号 {username} 的密码重置为 {DEFAULT_PASSWORD}，下次登录必须修改。")
        return 0
    _safe_print(f"没有找到账号 {username}。"
                f"现有账号：{', '.join(names) if names else '（无，请先启动一次服务完成初始化）'}")
    return 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="trace",
        description="Trace 命令行工具：重置账号密码 / 查看配置。",
    )
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("info", help="打印数据目录 / 数据库 / 端口等配置")
    rp = sub.add_parser("reset-password", help="把账号密码重置为 666666（下次登录强制修改）")
    rp.add_argument("username", nargs="?", default="admin", help="账号名，默认 admin")
    ap.add_argument("--info", action="store_true", help="等价于 info 子命令")
    ap.add_argument("--reset-password", "-rp", nargs="?", const="admin", default=None,
                    metavar="USERNAME", help="重置账号密码，默认 admin")
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.info or args.cmd == "info":
        return _print_info()
    if args.cmd == "reset-password":
        return _reset_password(args.username or "admin")
    if args.reset_password:
        return _reset_password(args.reset_password)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
