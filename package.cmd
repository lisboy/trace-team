@echo off
REM ============================================================================
REM  package.cmd  --  one-click build of the distribution packages (double-click)
REM
REM  Produces into  <repo>\dist_release\  :
REM    * Trace-lite-win64-<ver>-<stamp>.zip     (no install; target needs Python)
REM    * Trace-win64-exe-<ver>-<stamp>.zip      (single Trace.exe; zero install)
REM  plus a .sha256 next to each archive.
REM
REM  The Linux team package (bundled Python + MariaDB) must be built on Linux.
REM  Menu [4] builds it inside WSL when a WSL distro is installed (reuses the
REM  frontend built above via --no-build); with no WSL it fails with instructions
REM  instead of pretending to succeed. If dist\Trace-team-vcs-linux-x86_64.tar.gz
REM  exists it also offers to build the one-click variant with offline git/svn.
REM
REM  Menu: [1] win lite + win exe  [2] win lite  [3] win exe  [4] linux (WSL)
REM  It reuses packaging\windows-lite\build-package.ps1 and packaging\exe\build-exe.ps1
REM  -- no logic is duplicated here. The frontend is built once, then builders run with -NoBuild.
REM ============================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title Trace - build packages

set "ROOT=%~dp0"
if "%ROOT:~-1%"=="\" set "ROOT=%ROOT:~0,-1%"
set "OUT=%ROOT%\dist_release"
if not exist "%OUT%" mkdir "%OUT%" >nul 2>&1

set "VPY=%ROOT%\.venv\Scripts\python.exe"

if not exist "%ROOT%\game_agent" (
  echo [x] Put this script in the repository root, next to the game_agent\ folder.
  echo     Current folder: %ROOT%
  goto fail
)
if not exist "%VPY%" (
  echo [x] Python venv not found: %VPY%
  echo     Create it first, then re-run this script:
  echo        python -m venv .venv
  echo        .venv\Scripts\python.exe -m pip install -r requirements.txt
  goto fail
)
where npm >nul 2>&1
if errorlevel 1 (
  echo [x] npm not found on PATH - install Node.js 18+ first ^(needed to build the frontend^).
  goto fail
)

REM WSL is only needed for menu [4]; probe it once (no distro => wsl.exe errors out).
set "WSL="
wsl.exe -e uname -s >nul 2>&1
if not errorlevel 1 set "WSL=1"

:menu
cls
echo ============================================================
echo   Trace - build distribution packages
echo   output folder: %OUT%
echo ------------------------------------------------------------
echo   [1] Windows packages   win-lite zip + win-exe zip
echo   [2] Windows lite zip    (needs Python on target machine)
echo   [3] Windows exe  zip    (single Trace.exe, zero install)
echo   [4] Linux team package  (built inside WSL if installed)
echo   [0] Quit
echo ============================================================
set "SEL="
set /p "SEL=Choose 1-4, or 0 to quit : "
if "%SEL%"=="1" goto sel_all
if "%SEL%"=="2" goto sel_winlite
if "%SEL%"=="3" goto sel_winexe
if "%SEL%"=="4" goto sel_linux
if "%SEL%"=="0" goto done
goto menu

:sel_all
set "DO_WINLITE=1"
set "DO_WINEXE=1"
set "DO_LINUX="
goto askbuild

:sel_winlite
set "DO_WINLITE=1"
set "DO_WINEXE="
set "DO_LINUX="
goto askbuild

:sel_winexe
set "DO_WINLITE="
set "DO_WINEXE=1"
set "DO_LINUX="
goto askbuild

:sel_linux
set "DO_WINLITE="
set "DO_WINEXE="
set "DO_LINUX=1"
goto askbuild

:askbuild
set "RB=Y"
if exist "%ROOT%\web\dist\index.html" set /p "RB=Rebuild frontend first (npm run build)? [Y/n] : "
if /i "%RB%"=="n" (
  set "SKIP_FE=1"
) else (
  set "SKIP_FE="
)
if not exist "%ROOT%\web\dist\index.html" if /i "%RB%"=="n" (
  echo [x] web\dist is missing and rebuild was declined - nothing to package.
  pause
  goto menu
)

REM ---- build the frontend exactly once ----
if not defined SKIP_FE call :build_frontend
if errorlevel 1 goto menu

