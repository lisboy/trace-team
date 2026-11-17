<div align="center">

# 循迹 · Trace

**团队版 AI 研发助手**

一句话需求 → 读代码、改代码、编译、起服务、截图验收，全部由 AI 助手自己完成；
改动落成快照，供人审阅与回滚。

Python 3.12 · FastAPI · React 18 + TS · MySQL / MariaDB · 解压即用的 Linux 部署包

</div>

---

## 目录

- [什么是循迹](#什么是循迹)
- [角色与权限](#角色与权限)
- [核心特性](#核心特性)
- [它如何工作](#它如何工作)
- [快速开始](#快速开始)
- [配置](#配置)
- [打包分发](#打包分发)
- [项目结构](#项目结构)
- [技术栈](#技术栈)
- [文档](#文档)
- [版本](#版本)

---

## 什么是循迹

循迹（Trace）是一个**给团队用的游戏 / 软件研发智能体**。你在自己的工作区里发一句话需求，
AI 助手就会像一名真正的开发者那样**从读到写、从改到验**，端到端完成改动：

- 读项目结构 → 定位文件 → 写/改代码 → 编译、跑单测；
- 改完先做静态验证（编译 / 构建 / 单测）；**你要看效果时**才起服务 → 浏览器截图 →
  **自己看截图判断对错**（不对就改、重启、重截）；
- 最后交付一份**结构化变更清单**：这次动了哪些文件、净增删多少行、测试结论。

> 平台**不接管你的 git**。代码直接写进工作区，但每次 run 在写文件前都会自动留下**改动前快照**，
> 因此任何改动随时可以**审阅 diff 并一键回滚**。

团队版在个人版的基础上加了三件事：**账号登录**、**项目分派**、**管理员统一配置模型**。
素材库、语音、桌宠等个人创作功能已移除，界面聚焦「对话 + 团队 + 用量」。

## 角色与权限

| 角色 | 能做什么 |
|---|---|
| 管理员 | 团队页：新建账号、改角色、重置密码、分派项目、新建项目、给项目绑定唯一对话模型；可见全部项目 |
| 开发 | 在被分派的项目里发起对话，拥有编辑权限（可改代码、建修改型任务） |
| 需求 | 只读问答：可查看被分派项目、发消息做分析，不能改代码、不能建修改型任务 |

- 首次初始化会创建管理员 `admin`，初始密码 `666666`，**首次登录必须修改密码**。
- 管理员创建的账号、管理员重置过密码的账号，同样在下次登录时强制修改密码。
- 成员登录后只能看到被分派给自己的工作区；项目下拉框也只列出这些项目。
- 对话里的模型由管理员在「设置 → 智能体」中按项目绑定（作用范围选那个项目），**成员对话中不能切换模型**；
  项目只能用绑定的那一个档位。

## 核心特性

- **单一执行者，端到端交付** —— 一个助手对用户负责：查文件、改代码、编译、验证、汇报全包，
  需要看真实页面效果时自己起服务截图验收。
- **改动快照 + 变更报告** —— 工具层写前快照，任何项目（无论有没有 `.git`）都能审阅、回滚，
  并生成结构化变更清单。
- **项目记忆** —— 跨任务保留项目入口事实（起服务命令、测试账号、登录入口），
  上下文只给标题索引、按需取，避免爆上下文。
- **长效执行** —— 没有「轮次预算 / 墙钟上限」，只有明确的停止、卡死检测、静默结束才会终止。
  过程事件（思考、压缩、经验沉淀、重试）以行内时间线显示在 trace 中。
- **团队协作** —— MySQL / MariaDB、账号与角色、项目分派、管理员集中配置模型。
- **解压即用的 Linux 包** —— 内置 Python 运行时、全部 pip 依赖与 MariaDB，
  目标机不需要装 Python / MySQL / 开发工具；升级时保留数据库与上传文件。

## 它如何工作

```
用户：给主界面加一个卡牌详情弹窗
        │
        ▼
  ┌──────────────────────────────────────────────────────────┐
  │  助手（唯一执行者）                                          │
  │   1. list_dir / grep / read_file   读项目、定位文件          │
  │   2. write_file / edit_file        改代码（写前自动快照）     │
  │   3. run_command                   编译 / 跑单测             │
  │   4. start_service / screenshot_page 起服务 → 截图 → 自检    │
  │   5. report_to_user                交付变更清单 + 验证证据   │
  └──────────────────────────────────────────────────────────┘
        │
        ▼
  变更报告：改了哪些文件 / 净增删行数 / 测试结论
        │
        ▼
  用户：查看 diff 快照 → 满意保留，不满意一键回滚
```

## 快速开始

### 方式一：Linux 部署包（推荐，解压即用）

在 Linux 构建机上打出部署包（Windows 可双击 `package.cmd` 选 `[4]`，由 WSL 代打）：

```bash
bash packaging/linux-team/build-package.sh              # 普通包（不含 git/svn）
bash packaging/linux-team/build-package.sh --with-vcs   # 另加「一键安装版」
```

把 `dist/Trace-team-linux-<ver>-<时间戳>.tar.gz` 拷到目标机：

```bash
sudo mkdir -p /opt/trace
sudo tar -xzf Trace-team-linux-*.tar.gz -C /opt/trace
sudo chown -R $USER:$USER /opt/trace
cd /opt/trace

bash install.sh          # 首次会初始化内置 MariaDB
bash run.sh --daemon     # 或 bash run.sh 前台运行
```

浏览器打开 `http://<本机IP>:18888/`，用 `admin / 666666` 登录并按提示改密。
详细说明（升级保留数据、备份迁移、常见问题）见包内 `README.txt`。

Git / SVN 是可选组件，系统有就直接用，没有有两种补法：

- **一键安装版**（包名带 `-with-vcs`）：`install.sh` 自动扫描，已经有就跳过，
  缺哪个补哪个（包内 git 2.45.2 / svn 1.14.3 + 全部依赖，不联网、不需要 root）；
- **离线补装包** `Trace-team-vcs-linux-x86_64.tar.gz`（普通包装好之后随时补）：

```bash
tar xzf Trace-team-vcs-linux-x86_64.tar.gz && cd Trace-team-vcs
bash install.sh /opt/trace          # 默认只补系统缺的
```

运行时「系统优先，包内补位」：系统装的 git/svn 优先生效，包内版本只在系统缺的时候顶上。

### 方式二：从源码运行（开发）

团队版默认使用 MySQL / MariaDB；本机调试也可以临时用 SQLite。

```bash
# 1) 虚拟环境（需 Python 3.12+）
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux / macOS

# 2) 依赖
pip install -r requirements.txt

# 3) 配置
copy .env.example .env          # Linux 用 cp

# 4) 启动
python -m game_agent.main
```

- 用 MySQL：在 `.env` 填 `DATABASE_URL=mysql+aiomysql://用户:密码@127.0.0.1:3306/库名?charset=utf8mb4`
- 用 SQLite 调试：留空 `DATABASE_URL` 即可（库文件在 `GA_DATA_DIR/game_agent.db`）

建表在启动时自动完成（`init_db()`，幂等，含轻量迁移）。首启会创建默认团队与管理员
`admin / 666666`（已有管理员时只补齐角色配置，不动现有数据）。

## 配置

所有配置通过环境变量 / `.env` 提供，完整示例见 [`.env.example`](.env.example)。常用项：

| 变量 | 说明 | 默认 |
|---|---|---|
| `GA_MODE` | 运行模式（团队版仍用单进程 `lite`） | `lite` |
| `GA_DATA_DIR` | 数据目录（数据库 / 上传 / 工作区 / 日志）；团队版默认与个人版 `~/.trace` 分开 | `~/.trace-team` |
| `GA_SECRET_KEY` | 登录令牌签名主密钥；安装脚本自动生成 | 自动生成 |
| `DATABASE_URL` | 数据库连接串；团队版用 `mysql+aiomysql://…`，留空则退回 SQLite | 空 |
| `PROJECTS_ROOT` | 项目工作区根目录 | `<DATA_DIR>/projects` |
| `GA_HOST` / `GA_PORT` | 监听地址 / 端口 | `127.0.0.1` / `18888` |
| `GA_SEED_DEMO` | 首启把 `examples/cardgame` 播种成示例项目 | `0` |
| `GA_OPEN_BROWSER` | 就绪后自动打开浏览器 | 打包版开 / 源码关 |

模型配置全部在界面完成：管理员在「设置 → 模型配置」添加供应商与模型目录，
再在「设置 → 智能体」把作用范围切到团队默认或某个项目，给项目绑定唯一档位。
团队版没有 `.env` 全局兜底模型——没绑定档位时对话会直接失败并提示去绑定，
不会再悄悄使用环境变量里的旧 Key/旧端点。

## 打包分发

### Linux 团队版（内置运行时 + MariaDB）

必须在 Linux 上构建（要安装 manylinux wheel 并打包 Linux 版 Python + MariaDB）。
wheel 固定取 **manylinux2014（glibc 2.17）** 档位，构建末尾会做符号版本审计，
超出老服务器基线（`GLIBC` > 2.17 / `GLIBCXX` > 3.4.19 / `CXXABI` > 1.3.7）直接构建失败：

- **Linux 构建机**：`bash packaging/linux-team/build-package.sh`
- **Windows + WSL**：双击 `package.cmd` 选 `[4]`，脚本自动在 WSL 里构建，产物落在 `dist_release/`
- **没有本地 Linux**：推送到 GitHub 后，在 Actions 里运行 `Linux team package`（或打 `v*` tag 自动构建并附到 Release）

产物：`dist/Trace-team-linux-<ver>-<时间戳>.tar.gz`（含 `.sha256`）。
首次构建会把 Python 运行时与 MariaDB 下载到 `packaging/linux-team/vendor/` 缓存；
也可以 `--python-runtime` / `--mariadb` 指定本地包，离线构建。

要附送「一键安装版」（内置离线 git/svn）：

```bash
bash packaging/linux-team/build-vcs-bundle.sh   # 先做离线 git/svn 包（需 conda）
bash packaging/linux-team/build-package.sh --with-vcs
# 额外产物：Trace-team-linux-<ver>-<时间戳>-with-vcs.tar.gz
```

| 包 | 内容 | 适合 |
| --- | --- | --- |
| 普通包 | 运行时 + MariaDB + 前端 | 服务器已有 git/svn，或管理员自己装 |
| `-with-vcs` 一键安装版 | 另加离线 git/svn | 目标机不联网；`install.sh` 自动补缺 |
| `Trace-team-vcs-*.tar.gz` | 只有离线 git/svn | 已装普通包，之后才需要仓库同步 |

**升级不丢数据**：包内不含任何数据目录；升级时把新包解压覆盖到同一目录，
再运行 `bash install.sh`。脚本检测到已有数据库时只补程序、执行幂等的 `mariadb-upgrade`，
不会删除或重新初始化数据库；`.env`、上传文件、项目工作区原样保留。

### Windows（旧产物，非重点）

`package.cmd` 仍可产出 Windows 免安装 exe 与便携版；团队版以 Linux 部署为主。

打好的包请上传到 GitHub Releases，每个包配同名 `.sha256` 校验文件。

## 项目结构

```
game_agent/            # 后端
  agents/              #   单助手角色、工具实现、工具注册表
  api/                 #   FastAPI 应用与各路由（auth/team/runs/sessions/projects/...）
  auth/                #   登录、密码哈希、RBAC（管理员 / 开发 / 需求）
  core/                #   抽象、lite 容器、锁、队列、事件总线、技能、记忆
  db/                  #   SQLAlchemy 模型、会话、迁移
  llm/                 #   OpenAI / Anthropic 适配层、上下文、transcript
  orchestration/       #   编排器、快照、变更报告、项目记忆、策略
frontend/              # React 18 + TS + Vite + Tailwind 前端源码
web/dist/              # 前端构建产物（后端直接托管）
examples/cardgame/     # 自带示例项目（改代码 → 跑测试的靶子）
packaging/linux-team/  # Linux 团队版部署包（内置 Python + MariaDB）
packaging/exe/         # Windows 免安装 exe（旧产物）
tests/                 # pytest 测试
docs/                  # 设计文档与截图
```

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.12 · FastAPI · SQLAlchemy 2 (async) · asyncio |
| 数据 | MySQL / MariaDB（`aiomysql`）；SQLite（`aiosqlite`）仅本机调试 |
| 认证 | 账号密码（argon2id）· Bearer 令牌 · 首次登录强制改密 |
| 前端 | React 18 · TypeScript · Vite · Tailwind · Zustand |
| 大模型 | OpenAI 兼容 · Anthropic（统一适配层） |
| 打包 | Linux 一键包（内置 Python + MariaDB）· Windows PyInstaller |

## 文档

- [设计文档 v0.3](docs/DESIGN.md)
- [桌面端评估](docs/DESIGN-DESKTOP.md)
- [断点续跑与记忆](docs/DESIGN-RESUME-MEMORY.md)
- [Linux 部署包说明](packaging/linux-team/scripts/README.txt)

## 版本

当前版本 **v0.4.0**。

## License

本项目基于 [Apache License 2.0](LICENSE) 开源发布。

Copyright 2026 lisboy
