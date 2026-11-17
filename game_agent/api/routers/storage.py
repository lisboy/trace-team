"""数据存储位置：查看 / 更换数据根目录，并把已有数据迁移过去。

为什么需要：数据（SQLite 库 / 日志 / 上传 / 项目工作区）默认落在用户主目录的
`~/.trace`，时间久了会占满系统盘。用户希望能在设置里改到别的盘（如 D:），
并且「重新选一个地方后自动把数据搬过去」。

实现要点（安全第一）：
  * SQLite 正在被本进程写，**不能直接复制 .db 文件**（可能读到写了一半的页）。
    用 sqlite3 的在线 backup API 做一致性快照。
  * Linux 团队包的内置 MariaDB 数据目录就在 ``<DATA_DIR>/mysql``，同样不能在线复制。
    迁移流程：先复制其余文件（库继续服务）→ 停库 → 复制 mysql/ → 用包内 db.sh
    从新目录把库起起来。任何一步失败都把库从旧目录重新拉起，旧数据不动。
  * 迁移 = 先把整个数据目录复制到新位置，复制并校验成功后才写「指针文件」
    （`~/.trace_location`），并写一个待清理标记；旧目录**不在迁移时删**，
    等新实例用新目录正常启动、DB 打开成功后，再由启动逻辑删除旧目录。
    这样中途失败/新目录不可用，都还能退回旧目录，不会把数据弄丢。
  * 指针文件放在固定位置（用户主目录 / 打包版 exe 目录），不能放进数据目录自身——
    否则目录一搬走就记不住了。
  * 安装脚本会把当前数据目录写进 .env 的 GA_DATA_DIR。这种情况迁移要允许，
    成功后同步改写 .env（否则重启后脚本还在旧目录起库）；只有 shell/systemd
    注入的真环境变量才拒绝——那个改不了。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import stat
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from game_agent.api.deps import require
from game_agent.auth.rbac import PERM_AGENT_CONFIG
from game_agent import config as _cfg
from game_agent.config import (
    DATA_DIR,
    PENDING_CLEANUP_FILENAME,
    _DEFAULT_DATA_DIR,
    data_dir_env_override,
    data_dir_source,
    env_file_path,
    is_mysql,
    read_data_dir_pointer,
    rewrite_env_data_dir,
    write_data_dir_pointer,
)
from game_agent.core.context import RequestContext
from game_agent.core.instance_lock import INFO_FILENAME as _INSTANCE_INFO
from game_agent.core.instance_lock import LOCK_FILENAME as _INSTANCE_LOCK
from game_agent.core.instance_lock import PIDFILE_NAME as _PIDFILE
from game_agent.logging_setup import get_logger
from game_agent.platform_ui import picker_supported

# DB 文件名（DATABASE_URL 若被 .env 显式指定，DB_PATH 可能不存在，按标准名兜底）
_DB_NAME = getattr(_cfg, "DB_PATH", None).name if getattr(_cfg, "DB_PATH", None) else "game_agent.db"

log = get_logger("storage")
router = APIRouter(prefix="/api", tags=["storage"])

# 待清理标记：迁移成功后在**新**目录里写一份「旧目录路径」，启动时据此删除旧目录
PENDING_CLEANUP_FILE = PENDING_CLEANUP_FILENAME
_SQLITE_SUFFIXES = (".db", ".db-wal", ".db-shm")
# 运行时状态文件：不属于数据，而且正被本进程占用（Windows 上锁着，复制会 WinError 33）。
# 搬过去还会让新实例误判「已有实例在跑」，所以一律跳过。
_RUNTIME_FILES = frozenset({
    PENDING_CLEANUP_FILE,
    _INSTANCE_LOCK,
    _INSTANCE_INFO,
    _PIDFILE,
    ".trace_write_test",
})

# 迁移是「整目录复制」的分钟级动作，同一时刻只允许一次：
# 连点两下会启动两轮复制，后一轮把前一轮刚建出来的目录判成「非空」直接失败，
# 更糟的是两轮都写完指针后互相覆盖，用户看到的结果取决于谁最后收尾。
_MIGRATE_LOCK = asyncio.Lock()

# 迁移可能几分钟（十几 GB 的数据目录实测 3~6 分钟），没有任何反馈时用户会以为卡死，
# 甚至直接关窗口——所以把复制进度落在一个模块级单例里，前端轮询拿去做进度条。
# 单例够用：同一时刻只允许一次迁移（_MIGRATE_LOCK），不存在多份进度。
class MigrateProgress:
    """迁移进度。写方是复制用的线程，读方是事件循环里的 GET。

    只做「几个标量的读/写」，CPython 下单个属性赋值是原子的，不需要额外加锁；
    快照一次取多个字段，最坏只差一拍，进度条上看不出来。
    """

    def __init__(self) -> None:
        self.running = False
        self.phase = "idle"  # idle|scan|copy|db|done|error
        self.total_bytes = 0
        self.done_bytes = 0
        self.files_total = 0
        self.files_done = 0
        self.current = ""
        self.error = ""
        self.started_at = 0.0
        self.finished_at = 0.0
        self.source = ""
        self.target = ""

    def begin(self, src: Path, target: Path) -> None:
        import time

        self.__init__()
        self.running = True
        self.phase = "scan"
        self.source = str(src)
        self.target = str(target)
        self.started_at = time.time()

    def finish(self, *, error: str = "") -> None:
        import time

        self.running = False
        self.phase = "error" if error else "done"
        self.error = error
        self.current = ""
        self.finished_at = time.time()
        if not error:
            # 收尾时把计数对齐：正常结束就该是 100%，别让进度条停在 99.x%
            self.done_bytes = self.total_bytes
            self.files_done = self.files_total

    def snapshot(self) -> dict:
        import time

        elapsed = 0.0
        if self.started_at:
            end = self.finished_at or time.time()
            elapsed = max(0.0, end - self.started_at)
        pct = None
        if self.phase in ("copy", "copydb", "db"):
            # 扫描阶段总大小还没算完 → 不给百分比（前端显示「不确定」滚动条）
            if self.total_bytes > 0:
                pct = min(99.9, self.done_bytes * 100.0 / self.total_bytes)
        elif self.phase == "done":
            pct = 100.0
        speed = (self.done_bytes / elapsed) if elapsed > 0.5 and self.done_bytes else 0.0
        eta = None
        if speed > 0 and self.total_bytes > self.done_bytes and self.running:
            eta = int((self.total_bytes - self.done_bytes) / speed)
        return {
            "running": self.running,
            "phase": self.phase,
            "percent": pct,
            "done_bytes": self.done_bytes,
            "total_bytes": self.total_bytes,
            "done_human": _human(self.done_bytes),
            "total_human": _human(self.total_bytes),
            "files_done": self.files_done,
            "files_total": self.files_total,
            "current": self.current,
            "error": self.error,
            "elapsed_s": round(elapsed, 1),
            "speed_bps": int(speed),
            "speed_human": (_human(int(speed)) + "/s") if speed else "",
            "eta_s": eta,
            "source": self.source,
            "target": self.target,
        }


PROGRESS = MigrateProgress()


def _dir_size(p: Path) -> int:
    total = 0
    try:
        for f in p.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"


def _is_default(d: Path) -> bool:
    return str(d).lower() == str(Path(_DEFAULT_DATA_DIR).expanduser().resolve()).lower()


# ------------------------------------------------------------ 内置 MariaDB（Linux 包）
# <DATA_DIR>/mysql 是包内 MariaDB 的 datadir（里面还有一层 mysql 系统库）。
# 在线复制 InnoDB 文件必然不一致，所以迁移时必须先停库、复制、再从新目录起库。
def _local_db_dir() -> Path | None:
    if not is_mysql():
        return None
    d = DATA_DIR / "mysql"
    return d if (d / "mysql").is_dir() else None


def _db_port(data_dir: Path) -> int:
    try:
        return int((data_dir / "mysql_port").read_text(encoding="utf-8").strip() or 33306)
    except (OSError, ValueError):
        return 33306


def _port_open(port: int) -> bool:
    import socket

    s = socket.socket()
    s.settimeout(1.0)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _db_script() -> Path | None:
    """安装目录里的 scripts/db.sh（提供 db_start/db_stop）。源码运行没有 → None。"""
    roots: list[Path] = []
    pkg = (os.environ.get("GA_PKG_DIR") or "").strip()
    if pkg:
        roots.append(Path(pkg))
    try:
        # 打包版结构：<PKG>/game_agent/api/routers/storage.py
        roots.append(Path(__file__).resolve().parents[3])
    except IndexError:  # pragma: no cover - 目录层级异常
        pass
    for root in roots:
        script = root / "scripts" / "db.sh"
        # runtime/mariadb 是打包版独有目录，避免误用源码仓库里的同名脚本
        if script.is_file() and (root / "runtime" / "mariadb").is_dir():
            return script
    return None


def _run_db_sh(func: str, data_dir: Path, timeout: float) -> subprocess.CompletedProcess:
    """以指定数据目录执行包内 db.sh 的 db_start / db_stop（my.cnf 由它按新目录重写）。"""
    script = _db_script()
    if script is None:
        raise RuntimeError("找不到包内的 scripts/db.sh，无法安全搬迁数据库")
    pkg = script.parent.parent
    env = dict(os.environ)
    env["PKG_DIR"] = str(pkg)
    env["GA_PKG_DIR"] = str(pkg)
    env["GA_DATA_DIR"] = str(data_dir)
    env["GA_DB_SH"] = str(script)
    return subprocess.run(
        ["bash", "-c", '. "$GA_DB_SH" && ' + func],
        capture_output=True, text=True, timeout=timeout, env=env,
    )


def _stop_local_db(data_dir: Path) -> None:
    r = _run_db_sh("db_stop", data_dir, timeout=90)
    out = (r.stderr or r.stdout or "").strip()[-300:]
    if r.returncode != 0:
        raise RuntimeError(f"停止内置数据库失败：{out}")
    if _port_open(_db_port(data_dir)):
        raise RuntimeError(f"内置数据库没停下来（端口 {_db_port(data_dir)} 仍可连接），请稍后重试")


def _start_local_db(data_dir: Path) -> None:
    r = _run_db_sh("db_start", data_dir, timeout=180)
    out = (r.stderr or r.stdout or "").strip()[-300:]
    if r.returncode != 0 or not _port_open(_db_port(data_dir)):
        raise RuntimeError(f"内置数据库启动失败：{out or '端口未就绪'}")


def _require_env_file_writable() -> None:
    pf = env_file_path()
    if pf is None or not pf.is_file():
        raise HTTPException(status_code=400, detail="找不到 .env 文件，无法同步新的数据目录")
    if not os.access(pf, os.W_OK):
        raise HTTPException(
            status_code=400,
            detail=f"{pf} 不可写，无法同步新的数据目录；请用有权限的账号修改 GA_DATA_DIR 后重启再迁移",
        )


@router.get("/system/storage")
async def get_storage(ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """当前数据目录、默认目录、占用大小、所在盘剩余空间、以及等待生效的新目录（若有）。"""
    cur = DATA_DIR
    used = await asyncio.to_thread(_dir_size, cur)
    try:
        du = shutil.disk_usage(str(cur))
        free = du.free
        total = du.total
    except OSError:
        free = total = 0
    # 指针文件里记的目录若和当前进程实际用的不一致，说明「改过但还没重启」
    pointer = read_data_dir_pointer()
    pending = ""
    if pointer:
        pp = Path(pointer).expanduser().resolve()
        if str(pp).lower() != str(cur).lower():
            pending = str(pp)
    # 只有真环境变量才叫「钉死」；.env 是安装脚本的记录，迁移会同步改写。
    # 已迁移、等重启期间 .env 已指向新目录而本进程还在旧目录——别同时弹
    # 「不会生效」和「重启后生效」两条自相矛盾的提示。
    source = data_dir_source()
    pinned = data_dir_env_override() if (source == "env" and not pending) else ""
    return {
        "data_dir": str(cur),
        "default_dir": str(Path(_DEFAULT_DATA_DIR).expanduser()),
        "is_default": _is_default(cur),
        "used_bytes": used,
        "used_human": _human(used),
        "disk_free_bytes": free,
        "disk_total_bytes": total,
        "disk_free_human": _human(free),
        "pending_switch_to": pending,  # 非空=已改过目录，重启后生效
        # 非空=数据目录被环境变量 GA_DATA_DIR 钉死了，此时「迁移」不会生效，前端要说明白
        "env_override_dir": pinned,
        # 数据目录来源：env|dotenv|pointer|default（前端/日志排查用）
        "data_dir_source": source,
        # 带内置 MariaDB（Linux 包）：迁移期间数据库会停一下，前端要提前讲清楚
        "moves_local_db": _local_db_dir() is not None,
        # 「浏览…」能不能弹出系统原生文件夹窗口；false 时前端回退内置目录浏览器
        "picker_native": picker_supported(),
        "app_running": True,
    }


@router.get("/system/storage/migrate/progress")
async def migrate_progress(ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """迁移进度（前端每 300~500ms 轮询一次）。phase: idle|scan|copy|db|done|error。"""
    return PROGRESS.snapshot()


class PickDirIn(BaseModel):
    title: str | None = None


@router.post("/system/storage/pick-dir")
async def pick_storage_dir(body: PickDirIn, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """在**服务所在机器**上弹出系统「选择文件夹」窗口，返回用户选的绝对路径。

    这里必须是原生窗口：用户要的就是「电脑文件管理器那种」，而且要能直接进别的盘、
    网络位置、U 盘。窗口是模态阻塞的，所以丢到线程里跑，别把事件循环一起卡住。
    """
    from game_agent.platform_ui import PickerBusy, PickerUnavailable, pick_directory

    title = (body.title or "").strip() or "选择数据存放位置"
    try:
        path = await asyncio.to_thread(pick_directory, title)
    except PickerBusy as e:
        raise HTTPException(status_code=409, detail=str(e))
    except PickerUnavailable as e:
        # 501：这台机器弹不出原生窗口（无桌面/无 GUI），前端据此回退内置目录浏览器
        raise HTTPException(status_code=501, detail=f"无法打开系统文件夹窗口：{e}")
    except Exception as e:  # noqa: BLE001
        log.exception("pick-dir failed")
        raise HTTPException(status_code=500, detail=f"打开系统文件夹窗口失败：{e}")
    return {"ok": True, "path": path, "cancelled": not path}


class MigrateIn(BaseModel):
    target_dir: str


def _db_copy(src: Path, dst: Path) -> None:
    """SQLite 一致性快照：用在线 backup API，避免直接复制正在被写的库文件。"""
    src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    try:
        dst_conn = sqlite3.connect(str(dst))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _plan_copy(src: Path) -> list[tuple[Path, Path, int]]:
    """扫一遍源目录，列出要复制的 (源文件, 目标相对路径, 大小)，并累加总字节数。

    DB 文件（.db/-wal/-shm）和待清理标记不参与：DB 走 backup 快照，标记不该跟着搬。
    套接字/FIFO 之类非普通文件跳过（复制会直接报错，比如 MariaDB 的 mysqld.sock）。
    """
    src_res = src.resolve()
    plan: list[tuple[Path, Path, int]] = []
    for root, dirs, files in os.walk(src_res, followlinks=False):
        root_path = Path(root)
        # 符号链接目录 os.walk 默认不下钻（会漏数据），这里显式提示而不是静默丢
        for d in list(dirs):
            if (root_path / d).is_symlink():
                log.warning("storage migrate: 跳过符号链接目录 %s", root_path / d)
        for name in files:
            if name in _SQLITE_SUFFIXES or name in _RUNTIME_FILES:
                continue
            f = root_path / name
            try:
                st = f.stat()
            except OSError:
                continue  # 拷不到的（被占用/权限/断链）交给 copy 阶段报错，别在这里算坏总数
            if not stat.S_ISREG(st.st_mode):
                log.warning("storage migrate: 跳过非普通文件 %s", f)
                continue
            plan.append((f, f.relative_to(src_res), st.st_size))
    return plan


def _copy_plan(plan: list[tuple[Path, Path, int]], dst: Path) -> None:
    for f, rel, size in plan:
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        PROGRESS.current = str(rel)
        try:
            shutil.copy2(f, target)
        except FileNotFoundError:
            # 扫描到复制的几分钟里被删掉的临时文件（日志轮转、mysqld 退出时删 pid/socket）。
            # 不是数据丢失，跳过即可；真拷不动（权限/占用）仍然报错。
            log.warning("storage migrate: 源文件已消失，跳过 %s", rel)
            continue
        except OSError as e:
            raise RuntimeError(f"复制 {rel} 失败：{e}") from e
        PROGRESS.done_bytes += size
        PROGRESS.files_done += 1


def _mirror_dirs(src: Path, dst: Path) -> None:
    """把目录骨架在新位置建出来：空目录也要存在（projects / objects 这类启动时未必重建）。
    只建目录、不碰文件；符号链接目录不跟，也不伪装成真目录。"""
    src_res = src.resolve()
    for root, dirs, _files in os.walk(src_res, followlinks=False):
        root_path = Path(root)
        (dst / root_path.relative_to(src_res)).mkdir(parents=True, exist_ok=True)
        for d in list(dirs):
            if (root_path / d).is_symlink():
                dirs.remove(d)


def _do_migrate(src: Path, dst: Path) -> None:
    """把 src 整个目录复制到 dst（DB 走 backup，其余逐文件复制并上报进度）。

    用「先扫描后复制」两步：扫描拿到总字节数，复制阶段才能算出百分比；
    否则前端只能看到「已完成 N MB」而不知道还剩多少。
    """
    dst.mkdir(parents=True, exist_ok=True)
    src_res = src.resolve()
    _mirror_dirs(src_res, dst)

    PROGRESS.phase = "scan"
    plan = _plan_copy(src_res)
    db_src = src_res / _DB_NAME
    db_size = db_src.stat().st_size if db_src.is_file() else 0
    PROGRESS.total_bytes = sum(p[2] for p in plan) + db_size
    PROGRESS.files_total = len(plan) + (1 if db_size else 0)

    PROGRESS.phase = "copy"
    _copy_plan(plan, dst)

    # DB 快照（源文件存在才复制）
    if db_size:
        PROGRESS.phase = "db"
        PROGRESS.current = _DB_NAME
        _db_copy(db_src, dst / _DB_NAME)
        PROGRESS.done_bytes += db_size
        PROGRESS.files_done += 1
    PROGRESS.current = ""


def _migrate_all(src: Path, dst: Path, moving_db: bool) -> None:
    """执行整份搬迁；moving_db=True 时负责停库/复制 datadir/从新目录起库。

    复制顺序刻意把 mysql/ 放到最后：其余文件复制期间数据库还在服务（几分钟），
    停库时间只剩 datadir 本身的复制。失败时把库从旧目录重新拉起，旧数据一个字节不动。
    """
    dst.mkdir(parents=True, exist_ok=True)
    if not moving_db:
        _do_migrate(src, dst)
        return

    src_res = src.resolve()
    _mirror_dirs(src_res, dst)
    PROGRESS.phase = "scan"
    full = _plan_copy(src_res)
    app_plan = [p for p in full if p[1].parts[:1] != ("mysql",)]
    sqlite_db = src_res / _DB_NAME
    sqlite_size = sqlite_db.stat().st_size if sqlite_db.is_file() else 0
    PROGRESS.total_bytes = sum(p[2] for p in full) + sqlite_size
    PROGRESS.files_total = len(full) + (1 if sqlite_size else 0)

    stopped = False
    started = False
    try:
        PROGRESS.phase = "copy"
        _copy_plan(app_plan, dst)

        PROGRESS.phase = "stopdb"
        _stop_local_db(src_res)
        stopped = True

        # 停库后重新扫描：正常退出时会删掉 mysqld.pid / mysqld.sock，
        # 按停库前的清单复制会直接 FileNotFoundError。
        PROGRESS.phase = "copydb"
        db_plan = [p for p in _plan_copy(src_res) if p[1].parts[:1] == ("mysql",)]
        _copy_plan(db_plan, dst)

        if sqlite_size:  # 从 SQLite 升级来的旧库：顺手留一份一致性快照
            PROGRESS.phase = "db"
            PROGRESS.current = _DB_NAME
            _db_copy(sqlite_db, dst / _DB_NAME)
            PROGRESS.done_bytes += sqlite_size
            PROGRESS.files_done += 1

        PROGRESS.phase = "startdb"
        _start_local_db(dst)
        started = True
        PROGRESS.current = ""
    except Exception:
        PROGRESS.current = ""
        if stopped and not started:
            # 新库可能起了半截占着端口，先收掉再回旧目录起，否则两边抢 127.0.0.1:端口
            try:
                _stop_local_db(dst)
            except Exception:  # noqa: BLE001
                pass
            try:
                _start_local_db(src_res)
                log.warning("storage migrate failed: 已从旧目录重新启动内置数据库")
            except Exception:  # noqa: BLE001
                log.exception("storage migrate failed and old database could not be restarted")
        raise


@router.post("/system/storage/migrate")
async def migrate_storage(body: MigrateIn, ctx: RequestContext = Depends(require(PERM_AGENT_CONFIG))):
    """把当前数据目录整体迁移到 target_dir，成功后写指针（重启生效）并标记旧目录待清理。"""
    # 已经迁移过、还没重启：本进程仍在旧目录，再迁一次只会把旧目录再复制一遍。
    # 说清楚「先重启」，别让用户以为迁移没生效。
    pointer = read_data_dir_pointer()
    if pointer:
        pp = Path(pointer).expanduser().resolve()
        if str(pp).lower() != str(DATA_DIR.resolve()).lower():
            raise HTTPException(
                status_code=409,
                detail=f"数据目录已迁移到 {pp}，请先重启应用（stop.sh + run.sh）后再操作",
            )
    # 真·环境变量（shell/systemd）钉死时迁移注定不生效：直接说清楚，别让用户白等一次全量复制。
    # .env 里那份是安装脚本的记录，迁移会同步改写，不在此列。
    if data_dir_source() == "env":
        env_dir = data_dir_env_override()
        raise HTTPException(
            status_code=400,
            detail=(f"数据目录被环境变量 GA_DATA_DIR 固定为 {env_dir}，在设置页换位置不会生效。"
                    "请先删掉启动脚本/systemd 里的 GA_DATA_DIR，再来迁移。"),
        )
    if _MIGRATE_LOCK.locked():
        raise HTTPException(status_code=409, detail="已有一次迁移正在进行，请等它结束再操作")
    async with _MIGRATE_LOCK:
        return await _migrate_locked((body.target_dir or "").strip())


async def _migrate_locked(raw: str) -> dict:
    """真正的迁移流程（调用方已持有 _MIGRATE_LOCK）。"""
    if not raw:
        raise HTTPException(status_code=400, detail="请选择目标目录")
    target = Path(raw).expanduser()
    if not target.is_absolute():
        raise HTTPException(status_code=400, detail="目标目录必须是绝对路径")
    target = target.resolve()
    src = DATA_DIR.resolve()

    if str(target).lower() == str(src).lower():
        raise HTTPException(status_code=400, detail="目标目录就是当前数据目录，无需迁移")

    # 目标不能落在当前数据目录里面（否则会递归复制自己）
    try:
        target.relative_to(src)
        raise HTTPException(status_code=400, detail="目标目录不能位于当前数据目录内部")
    except ValueError:
        pass
    # 反过来：源在目标内部也不行
    try:
        src.relative_to(target)
        raise HTTPException(status_code=400, detail="当前数据目录位于目标目录内部，请换一个目录")
    except ValueError:
        pass

    # 目标必须不存在、或为空目录
    if target.exists():
        if not target.is_dir():
            raise HTTPException(status_code=400, detail="目标路径已存在且不是文件夹")
        try:
            if any(target.iterdir()):
                raise HTTPException(status_code=400, detail="目标文件夹非空，请选择一个空文件夹")
        except PermissionError:
            raise HTTPException(status_code=400, detail="目标文件夹无访问权限")
    # 可写性探测
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".trace_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"目标目录不可写：{e}")

    # 空间检查：目标盘剩余空间要大于源目录大小的 1.1 倍
    need = await asyncio.to_thread(_dir_size, src)
    try:
        free = shutil.disk_usage(str(target)).free
        if free < need * 1.1:
            raise HTTPException(
                status_code=400,
                detail=f"目标磁盘空间不足：需要约 {_human(int(need * 1.1))}，可用 {_human(free)}",
            )
    except OSError:
        pass

    # .env 里的 GA_DATA_DIR 是安装脚本的记录：迁移必须能同步改写它，否则重启后
    # 启动脚本仍在旧目录起库。先确认可写，别等全量复制完才失败。
    source = data_dir_source()
    if source == "dotenv":
        _require_env_file_writable()

    local_db = _local_db_dir()
    if local_db is not None and _db_script() is None:
        raise HTTPException(
            status_code=400,
            detail="检测到内置数据库，但找不到包内的 scripts/db.sh（请用 run.sh 启动服务），无法安全搬迁数据库",
        )

    log.info("storage migrate start: %s -> %s (%s)", src, target, _human(need))
    PROGRESS.begin(src, target)
    try:
        await asyncio.to_thread(_migrate_all, src, target, local_db is not None)
    except Exception as e:  # noqa: BLE001
        log.exception("storage migrate failed")
        PROGRESS.finish(error=str(e))
        # 清掉半成品，避免用户下次选到非空目录
        _cleanup_partial(target)
        raise HTTPException(status_code=500, detail=f"迁移失败（旧数据未动）：{e}")

    # 复制成功 → 先改 .env（下次启动脚本也用新目录），再写指针（重启后使用新目录）
    pointer_arg = "" if _is_default(target) else str(target)
    try:
        if source == "dotenv":
            rewrite_env_data_dir(pointer_arg)
        write_data_dir_pointer(pointer_arg)
    except Exception as e:  # noqa: BLE001
        log.exception("storage migrate: 写数据目录记录失败，开始回滚")
        PROGRESS.finish(error=str(e))
        await asyncio.to_thread(_rollback_after_commit_failure, src, target, local_db is not None)
        raise HTTPException(status_code=500, detail=f"迁移后写入数据目录记录失败，已回退：{e}")

    # 新目录里留下「旧目录待清理」标记，重启确认能打开后再删旧目录
    try:
        (target / PENDING_CLEANUP_FILE).write_text(str(src), encoding="utf-8")
    except Exception:  # noqa: BLE001 - 写不了标记只是不能自动清理旧目录，不影响切换
        log.warning("write pending-cleanup marker failed")

    log.info("storage migrate done: now=%s (restart to apply)", target)
    PROGRESS.finish()
    if local_db is not None:
        message = "数据已复制到新目录，内置数据库已从新位置启动；重启应用后路径生效，重启时会自动清理旧目录。"
    else:
        message = "数据已复制到新目录，重启后生效；重启时会自动清理旧目录。"
    return {
        "ok": True,
        "target_dir": str(target),
        "is_default": _is_default(target),
        "need_restart": True,
        "moved_local_db": local_db is not None,
        "env_file_updated": source == "dotenv",
        "message": message,
    }


def _cleanup_partial(target: Path) -> None:
    try:
        for child in target.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    except Exception:  # noqa: BLE001
        pass


def _rollback_after_commit_failure(src: Path, target: Path, moved_db: bool) -> None:
    """配置记录写失败时回滚：库切回旧目录（新库可能已在跑），再清掉新目录里的副本。"""
    if moved_db:
        try:
            _stop_local_db(target)
        except Exception:  # noqa: BLE001
            pass
        try:
            _start_local_db(src)
        except Exception:  # noqa: BLE001
            log.exception("回滚：旧目录数据库启动失败，请执行 stop.sh 后重新 run.sh")
    _cleanup_partial(target)
