@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0.."
title ytmp-dl installer

echo ============================================
echo   ytmp-dl - one-click installer
echo ============================================
echo.

REM ---------- 1. Python 3.13 (use existing, else silent install) ----------
set "PY="
set "PY313=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if exist "!PY313!" set "PY=!PY313!"
if not defined PY (
  where python >nul 2>nul && python -c "import sys; raise SystemExit(0 if sys.version_info[:2]==(3,13) else 1)" 2>nul && set "PY=python"
)
if not defined PY (
  echo Python 3.13 not found - downloading installer...
  powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.13.5/python-3.13.5-amd64.exe' -OutFile \"$env:TEMP\python-install.exe\""
  if errorlevel 1 (echo download failed & pause & exit /b 1)
  echo Installing Python 3.13 silently ^(no clicks needed^)...
  "%TEMP%\python-install.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0 Include_doc=0 Include_launcher=0
  set "PY=!PY313!"
  if not exist "!PY!" (echo Python install failed & pause & exit /b 1)
)
echo Using: !PY!
"!PY!" --version || (echo Python broken & pause & exit /b 1)

REM ---------- 2. venv + deps ----------
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -c "import sys; raise SystemExit(0 if sys.version_info[:2]==(3,13) else 1)" 2>nul
  if errorlevel 1 (echo Removing .venv built with wrong Python version & rmdir /s /q .venv)
)
if not exist .venv (
  echo Creating virtualenv...
  "!PY!" -m venv .venv || (echo venv failed & pause & exit /b 1)
)
call .venv\Scripts\activate.bat
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt pyinstaller || (echo pip install failed & pause & exit /b 1)
echo Dependencies installed.

REM ---------- 3. ffmpeg ----------
if not exist bin\ffmpeg.exe set NEED_FFMPEG=1
if not exist bin\ffprobe.exe set NEED_FFMPEG=1
if defined NEED_FFMPEG (
  echo Downloading ffmpeg essentials build ^(~80 MB^)...
  if not exist bin mkdir bin
  powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile 'ffmpeg-dl.zip'"
  powershell -NoProfile -Command "Expand-Archive -Force 'ffmpeg-dl.zip' 'ffmpeg-tmp'"
  for /d %%d in (ffmpeg-tmp\ffmpeg-*) do (
    copy /y "%%d\bin\ffmpeg.exe" bin\ffmpeg.exe >nul
    copy /y "%%d\bin\ffprobe.exe" bin\ffprobe.exe >nul
  )
  rmdir /s /q ffmpeg-tmp ffmpeg-dl.zip 2>nul
)
echo ffmpeg ready.

REM ---------- 4. build exe ----------
echo Building ytmp-dl.exe ^(takes a few minutes^)...
python -c "import datetime,pathlib;pathlib.Path('ytmp_dl/_buildinfo.py').write_text('BUILD_STAMP = '+repr(datetime.datetime.now().isoformat(timespec='minutes'))+'\n')"
pyinstaller ytmp-dl.spec --noconfirm --clean > build-log.txt 2>&1
if errorlevel 1 (
  echo BUILD FAILED - see build-log.txt
  pause & exit /b 1
)

REM ffmpeg must sit next to the exe — the frozen app looks in dist\bin\
if not exist dist\bin mkdir dist\bin
copy /y bin\ffmpeg.exe dist\bin\ffmpeg.exe >nul
copy /y bin\ffprobe.exe dist\bin\ffprobe.exe >nul

REM ---------- 5. config + shortcut ----------
if not exist dist\config.toml copy /y config.toml.example dist\config.toml >nul
powershell -NoProfile -Command "$ws=New-Object -ComObject WScript.Shell; $s=$ws.CreateShortcut([IO.Path]::Combine([Environment]::GetFolderPath('Desktop'),'ytmp-dl.lnk')); $s.TargetPath='%CD%\dist\ytmp-dl.exe'; $s.Arguments='gui'; $s.WorkingDirectory='%CD%\dist'; $s.IconLocation='%CD%\dist\ytmp-dl.exe,0'; $s.Save()"

echo.
echo ============================================
echo   DONE!  dist\ytmp-dl.exe is ready.
echo   A 'ytmp-dl' shortcut was put on your Desktop.
echo.
echo   Next: run it, click 'Sign in' in the Account
echo   panel, then 'Run All'. Music lands in
echo   dist\music\ - point Plex there.
echo ============================================
start "" "%CD%\dist\ytmp-dl.exe" gui
endlocal
