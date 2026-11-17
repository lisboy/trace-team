循迹（Trace）团队版 —— Git / SVN 离线安装包
=============================================

这是给已经部署好的 Trace 用的可选组件：把 git 和 svn 装进 Trace 自己的
vendor/vcs 目录。目标机不需要联网、不需要 root、不需要装任何系统包，
也不需要开发工具。

默认只补系统里缺的：系统 PATH 里已有 git 就不装 git，已有 svn 就不装 svn，
两个都有就什么都不做。Trace 运行时也是「系统优先，包内补位」——系统装的、
发行版自带的、管理员自己装的都照常生效，包内的只在系统缺的时候顶上。

适用：x86_64 Linux，glibc >= 2.17（Ubuntu/Debian/CentOS/RHEL/Rocky/Alma/
Fedora/openSUSE 都可以）。Alpine（musl）不支持。

版本
----
  git    2.45.2
  svn    1.14.3（subversion，含 svn / svnadmin 等命令行）

安装
----
  1) 解压
       tar xzf Trace-team-vcs-linux-x86_64.tar.gz
       cd Trace-team-vcs-linux-x86_64

  2) 一键安装（默认找 /opt/trace，也可以显式给路径）
       bash install.sh                # 只补系统缺的（装到 /opt/trace/vendor/vcs）
       bash install.sh /path/to/trace # Trace 在别的目录时
       bash install.sh --all ...      # 两个都装，但运行时仍优先用系统的
       bash install.sh --force ...    # 覆盖重装（自动包含 --all）
       bash install.sh --prefer-bundled ...  # 两个都装，运行改为优先用包内的

  3) 如果 Trace 正在运行，重启一次让它拿到新的 PATH：
       bash /opt/trace/stop.sh && bash /opt/trace/run.sh --daemon

装完之后
--------
  - run.sh 启动时系统优先：PATH 里已有的 git/svn 优先生效，系统没有的才用
    <Trace目录>/vendor/vcs/bin。只有实际用到包内 git 时才指定包内 CA 证书，
    系统 git 的证书配置不受影响。
  - 想强制用包内版本（例如系统 git 太旧）：
      touch <Trace目录>/vendor/vcs/prefer-bundled      # 重启后生效
    改回系统优先：删掉这个文件再重启。
  - 卸载：删掉 <Trace目录>/vendor/vcs 整个目录即可，不动数据库和项目。
  - 升级 Trace 安装包不会删掉 vendor/vcs（覆盖解压只换程序文件）。

常见问题
--------
  Q: 安装时报 glibc 版本太旧 / 架构不匹配？
  A: 目标机不满足 x86_64 + glibc >= 2.17。可以改用系统包管理器：
     apt install git subversion  /  dnf install git subversion

  Q: 报「没找到 Trace 安装目录」？
  A: 显式指定：bash install.sh /opt/trace（目录里应有 run.sh 或 scripts/run.sh）

  Q: HTTPS 仓库报证书错误？
  A: 包内已带 Mozilla CA 列表。如果仓库用企业自签 CA，把 CA 证书追加到
     <Trace目录>/vendor/vcs/ssl/cacert.pem 后重启 Trace。

  Q: git 能跑但 clone 报错？
  A: install.sh 会做一次本地 init/commit/clone 自检；自检失败时请把输出和
     `uname -a`、glibc 版本一起反馈。
