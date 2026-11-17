"""全局配置：从环境变量读取，提供 lite / server 双模式所需的所有开关与路径。

设计约束：业务代码只依赖 5 个抽象接口，本模块只决定「用哪套实现」与「路径/密钥」。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# .env 里写的 GA_DATA_DIR 只是安装脚本记下的「当前数据目录」，不是用户手工钉死：
# 设置页迁移时要允许并同步改写这一行（见 data_dir_source / rewrite_env_data_dir）。
# 真正从 shell / systemd 注入的环境变量才拒绝迁移——那个改不了 .env 也没用。
_ENV_FILE: Path | None = None
_PRELOAD_DATA_DIR = os.environ.get("GA_DATA_DIR", "")

try:
    from dotenv import find_dotenv, load_dotenv

    if getattr(sys, "frozen", False):
        # 冻结成 exe 后没有 .py 文件，load_dotenv() 默认从当前工作目录找 .env；
        # 明确指向 exe 同目录的 .env，双击启动（工作目录未必是 exe 目录）也能读到配置。
        _env_path: Path | None = Path(sys.executable).parent / ".env"
    else:
        _found = find_dotenv(usecwd=True)
        _env_path = Path(_found) if _found else None
    if _env_path is not None and _env_path.is_file():
        load_dotenv(_env_path)
        _ENV_FILE = _env_path.resolve()
    else:
        load_dotenv()
except Exception:  # pragma: no cover - 没有 python-dotenv 也能跑（环境变量已注入）
    pass

ENV = os.environ


def _str(key: str, default: str = "") -> str:
    v = ENV.get(key)
    return v if v not in (None, "") else default


def _bool(key: str, default: bool = False) -> bool:
    v = ENV.get(key)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(key: str, default: int) -> int:
    v = ENV.get(key)
    if v in (None, ""):
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    v = ENV.get(key)
    if v in (None, ""):
        return default
    try:
        return float(v)
    except ValueError:
        return default


# ---------- 模式 ----------
MODE: str = _str("GA_MODE", "lite").lower()
# 注意：server 只保留了分支判断（如 can_reveal / list_dirs 的目录限制），
# 存储与队列**没有** server 实现——本版本只有 SQLite + core/lite 进程内实现，
# 设 GA_MODE=server 不会变成分布式部署。
assert MODE in ("lite", "server"), f"GA_MODE 非法: {MODE}"

# ---------- 数据目录 / 路径 ----------
# 数据根目录（DB / 日志 / 上传 / 项目工作区都在它下面）。团队版默认用 ~/.trace-team，
# 与个人版的 ~/.trace（指针 ~/.trace_location）完全分开：两者装在同一台机器上时
# 各自使用各自的库，互不污染。允许用户在「设置 / 存储空间」里改到别的盘。
#
# 记忆方式用「指针文件」——因为用户选的目录本身不能用来记住自己（搬走/删掉就丢了），
# 所以需要一个固定位置的小文件存「当前数据目录在哪」，进程启动时先读它：
#   ① 用户主目录的 ~/.trace_team_location（默认/最早的位置，永远可写）；
#   ② 打包版另外再看 exe 同目录的 .trace_team_location（便携盘换机也能跟着走）。
# 环境变量 GA_DATA_DIR 优先级最高（调试/测试用），显式给了就不再读指针文件。
_POINTER_FILENAME = ".trace_team_location"


def _pointer_files() -> list[Path]:
    """指针文件的候选位置（按优先级）。第一个是主位置，写指针时也写它。"""
    out: list[Path] = [Path.home() / _POINTER_FILENAME]
    if getattr(sys, "frozen", False):
        out.append(Path(sys.executable).parent / _POINTER_FILENAME)
    return out


def _read_pointer() -> str:
    """读指针文件里记的数据目录（空串=没有配置，走默认）。"""
    for pf in _pointer_files():
        try:
            if pf.is_file():
                raw = pf.read_text(encoding="utf-8").strip()
                if not raw:
                    continue
                # 新格式是 JSON {"data_dir": "..."}；兼容历史上的纯文本路径
                if raw.startswith("{"):
                    try:
                        return str(json.loads(raw).get("data_dir") or "")
                    except Exception:  # noqa: BLE001
                        continue
                return raw
        except Exception:  # noqa: BLE001 - 读不到就继续看下一个
            pass
    return ""


def read_data_dir_pointer() -> str:
    """对外暴露：当前指针文件里记的数据目录（没有则空串）。设置页用来判断是否被改过。"""
    return _read_pointer()


def write_data_dir_pointer(new_dir: str) -> None:
    """把「数据目录改到 new_dir」记进指针文件。new_dir 为空 = 恢复默认（删掉指针）。"""
    for pf in _pointer_files():
        try:
            pf.parent.mkdir(parents=True, exist_ok=True)
            if new_dir:
                pf.write_text(json.dumps({"data_dir": new_dir}, ensure_ascii=False), encoding="utf-8")
            else:
                pf.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001 - 写第二个（exe 目录）失败不影响主位置
            pass


def env_file_path() -> Path | None:
    """本次启动实际读到的 .env 路径（没读到则为 None）。"""
    return _ENV_FILE


def _env_file_value(key: str) -> str:
    """直接读 .env 文件里某个键的值（同键多行时以最后一行生效为准）。"""
    if _ENV_FILE is None:
        return ""
    try:
        lines = _ENV_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    val = ""
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        if k.strip() == key:
            val = v.strip().strip('"').strip("'")
    return val


def data_dir_source() -> str:
    """GA_DATA_DIR 的来源：env（真环境变量）| dotenv（.env 文件）| pointer | default。

    设置页据此决定「迁移能不能做」：env 改不动（重启后又被注入）→ 拒绝；
    dotenv 是安装脚本写的记录 → 允许迁移，迁移成功后同步改写 .env。
    """
    cur = _str("GA_DATA_DIR", "")
    if not cur:
        return "pointer" if _read_pointer() else "default"
    if not _PRELOAD_DATA_DIR and _env_file_value("GA_DATA_DIR") == cur:
        return "dotenv"
    return "env"


def rewrite_env_data_dir(new_dir: str) -> None:
    """把 .env 里的 GA_DATA_DIR 改成 new_dir（空串=留空，走指针/默认），其余行原样保留。

    迁移成功后才调用；写失败会抛异常，调用方据此回滚（否则下次启动脚本还在老目录起库）。
    """
    pf = _ENV_FILE
    if pf is None or not pf.is_file():
        raise RuntimeError("找不到 .env 文件，无法同步数据目录")
    out: list[str] = []
    done = False
    for line in pf.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        key = s.split("=", 1)[0].strip() if "=" in s else ""
        if not done and s and not s.startswith("#") and key == "GA_DATA_DIR":
            out.append(f"GA_DATA_DIR={new_dir}")
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f"GA_DATA_DIR={new_dir}")
    tmp = pf.with_name(pf.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        tmp.chmod(pf.stat().st_mode & 0o777)
    except OSError:
        pass
    tmp.replace(pf)


_DEFAULT_DATA_DIR = str(Path.home() / ".trace-team")
# 优先级：环境变量 > 指针文件 > 默认 ~/.trace-team
_DATA_DIR_RAW = _str("GA_DATA_DIR", "") or _read_pointer() or _DEFAULT_DATA_DIR
DATA_DIR = Path(_DATA_DIR_RAW).expanduser().resolve()
PROJECTS_ROOT = Path(
    _str("PROJECTS_ROOT", str(DATA_DIR / "projects"))
).expanduser().resolve()
# 聊天附图（用户贴的原图）单独存这里，不落在项目工作区里：
#   ① 工作区常被快照回滚 / 切换分支 / 换机器 / 清缓存整个重来，埋在里面的附图会跟着消失；
#   ② 用户在自己仓库里 git clean/checkout 也会把 .trace/uploads 当未跟踪文件删掉。
# 放到 DATA_DIR 下（与 db/日志同级、随安装目录一起走），附图就能和工作区生命周期解耦。
# 旧数据仍在各工作区的 .trace/uploads/（读取时按项目回退、启动时迁移到新目录）。
CHAT_UPLOADS_ROOT = Path(
    _str("GA_CHAT_UPLOADS_ROOT", str(DATA_DIR / "chat_uploads"))
).expanduser().resolve()
LOG_DIR = DATA_DIR / "logs"
TMP_DIR = DATA_DIR / "tmp"

# 迁移后的「旧目录待清理」标记（写在**新**目录里，内容是旧目录绝对路径）。
# 只有启动时 DB 打开成功后才按它删旧目录——所以迁移本身绝不删数据。
# 名字单点定义在这里：迁移接口（api/routers/storage.py）写它、启动逻辑（main.py）读它，
# 两边各写一份字面量，改一处就静默失效（旧目录永远删不掉）。
PENDING_CLEANUP_FILENAME = ".trace_pending_cleanup"


def data_dir_env_override() -> str:
    """显式钉死数据目录的环境变量值（空串=没指定）。

    非空时 ``GA_DATA_DIR`` 优先级高于指针文件。注意它同样包含 .env 里的值——
    「能不能迁移」不看这里，看 ``data_dir_source()``：.env 来源可以迁移并改写，
    真正的环境变量（shell/systemd）才拒绝，否则用户会得到「迁移成功」却什么都没变。
    """
    return _str("GA_DATA_DIR", "")

# ---------- 密钥 ----------
SECRET_KEY = _str("GA_SECRET_KEY", "")
if not SECRET_KEY and MODE == "lite":
    # lite 离线可接受一个确定性开发密钥；server 模式必须由用户设置
    SECRET_KEY = "dev-insecure-secret-change-me-in-server"

# ---------- 数据库 ----------
# 团队版默认 MySQL（Linux 包内置 MariaDB），SQLite 仅留给本机调试与冒烟测试。
# 留空 → 用 DATA_DIR 下的 game_agent.db（测试/单机试用）；写 mysql:// 或 mariadb://
# 会自动换成 aiomysql 驱动。
DATABASE_URL = _str("DATABASE_URL", "")
REDIS_URL = _str("REDIS_URL", "")

if not DATABASE_URL:
    DB_PATH = DATA_DIR / "game_agent.db"
    DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"
elif DATABASE_URL.startswith(("mysql://", "mariadb://")):
    DATABASE_URL = "mysql+aiomysql://" + DATABASE_URL.split("://", 1)[1]
elif not DATABASE_URL.startswith(("sqlite", "mysql+aiomysql", "mysql+asyncmy")):
    raise RuntimeError(
        "DATABASE_URL 只支持 sqlite+aiosqlite:// 或 mysql+aiomysql:// 连接串；"
        f"当前值：{DATABASE_URL}"
    )

# ---------- LLM ----------
# 只有「真实大模型」一种运行方式：不存在 mock/离线演示模式。
# 团队版**没有 .env 全局兜底模型**：模型/地址/Key 只能来自管理员绑定的「模型档位」
# （项目绑定 → 团队默认档位 → 角色绑定）。没绑定时 run 直接失败并提示去绑定，
# 绝不悄悄用环境变量里的旧 Key/旧端点（那会让「界面显示已接通、实际 403」永远查不出来）。
# LLM_MODEL 只是档位缺模型名时的显示/兜底标签，不参与任何端点选择。
LLM_MODEL = _str("GA_LLM_MODEL", "gpt-4o-mini")
# 调用失败重试（429/5xx/超时/网络错误）：指数退避，遵循 Retry-After
# 尝试次数固定为 5（含首次），不支持用环境变量调整：GA_LLM_RETRIES 曾是死配置，已删除。
LLM_MAX_ATTEMPTS = 5
LLM_CALL_TIMEOUT_S = _int("GA_LLM_CALL_TIMEOUT_S", 360)  # 单次 LLM 调用硬上限（防网关挂起卡死 run）
LLM_RETRY_BASE_S = float(_str("GA_LLM_RETRY_BASE_S", "1.5") or 1.5)
# 建连超时（只等 TCP+TLS 握手，不覆盖模型出字时间）：默认 8s。
# 以前跟着 timeout=300 一起用 300s，是个大坑：中转站挂在 Cloudflare 后面、有两个 A 记录，
# 其中一个时不时黑洞（实测 3 次里 1 次 SYN 无响应），300s 就直接白等满五分钟
# （2026-11-13 那次 309.7s 的调用 = 300s 建连超时 + 正常耗时）。跨洋握手 0.6~0.9s 就够，
# 8s 给足余量，失败立刻退避重试（下一轮 DNS/选路多半换了健康的那台）。
LLM_CONNECT_TIMEOUT_S = float(_str("GA_LLM_CONNECT_TIMEOUT_S", "8") or 8)
# 进程级 LLM 出站并发上限：主agent + 子agent 共用同一把 key，一起打满中转配额只会换来
# 429 + 指数退避（实测 412 次 429 全发生在长 run 里，并把 4.7% 的调用拖成 >30s）。
# 闸门是自适应的：收到 429 立刻腰斩并发，连续成功后再慢慢加回来。0 = 不限（仅排查用）。
LLM_MAX_CONCURRENCY = _int("GA_LLM_MAX_CONCURRENCY", 12)
# 是否显式声明「允许一次返回多个工具调用」（OpenAI 兼容协议里的 parallel_tool_calls）。
# 有些中转/推理后端默认关闭，模型明明能一次发 3 个 grep 也硬拆成 3 轮，每轮都要付一次
# 完整往返（实测历史平均只有 1.5 个工具/轮）。端点不认这个字段时适配层会自动去掉重发。
LLM_PARALLEL_TOOL_CALLS = _bool("GA_LLM_PARALLEL_TOOL_CALLS", True)
# 出站并发的实测依据（2026-11-14，gonode.si / deepseek-v4.1-flash，41k prompt 固定，重复请求
# 吃满前缀缓存）：1 路 3.5~4.2s，2 路 3.4~4.2s，4 路 4.9s，8 路 4.5~5.0s，12 路 4.7~5.3s，
# 16 路 4.2~4.4s —— 单请求延迟几乎不随并发上升（因为 95% 的 prompt 命中缓存、瓶颈在等首 token），
# 而聚合吞吐 18→122→157→296→320→514 tok/s。所以上限调高到 12：多开就是纯赚，
# 429 时由自适应闸门腰斩收回。
# 子agent 的单次回复上限：**默认 0 = 不设**（重要）。实测近 7 天 384 次 sub 调用里，
# completion >4096 的那 18.2% 占掉 33% 的 sub 时间，但它们 text 中位 0 字符、只有 2 个
# 工具调用（arguments 中位 147 字符）——也就是说这些 token 全是**隐藏思考**，不是长报告。
# 硬截断会把这些工具调用的 JSON 切碎（参数解析失败 → 整轮白扔），所以这里保持不设，
# 从源头治：sub 角色的推理强度默认关掉（见 LLM_SUB_REASONING_EFFORT）。
LLM_SUB_MAX_OUTPUT_TOKENS = _int("GA_SUB_MAX_TOKENS", 0)
# 子agent 的推理强度默认值（档位没显式配 reasoning_effort 时生效）：none。
# 依据：sub 只做只读检索与核对（read_file/grep/glob），结论由主agent 复核；同一条
# sub 风格的请求实测——不传该字段（走端点默认）5.35s / 537 completion tokens，
# 显式 none 3.62s / 209 tokens（-32% 延迟、-61% 输出 token），且思考 token 归零。
# 想要更保守可设 low；设成空串 = 不干预（走端点默认）。主agent 不受此影响。
LLM_SUB_REASONING_EFFORT = _str("GA_SUB_REASONING_EFFORT", "none")
# 同 run 内「同工具 + 同参数」重复调用直接复用上次结果（只读类工具）。
# 实测近 40 个 run 的 2093 轮里有 311 轮含完全重复的调用（15%），重复 grep/read 大仓库
# 一次就是几百毫秒到几秒；更麻烦的是重复结果会再塞一遍上下文，把后续每轮都撑大。
# 任何写/执行类工具跑过之后立即清空记忆，避免「改完还读旧内容」。
TOOL_MEMO_ENABLED = _bool("GA_TOOL_MEMO", True)
TOOL_MEMO_MAX_ENTRIES = _int("GA_TOOL_MEMO_MAX", 200)
# 历史压缩用的「摘要调用」单独给一套更短的上限：它不是正文产出，卡住时用户看到的是
# 「压缩/汇报时一直不动」。默认 120s / 2 次（最坏 ~2×120s + 退避），失败就退化为要点节选，
# 绝不让摘要器按正文的 5×360s 预算把整个 run 拖死。
LLM_SUMMARY_TIMEOUT_S = _int("GA_LLM_SUMMARY_TIMEOUT_S", 120)
LLM_SUMMARY_MAX_ATTEMPTS = max(1, min(_int("GA_LLM_SUMMARY_MAX_ATTEMPTS", 2), LLM_MAX_ATTEMPTS))
# 整次压缩（可能分好几块串行摘要）的总墙钟预算：超了就停下，用已有摘要 + 要点节选兜底。
# 否则 4 块 × 每块多次重试会把「压缩」这一步本身拖成好几分钟的假死。
LLM_SUMMARY_CYCLE_BUDGET_S = _int("GA_LLM_SUMMARY_CYCLE_BUDGET_S", 240)
# 中转站挂在 Cloudflare 后面时，httpx 默认 UA（python-httpx/x.y）会被 WAF 判成机器人，
# /models 与 /chat/completions 一律回 HTTP 403 + 「Just a moment...」HTML ——
# 表现成「Key 无效/额度不足」，其实是端点根本没把请求转给上游。带浏览器 UA 即 200。
# 置空 = 用 httpx 默认 UA（自建端点如 Ollama 无此问题，但默认带也无害）。
LLM_USER_AGENT = _str("GA_LLM_USER_AGENT",
                      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
# 上下文现场（llm_transcripts）保留天数：每轮存一份完整请求，长 run 一轮就上百 KB，
# 不清库迟早把库里堆满。0 = 不保留（等于关掉现场记录，只剩计数统计）。
LLM_TRANSCRIPT_KEEP_DAYS = _int("GA_LLM_TRANSCRIPT_KEEP_DAYS", 14)
# 旧工具结果降级（节省上下文，不影响「可再生信息」的可用性）：
# 文件内容/命令输出这类**可由路径或命令再生**的 tool 结果，模型看过一轮之后就从历史里
# 降级成「前 N 字符 + 省略提示」的存根——需要细节时它会自己再 read_file/run_command，
# 但不必让一份 8k 的旧文件内容在后几十轮里每轮重发。置 0 = 关闭该降级。
# SHRINK_CHARS = 触发降级的最小结果长度；SHRINK_KEEP = 降级后保留的前缀字符数。
TOOL_RESULT_SHRINK_CHARS = _int("GA_TOOL_RESULT_SHRINK_CHARS", 1500)
TOOL_RESULT_SHRINK_KEEP = _int("GA_TOOL_RESULT_SHRINK_KEEP", 1000)
# 降级的**压力门槛**（占上下文预算的比例）：估算用量低于这个比例时一律不动历史。
# 以前是无条件降级——只要工具结果超过 1500 字符、又不在最近一轮，下一轮就被削成 400 字符存根。
# 代价在真实会话里立刻显现（2026-11-14 02:06 那次「看看这个项目」）：
#   · README.md（255 行）被 read_file 读了 4 次不同区间（1-150 / 150-254 / 18-147 / 205-254），
#     因为上一轮读到的内容下一轮就被削没了 → 模型「不记得」，只能再读一遍（8 轮才干完一件事）；
#   · 每次削都是在**历史中段**改写消息，从改写点之后的前缀缓存全部作废，
#     逐轮少命中 1~3k tokens（那次 run 命中率 79.9%，而长 run 是 95%）。
# 上下文还很宽松（那次只用了 16k / 192k 预算，8%）时做这种删除是纯亏，所以加压力门控。
TOOL_RESULT_SHRINK_PRESSURE = float(_str("GA_TOOL_RESULT_SHRINK_PRESSURE", "0.6") or 0.6)

# 单次回复输出上限（OpenAI 兼容端点的 max_tokens）：0 = 不传（走端点默认）。
# 个别模型在收尾/思考时会无限生成，把单轮拖到几十秒——给个上限可兜住。
# 注意：推理模型的思考 token 也计入该上限，设太小会截断思考/工具调用，默认不设。
LLM_MAX_OUTPUT_TOKENS = _int("GA_LLM_MAX_TOKENS", 0)
# 长 run 后段自动降一档推理强度（用更少思考换每步生成时间）：
# 0 = 关闭（默认）。N>0 时第 N 轮起把档位里的 reasoning_effort 逐级下调
# （high→medium→low→minimal），每再满 N 轮再降一档，不低于最低档。
# 仅在档位显式配了 reasoning_effort 时生效；不配（走端点默认）时不动。
LLM_REASONING_STEP_DOWN_AFTER = _int("GA_REASONING_STEP_DOWN_AFTER", 0)

# ---------- 联网搜索（web_search / web_fetch 工具） ----------
# bing = 无 key 抓结果页（国内可达，默认）；ddg = DuckDuckGo HTML 版（部分网络不可达）；
# tavily = 需要 GA_TAVILY_KEY，质量最好。
SEARCH_PROVIDER = _str("GA_SEARCH_PROVIDER", "bing").lower()
TAVILY_API_KEY = _str("GA_TAVILY_KEY")
WEB_SEARCH_TIMEOUT_S = _int("GA_WEB_SEARCH_TIMEOUT_S", 15)

_ROLE_MODELS_RAW = _str("GA_ROLE_MODELS", "")
ROLE_MODELS: dict[str, str] = {}
for pair in _ROLE_MODELS_RAW.split(","):
    if ":" in pair:
        k, v = pair.split(":", 1)
        k = k.strip()
        # 旧配置里的键名（planner=会话模型）映射到 v3 的 main
        if k == "planner":
            k = "main"
        ROLE_MODELS[k] = v.strip()

# ---------- 认证 ----------
# 这个版本没有登录：身份是本机默认用户（见 game_agent/auth/local.py）。
# 保留占位是为了「不要因为 .env 里还留着旧键就报错」——旧的 GA_AUTH_MODE 一律忽略。
PLACEHOLDER_ENABLED = _bool("PLACEHOLDER_ENABLED", True)

# 首启后是否把 examples/cardgame 拷成示例工作区（并登记为项目）。
# 这个版本没有账号概念：「播种」只播示例项目，不再造 demo 账号/密码。
SEED_DEMO = _bool("GA_SEED_DEMO", False)

# ---------- 服务 ----------
HOST = _str("GA_HOST", "127.0.0.1")
# 默认端口故意避开 Windows 的「保留端口段」（excludedportrange）：启用 Hyper-V / WSL /
# Docker 后 winnat 会把成段端口留给自己，段内 bind 直接 WinError 10013，且这段范围每次
# 重启都可能漂。8765 恰好常落在保留段里（实测本机 8751-9350 全被占），所以默认换成
# 15000（远在动态端口 1024-15000 的上沿之外、也不在保留段）。被占用时 main 还会自动顺延。
PORT = _int("GA_PORT", 18888)
# 调试实例：另起端口连同一套数据库/数据目录做前端/接口联调时打开。
# 打开后本进程不写 pid 文件、不做启动僵尸清理、不起 worker 池（只提供 API + 静态页），
# 避免把主实例正在执行的 run 误标成「服务重启中断」，也避免两个进程抢同一个队列。
DEBUG_INSTANCE = _bool("GA_DEBUG_INSTANCE", False)
# 逃生开关：明知这套数据目录已有实例在跑，还是强行再起一个。
# 默认（False）时启动守卫会拦下并退出——因为第二个实例会跑「僵尸 run 清理」，把第一个
# 实例里正在跑的 run 全标成「服务重启，本次执行被中断」（2026-11-17 用户实测的
# 「老是重启、我的活被中断」）。放行的实例同样不做僵尸清理，见 core/instance_lock.py。
ALLOW_MULTI_INSTANCE = _bool("GA_ALLOW_MULTI_INSTANCE", False)

# ---------- 运行约束（设计 §6 / §7）----------
# 一次任务跑多久、调多少次工具都没有上限。这条只是「判死线」：某个 run 连续这么久
# 一个事件都没落（LLM 往返/工具调用/批量进度都会落事件），说明 worker 已经死了或
# 永久挂在外部调用上，界面不该一直挂「运行中」。30 分钟远大于任何单个正常步骤
# （单次 LLM 往返 / 单条命令 / 一次仓库同步都在几分钟内）。
RUN_STALE_S = _int("GA_RUN_STALE_S", 1800)
# 用户点了「停止」（runs.cancel_requested=1）后，最多再等这么久还没落终态，就由看门狗
# 强制把 run 落成 cancelled。正常的取消是秒级的（取消看门狗 task.cancel() 会立刻掀翻
# LLM 流 / 现在的 run_command）。但如果卡在一个**不可取消**的 await 上（历史 bug：to_thread
# 跑同步子进程），cancel 掀不翻，run 就会永远停在 running、界面「停止中」一直转圈。
# 这条是最后一道兜底：置位取消后超过这么久仍未结束 = 已经僵死，强制收尸成 cancelled。
CANCEL_GRACE_S = _int("GA_CANCEL_GRACE_S", 60)
# 并行执行的 run 数 = 进程内 worker 数（每个 worker 一次跑一条 run）。单机 lite 模式是
# 单进程多协程：几条 run 靠 asyncio 并行 + 各自线程里的阻塞调用跑，不需要起多个进程。
# 2026-10-30 用户要求「多个会话不需要排队，可以同时开多个对话」：默认从 3 提到 64，
# 单机使用相当于不设人为上限（真正瓶颈是 CPU/GPU/内存与外部端点配额）。
MAX_CONCURRENT_RUNS = _int("GA_MAX_CONCURRENT_RUNS", 64)  # 并行执行的 run 数（跨会话）
# 单次工具调用的「无进展」硬超时（秒）。注意语义是**连续这么久没有任何进展**才算卡死，
# 不是总时长上限：批量出图/配音每出一张(条)会推进 t.progress_seq，只要在出就不断重置计时。
# 这样「11 张各 1-2 分钟、正常一张张出」的批量不会被整批误杀（实测事故：总时长 900s
# 一刀切，把正在正确出图的批量判超时）。工具内部另有自己的单张时限（命令 300s / 出图 600s /
# TTS 600s / LLM 360s），这里是第三层兜底：某条外部调用（下载、第三方 SDK、半死连接）
# 卡住不返回时把控制权还给模型。默认略高于单张最长时限（出图/TTS 600s），只有真正
# 「毫无音讯」的挂起才会命中。设为 0 关闭。
TOOL_HARD_TIMEOUT_S = _int("GA_TOOL_HARD_TIMEOUT_S", 900)
# 同一轮里模型一次最多发多少个工具调用。模型偶尔会把整段清单拆成几十个并行调用，
# 一轮塞爆线程池/外部连接数；这里做个上限保护（超出的调用直接判失败并提示分批）。
MAX_TOOL_CALLS_PER_ROUND = _int("GA_MAX_TOOL_CALLS_PER_ROUND", 12)
# 同项目允许多个 run 并行改工作区（改动审阅/回滚走写前快照，互不依赖）；关掉则同项目串行
SAME_PROJECT_PARALLEL = _bool("GA_SAME_PROJECT_PARALLEL", True)
LOCK_TTL_S = _int("GA_LOCK_TTL_S", 120)              # 写锁 TTL
LOCK_WAIT_S = _int("GA_LOCK_WAIT_S", 1800)           # 锁等待上限（30 分钟）
TOOL_TIMEOUT_S = _int("GA_TOOL_TIMEOUT_S", 300)
# 一条需求「清单没做完」时，系统最多自动续跑几轮（0 = 关闭自动续跑，回到手动点「继续」）。
# 每轮 = 几十次 LLM 往返，所以有上限：做不了的东西不能靠无限重试烧额度。
AUTO_CONTINUE_MAX = _int("GA_AUTO_CONTINUE_MAX", 2)
# 搜索/递归扫描类命令的单独时限（默认 60s，会被 TOOL_TIMEOUT_S 夹住）。
# 实测：`Get-ChildItem -Recurse -File -Include *.py,*.png …` 这种整库扫描在十万级文件的目录上
# 能跑几分钟，界面上一直挂「执行中…」，用户只能看着卡住。构建/测试需要 300s，扫描不需要：
# 扫这么久通常说明扫的范围不对，早点掐断 + 引导去用会剪枝的内置 grep/glob/list_dir 更划算。
SEARCH_TIMEOUT_S = max(10, min(_int("GA_SEARCH_TIMEOUT_S", 60), TOOL_TIMEOUT_S))
# 单个子agent 的挂钟上限（秒）：spawn_agents 派出的子agent 超过这个时间还没交结论，
# 取消看门狗直接掐掉它，并把「超时未完成」如实回给主agent。为什么必须有：子agent 卡在
# 一次长工具调用/长思考里时没有任何上限，整批永远收不回来，界面就一直停在
# 「还有 N 个子agent 在后台跑」（2026-11-15 用户实测）。只掐这一条子任务，不连带整轮。
# 0 = 不限。
SUBAGENT_TIMEOUT_S = _int("GA_SUBAGENT_TIMEOUT_S", 600)


# 写工具受限目录（设计 §7.3.1）
PROTECTED_PREFIXES = (".venv", "venv", "__pycache__", ".git", ".trace/events")

# ---------- 运行命令白名单（按操作系统给） ----------
# 实测问题：模型经常凭直觉写「另一套系统」的命令（Windows 上写 ls/cat/pwd/2>/dev/null/`;` 串联，
# 或 Linux 上写 dir/where），每撞一次白名单就白烧一轮，还会反复重试同一个错命令。
# 所以：① 白名单本身按平台给；② 把「当前是什么系统 / shell / 该怎么写」同时注入系统提示与报错文案，
# 让它在第一条命令之前就知道该用哪套写法，撞墙时也能立刻自纠。
PLATFORM = "windows" if sys.platform == "win32" else ("macos" if sys.platform == "darwin" else "linux")
SHELL_NAME = "cmd.exe" if PLATFORM == "windows" else "/bin/sh"

# 两边都该有的：解释器 / 包管理 / 版本控制 / 回显 / 媒体转换（游戏项目要压视频、抽帧、转音频）
_COMMON_COMMANDS = [
    "python", "python3", "py", "pytest", "compileall", "ruff", "pip",
    "git", "svn", "npm", "npx", "node", "echo", "ffmpeg", "ffprobe",
]
# Windows（cmd.exe）——含常用只读查看/比较类内置命令（fc、comp、tree、attrib、ipconfig…），
# 这些以前全不在名单里，模型一猜就是「命令不在白名单：fc」，白烧一轮。
_WIN_COMMANDS = [
    "cd", "chdir", "pushd", "popd", "dir", "tree", "type", "more", "sort",
    "find", "findstr", "fc", "comp", "attrib", "where", "set", "cls", "chcp", "ver",
    "copy", "move", "ren", "del", "erase", "mkdir", "md", "rmdir", "rd",
    "xcopy", "robocopy", "expand", "subst",
    "start", "tasklist", "taskkill", "netstat", "ipconfig", "ping",
    "whoami", "hostname", "systeminfo", "driverquery",
    "powershell", "pwsh", "cmd", "curl", "tar",
]
# POSIX（/bin/sh）
_POSIX_COMMANDS = [
    "cd", "pwd", "ls", "tree", "cat", "head", "tail", "grep", "sed", "awk", "cut",
    "sort", "uniq", "wc", "diff", "file", "du", "df", "tee", "xxd", "od",
    "cp", "mv", "rm", "mkdir", "rmdir", "touch", "chmod", "ln", "stat", "basename", "dirname",
    "find", "xargs", "which", "env", "export", "bash", "sh", "true", "sleep", "date",
    "ps", "kill", "lsof", "curl", "wget", "tar", "unzip", "zip", "gzip", "gunzip",
]

# 含文件搬运/查找类：组长经常要把截图、构建产物搬到静态目录里再验证（cp/copy/move/mkdir…），
# 以前这类命令一律被拦（实测反馈：「命令不在白名单：cp」），逼它绕道 node -e 写脚本。
COMMAND_WHITELIST = list(dict.fromkeys(
    _COMMON_COMMANDS + (_WIN_COMMANDS if PLATFORM == "windows" else _POSIX_COMMANDS)
))

# ---------- 命令策略：默认 open（只拦真正的危险动作），不再按白名单卡语法 ----------
# 用户反馈（多次）：「为什么这么多白名单限制？不是语法对不对就行了吗？」
# 结论：语法交给 shell 自己判断，白名单那层只制造摩擦（每遇一条新命令就白烧一轮）。
# 现在的分工：
#   ① 危险动作 → 拦（不可逆的删盘/格式化/关机/远程执行/强推）；
#   ② bash 习惯 → 自动改写成当前 shell 的写法（/dev/null→2>nul、`;`→&&、`| head -N`→工具截取…）；
#   ③ 搜文本类（grep/rg）→ 直接改走内置 grep 工具，不再让命令失败；
#   ④ 其他一律放行，跑不对时由命令自己的报错说话。
# 需要回到严格白名单：GA_SHELL_POLICY=strict
SHELL_POLICY = (os.getenv("GA_SHELL_POLICY", "open") or "open").strip().lower()
if SHELL_POLICY not in ("open", "strict"):
    SHELL_POLICY = "open"

# 只拦这些：会毁掉机器/仓库/远端，且基本不可能出现在正常开发流程里
DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    (r"\bformat\s+[a-z]:", "格式化磁盘"),
    (r"\b(rd|rmdir)\s+/s\s+/q\s+\"?[a-z]:(\\|\"|$)", "递归删除整个磁盘"),
    (r"\bdel\b[^\n]*\b[a-z]:\\\s*(\*|$)", "删除盘符根目录"),
    (r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~/?|\"?[a-z]:\\?\*?\"?)\s*$", "递归删除根目录/家目录"),
    (r"\bdiskpart\b|\bbcdedit\b|\bbootrec\b|\bcipher\s+/w\b", "改分区/引导记录"),
    (r"\breg\s+(delete|add)\s+hklm", "改注册表 HKLM"),
    (r"\bnet\s+(user|localgroup)\b[^\n]*\s/add\b", "新增系统账号/提权"),
    (r"\b(shutdown|Restart-Computer|Stop-Computer)\b", "关机/重启"),
    (r"(curl|wget|iwr|Invoke-WebRequest)[^\n|]*\|\s*(bash|sh|iex|Invoke-Expression)", "下载即执行"),
    (r"powershell[^\n]*\s-(enc|encodedcommand)\b", "编码过的远程脚本"),
    (r"\bgit\s+push\b[^\n]*--force", "强推远端"),
    (r"\b(npm|pnpm|yarn)\s+publish\b", "发版到公共仓库"),
    (r"\bmkfs(\.\w+)?\b|\bdd\s+if=", "抹盘"),
]


def env_shell_hint() -> str:
    """当前系统的 shell 与命令写法（注入系统提示 / 白名单报错文案），避免模型凭直觉写另一套系统的命令。"""
    if PLATFORM == "windows":
        return (
            "Windows / cmd.exe。进目录 cd /d <目录>；"
            "列目录 dir、看文件 type <文件>、找程序 where <名字>、搬运 copy/move/del/mkdir；"
            "多条命令用 && 连接；丢弃输出写 2>nul。"
            "bash 习惯会被自动改写成 cmd 写法（/dev/null→2>nul、`;`→&&、$VAR→%VAR%、/c/x→C:/x、"
            "`| head -N`/`| tail -N`→去掉管道由工具截取），cp/mv/rm/ls/cat/pwd 会交给 powershell、"
            "which 会换成 where；但 grep/rg/sed/awk 没有可靠对应——搜文本一律用 grep 工具。"
        )
    return (
        f"{PLATFORM} / {SHELL_NAME}：进目录 cd、列目录 ls、看文件 cat、搜索 grep、翻尾部 tail；"
        "搬运 cp/mv/rm/mkdir、找程序 which；多条命令用 && 或 `;`，丢弃输出用 2>/dev/null。"
    )



def env_examples() -> str:
    """「这类事该怎么写」的常用命令范例——一开始就给，别让模型一边猜一边撞白名单。"""
    if PLATFORM == "windows":
        return (
            "列目录 dir /b · 看文件 type <f> · 看大文件尾部 powershell Get-Content <f> -Tail 20 · "
            "搜文本用 grep 工具（shell 里没有 grep，findstr 参数完全不同，别写）· "
            "比较两个文件 fc /b <a> <b>（更省事的是 read_file 两个文件）· "
            "查端口占用 netstat -ano | findstr :18888 · 查进程 tasklist | findstr node · "
            "杀进程 taskkill /F /PID <pid> · 压视频/转音频 ffmpeg -i in.mp4 out.webm"
        )
    return (
        "列目录 ls -la · 看文件 cat <f> · 看尾部 tail -n 20 <f> · 搜文本 grep -rn \"关键词\" . · "
        "比较文件 diff <a> <b> · 查端口 lsof -i :18888 · 查进程 ps aux | grep node · "
        "杀进程 kill -9 <pid> · 压视频/转音频 ffmpeg -i in.mp4 out.webm"
    )


def env_brief(cwd: str | None = None) -> str:
    """系统提示最前面的「运行环境」块。

    实测：模型不看环境就下命令（Windows 上写 ls/cat/`;`/2>/dev/null，Linux 上写 dir/where），
    被白名单拦一次就烧一轮，被「静默失效」坑一次更贵。所以把环境放在**上下文最前面**，
    而不是靠第一条命令去试探。
    """
    lines = [
        "## 运行环境（第一条命令之前先读这段，不要靠试错猜环境）",
        f"- 操作系统: {PLATFORM} · shell: {SHELL_NAME}"
        + (f" · 工作目录: {cwd}" if cwd else ""),
        "- **先想工具、再想 shell（90% 的探查根本不用 shell）**：搜文本用 grep 工具"
        "（pattern/path/include/context）、找文件用 glob、看某几行用 read_file(offset,limit)、"
        "读整篇用 read_file、找改动范围用 grep。**不要写 shell 的 grep/rg/findstr**"
        "（cmd 没有 grep，findstr 参数也完全不同，写了必被拦）；也不要 cat/head/tail 翻文件。"
        "shell 只留给：构建、跑测试、起/停服务、装依赖。",
        f"- 命令写法: {env_shell_hint()}",
        f"- 常用写法（直接照抄）: {env_examples()}",
        f"- 命令策略: {('只拦危险动作（删盘/格式化/关机/远程执行/强推），其余照常执行' if SHELL_POLICY == 'open' else '严格白名单（GA_SHELL_POLICY=strict）')}"
        f"；bash 习惯会自动改写成 cmd 写法；grep/rg 自动转成 grep 工具执行。",
        "- 常见可用写法: " + ", ".join(COMMAND_WHITELIST[:24]) + " …"
        "（清单外的命令也能跑，只是没在这里列出来）",
        "- 不确定怎么写时：能用 fs 工具（read_file/write_file/edit_file/glob/grep）就别绕 shell；"
        "非得跑就用 powershell -NoProfile -Command \"...\"、node -e \"...\"、python -c \"...\"。",
        "",
        "## 编辑规则（工具层强制，违反会被直接拦）",
        "- 改**已存在**的文件：先 read_file 拿到最新内容，再用 edit_file 精确替换（首选）或 write_file 整体重写；"
        "大文件用 offset/limit 分段读，只看要动的那几行也行。新建文件（路径不存在）和空文件不需要先读。",
        "- 你读过之后文件若被改动过（别的会话、构建产出、脚本生成），写入会被要求重新 read_file——照做，不要绕。",
        "- 整体重写会冲掉别人的改动：不确定就 edit_file 改局部；写完用 read_file 复查一眼。",
    ]
    return "\n".join(lines)


def ensure_dirs() -> None:
    for d in (DATA_DIR, PROJECTS_ROOT, LOG_DIR, TMP_DIR):
        d.mkdir(parents=True, exist_ok=True)


def default_python_bin() -> str:
    """项目用的默认 Python 解释器路径。

    普通源码运行时就是当前解释器（sys.executable）。但在 PyInstaller 打包出来的
    单文件 exe 里，sys.executable 指向 Trace.exe 本身——拿它去改写用户项目里的
    `python xxx.py` 命令会变成 `"Trace.exe" xxx.py`，必然跑错。所以冻结运行时
    返回空串（下游按 None 处理，命令保持原样走 PATH 里的 python）。
    """
    if getattr(sys, "frozen", False):
        return ""
    return sys.executable


def is_sqlite() -> bool:
    return DATABASE_URL.startswith("sqlite")


def is_mysql() -> bool:
    return DATABASE_URL.startswith(("mysql+aiomysql", "mysql+asyncmy"))
