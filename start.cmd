@echo off
REM 循迹（Trace）· lite 模式一键启动（Windows 单机版）。
REM
REM 用法：
REM   双击 start.cmd        -> 菜单（启动服务 / 停止服务 / 退出）
REM   start.cmd run         -> 直接启动服务（等同菜单 [1]）
REM   start.cmd stop        -> 停止服务
REM 说明：首次启动内置管理员 admin / 666666，首次登录必须改密；其他账号由管理员在「团队」页创建。
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0"
set "ROOT=%~dp0"

if not exist .venv ( python -m venv .venv )
call .venv\Scripts\activate.bat
if not exist .env ( copy .env.example .env >nul )

REM ---- 带参数就按参数执行；双击（无参数）进菜单 ----
set "ACTION=%~1"
if "%ACTION%"=="" goto menu
if /i "%ACTION%"=="run" goto run
if /i "%ACTION%"=="stop" goto dobatchstop
goto menu

:menu
cls
echo ============================================================
echo   循迹 Trace
echo ------------------------------------------------------------
echo   [1] 启动服务（就绪后自动打开浏览器）
echo   [2] 停止服务（顺带停掉本地出图服务，释放显存）
echo   [0] 退出
echo ============================================================
echo.
set "CHOICE="
set /p "CHOICE=请输入数字后回车："
if "%CHOICE%"=="1" goto run
if "%CHOICE%"=="2" goto dobatchstop
if "%CHOICE%"=="0" goto end
goto menu

:run
REM DB: DATABASE_URL 留空 = SQLite（本机开发/试用）；团队正式部署用 MySQL（Linux 包内置 MariaDB）。
echo.
echo [trace] 正在启动服务，就绪后会自动打开浏览器...
echo         首次登录：admin / 666666（会强制修改密码）。
echo         端口默认 18888，被占用或落在系统保留段会自动顺延，日志里会打印实际端口。
echo         关闭本窗口 = 停止服务。
echo.
set GA_MODE=lite
set GA_OPEN_BROWSER=1
python -m game_agent.main
echo.
echo [trace] 服务已停止。
pause
goto :eof

:dobatchstop
REM 交给同目录的 stop.cmd（它会再确认一次），然后回到菜单。
if exist "%ROOT%stop.cmd" (
  call "%ROOT%stop.cmd"
) else (
  echo [trace] 未找到 stop.cmd，改用脚本停止逻辑。
  python "%ROOT%scripts\stop_all.py"
  pause
)
goto menu

:end
popd
endlocal
