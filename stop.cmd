@echo off
REM 循迹 Trace - stop launcher.
REM
REM 双击 stop.cmd -> 停止本服务（后端）。
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0"

echo ============================================================
echo   循迹 Trace - 停止服务
echo ------------------------------------------------------------
echo   将停止本地服务进程
echo ============================================================
echo.
set "GO="
set /p "GO=确认停止服务？y 回车继续，其他键取消： "
if /i not "%GO%"=="y" goto cancel
echo.

if not exist .venv goto nopy
call .venv\Scripts\activate.bat
python "%~dp0scripts\stop_all.py"
if errorlevel 1 goto failed
echo.
echo [trace] 已停止服务（如仍有窗口，可直接关闭，或按 Ctrl+C）。
pause
goto :eof

:failed
echo.
echo [trace] 停止服务时出现异常，请看上方输出（若为端口被占用，请手动结束对应 PID）。
pause
goto :eof

:nopy
echo [trace] 没有找到 .venv，无法调用停止脚本。
echo         可直接在任务管理器里结束本目录的 python.exe 进程。
pause
goto :eof

:cancel
echo 已取消，未做任何改动。
ping -n 3 127.0.0.1 >nul
goto :eof