set "RC=0"
if defined DO_WINLITE call :do_winlite
if errorlevel 1 goto fail
if defined DO_WINEXE  call :do_winexe
if errorlevel 1 goto fail
if defined DO_LINUX   call :do_linux
if errorlevel 1 goto fail

echo.
echo [checksum] writing .sha256 ...
"%VPY%" "%ROOT%\packaging\checksums.py" "%OUT%"
if errorlevel 1 set "RC=1"

set "NART=0"
for %%f in ("%OUT%\*.zip" "%OUT%\*.tar.gz") do set /a NART+=1
if "%NART%"=="0" (
  echo [!] No package archive was produced.
  set "RC=1"
)

echo.
echo ============================================================
if "%RC%"=="0" (
  echo   BUILD OK
) else (
  echo   BUILD FAILED - see messages above
)
echo   output folder: %OUT%
echo ============================================================
dir /b /o-d "%OUT%"
echo.
pause
goto menu

REM ------------------------------------------------------------------ workers
:build_frontend
echo.
echo === [frontend] npm ci / npm run build ... ===
pushd "%ROOT%\frontend"
if not exist "node_modules" (
  call npm ci
  if errorlevel 1 (popd & echo [x] npm ci FAILED & exit /b 1)
)
call npm run build
if errorlevel 1 (popd & echo [x] npm run build FAILED & exit /b 1)
popd
if not exist "%ROOT%\web\dist\index.html" (
  echo [x] web\dist\index.html still missing after build.
  exit /b 1
)
exit /b 0

:do_winlite
echo.
echo === [win-lite] packaging ... ===
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\packaging\windows-lite\build-package.ps1" -OutDir "%OUT%" -NoBuild
if errorlevel 1 (echo [x] win-lite FAILED & exit /b 1)
goto :eof

:do_winexe
echo.
echo === [win-exe] packaging (PyInstaller, ~1-3 min) ... ===
"%VPY%" -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
  echo [x] PyInstaller is not installed. Install it, then re-run:
  echo        "%VPY%" -m pip install pyinstaller
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\packaging\exe\build-exe.ps1" -OutDir "%OUT%" -NoBuild
if errorlevel 1 (echo [x] win-exe FAILED & exit /b 1)
goto :eof

:do_linux
echo.
echo === [linux-team] packaging (inside WSL) ... ===
if not defined WSL goto linux_nowsl
set "WROOT="
for /f "usebackq delims=" %%p in (`wsl.exe wslpath -a "%ROOT%"`) do if not defined WROOT set "WROOT=%%p"
set "WOUT="
for /f "usebackq delims=" %%p in (`wsl.exe wslpath -a "%OUT%"`) do if not defined WOUT set "WOUT=%%p"
if not defined WROOT (
  echo [x] wslpath failed to map "%ROOT%" into WSL.
  exit /b 1
)
echo     repo in WSL : %WROOT%
echo     output      : %WOUT%
echo     first build downloads Python runtime + MariaDB into packaging/linux-team/vendor
set "VCSOPT="
if not exist "%ROOT%\dist\Trace-team-vcs-linux-x86_64.tar.gz" goto vcs_done
echo     found dist\Trace-team-vcs-linux-x86_64.tar.gz (offline git/svn)
set "WV="
set /p "WV=Also build the one-click package with offline git/svn? [y/N] : "
if /i "%WV%"=="y" set "VCSOPT=--with-vcs"
:vcs_done
wsl.exe -e bash -lc "cd '%WROOT%' && bash packaging/linux-team/build-package.sh --out '%WOUT%' --no-build %VCSOPT%"
if errorlevel 1 (
  echo [x] linux-team FAILED - see the [build][x] line above.
  exit /b 1
)
goto :eof

:linux_nowsl
echo [x] The Linux team package must be built on Linux, and this machine has no usable WSL distro.
echo.
echo     Option A - build here after a one-time install (needs admin, reboot may be required):
echo        wsl --install -d Ubuntu
echo     then double-click this script again and pick [4].
echo.
echo     Option B - no local Linux: push this repo and run the
echo        "Linux team package" GitHub Actions workflow (Actions tab -^> Run workflow).
echo     Nothing was built.
exit /b 1

:fail
echo.
echo ============================================================
echo   BUILD FAILED - fix the error above, then re-run.
echo   (No checksum was written; existing packages are untouched.)
echo ============================================================
echo.
pause
endlocal
exit /b 1

:done
endlocal
exit /b 0
