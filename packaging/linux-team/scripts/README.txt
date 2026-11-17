========================================================
 循迹 Trace · 团队版 Linux 部署包
========================================================

本包「解压即用」：内置 Python 运行时、全部依赖、MariaDB 与前端页面，
目标机不需要安装 Python / pip / MySQL，也不需要联网。

完整步骤、升级、备份恢复、systemd、防火墙与排障见包内《部署手册.md》。

--------------------------------------------------------
一、目标机要求
--------------------------------------------------------
- 64 位 Linux（x86_64），glibc >= 2.17
  支持 Ubuntu / Debian / CentOS / RHEL / Rocky / Alma / Fedora / openSUSE 等
  glibc 发行版；Alpine（musl）不支持。
- 内存建议 >= 2GB，磁盘建议 >= 5GB（程序约 400MB，其余是数据库与项目工作区）
- 用普通账号运行；root 也能跑，但数据目录会归 root 所有
- 项目要同步 Git / SVN 时，需要 git / subversion 命令行：
  ① 系统已装（发行版自带或管理员自己装的）→ Trace 直接用它，不重复装；
  ② 用「一键安装版」Trace-team-linux-*-with-vcs.tar.gz：install.sh 会扫描系统，
     已有就跳过，缺哪个补哪个（不联网、不需要 root）；
  ③ 自行安装：apt install git subversion / dnf install git subversion；
  ④ 已经装了普通包、之后才想补：用离线包 Trace-team-vcs-linux-x86_64.tar.gz。
  （不装也能用，只是项目里的仓库同步会失败并提示）

--------------------------------------------------------
二、安装
--------------------------------------------------------
1) 解压到固定目录，例如 /opt/trace：

     sudo mkdir -p /opt/trace
     sudo tar -xzf Trace-team-linux-*.tar.gz -C /opt/trace
     sudo chown -R $USER:$USER /opt/trace
     cd /opt/trace

2) 安装（首次会初始化内置 MariaDB，约 10~30 秒）：

     bash install.sh

   数据默认放在 ~/.trace-team（数据库、聊天附图、项目工作区、日志）。
   想放到别的盘：bash install.sh --data-dir /data/trace
   想换数据库端口：bash install.sh --db-port 33307

   「一键安装版」（文件名带 -with-vcs）还会顺带处理 git/svn：
   系统有就跳过，缺的用包内离线包补装；不想让它碰：bash install.sh --no-vcs

   之后想换数据目录，不要只改 .env：把旧数据目录整体拷到新位置，
   再用 --data-dir 指向新位置重新安装（详见部署手册）。

--------------------------------------------------------
三、启动 / 停止
--------------------------------------------------------
  前台：   bash run.sh            （Ctrl+C 停止）
  后台：   bash run.sh --daemon   （日志：<数据目录>/logs/run.out）
  停止：   bash stop.sh

  浏览器打开：http://<本机IP>:18888/
  （端口被占用会自动顺延，实际地址见启动输出或日志）

  首次登录：admin / 666666（首次登录必须改密）

  登录后的初始配置顺序：
    1. 设置 → 模型：添加模型供应商与团队公共档位（管理员才看得到）
       需要某个项目用专属模型时，把档位建在该项目下
    2. 团队 → 新建项目，并为每个项目绑定对话模型
    3. 设置 → 助手行为：可按项目覆盖提示词 / 模型（不选项目就是团队默认）
    4. 团队 → 新建账号（开发 / 需求），给账号分派项目
    5. 设置 → 项目记忆 / MCP：都是按项目分别配置的
    6. 成员登录后只能看到被分派项目的会话

--------------------------------------------------------
四、升级（重要：数据不会丢）
--------------------------------------------------------
数据库、上传文件、项目工作区全部在「数据目录」里，程序包里没有任何数据。
升级按下面顺序做（先停服务，避免旧进程继续写库）：

  cd /opt/trace
  bash stop.sh
  tar -xzf Trace-team-linux-新版本.tar.gz -C /opt/trace   # 覆盖程序文件
  bash install.sh
  bash run.sh --daemon

install.sh 检测到已有数据库时只会：
  - 保留 <数据目录>/mysql（全部数据）
  - 保留 chat_uploads / uploads / projects / logs
  - 保留你的 .env（缺 GA_SECRET_KEY 时会自动补一个）
  - 用新版程序补依赖，并对数据库执行幂等的 mariadb-upgrade
  - 启动时由程序做增量建表/加列迁移（只加不删）
绝不会删除或重新初始化数据库。

换目录安装：新目录解压后
  bash install.sh --data-dir /旧的数据目录
然后确认 .env 里的 GA_DATA_DIR 指向旧目录，即可沿用全部数据。

升级前建议先备份（见下）。

--------------------------------------------------------
五、备份 / 恢复 / 迁移
--------------------------------------------------------
  备份（不用停服务，数据库走事务一致性快照）：
    bash scripts/backup.sh
    # 产物在 <数据目录>/backups：db-*.sql.gz / files-*.tar.gz / env-*.bak / projects-*.tar.gz
    # 备份到别的盘：bash scripts/backup.sh --out /mnt/backup
    # 项目工作区很大时：加 --no-projects

  恢复（新机器或回滚）：
    1) 解压同版本（或更新版本）程序包，cd 进目录
    2) 解压数据目录到目标位置，放入 env-*.bak 为 .env
    3) bash install.sh --data-dir <数据目录>
    4) 启动数据库后导入：bash run.sh --daemon，再
       zcat db-*.sql.gz | runtime/mariadb/bin/mariadb --defaults-file=<数据目录>/mysql/my.cnf -u trace
    5) 解压 files-* 与 projects-* 到数据目录
  详细命令见《部署手册.md》。

  注意：GA_SECRET_KEY 换了之后已登录用户要重新登录；账号/项目数据不受影响。

--------------------------------------------------------
六、常见问题
--------------------------------------------------------
- MariaDB 起不来：
    看 <数据目录>/logs/mariadb.err
    常见原因：数据目录权限不足、磁盘满、端口 33306 被别的 MySQL 占用。
- 启动报「运行时依赖不完整」：
    包不完整（解压时漏文件），重新完整解压并重跑 install.sh。
- 打不开页面：
    .env 里 GA_HOST 默认 0.0.0.0；确认防火墙放行 18888/TCP。
    多人使用请放内网，不要直接暴露公网（需要 HTTPS 时前置 Nginx/Caddy）。
- RHEL / CentOS 上 MariaDB 被 SELinux 拦住：
    看 /var/log/audit/audit.log；给程序目录打标签或临时 setenforce 0 验证，
    长期方案见《部署手册.md》「SELinux」。
- 忘记管理员密码：
    其他管理员可在「团队」页重置；唯一管理员也忘了时用命令行重置：
    runtime/python/bin/python3 -m game_agent.cli reset-password admin
- 想换数据库端口：
    停服务后改 <数据目录>/mysql_port 与 .env 的 DATABASE_URL 端口，重跑 install.sh。
