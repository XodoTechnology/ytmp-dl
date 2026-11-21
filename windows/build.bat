@echo off
setlocal
REM Build ytmp-dl.exe for Windows. Run from the repo root in cmd.exe.
REM Produces dist\ytmp-dl.exe (self-contained: python + deps + ffmpeg).
cd /d "%~dp0.."

where python >nul 2>nul || (echo Python not found - run windows\install.bat instead & exit /b 1)

if not exist .venv (
  python -m venv .venv
)
call .venv\Scripts\activate.bat

pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt pyinstaller

REM --- fetch ffmpeg/ffprobe (one-time, ~80 MB) ---
if not exist bin\ffmpeg.exe set NEED_FFMPEG=1
if not exist bin\ffprobe.exe set NEED_FFMPEG=1
if defined NEED_FFMPEG (
  echo Downloading ffmpeg essentials build...
  if not exist bin mkdir bin
  powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile 'ffmpeg-dl.zip'"
  powershell -NoProfile -Command "Expand-Archive -Force 'ffmpeg-dl.zip' 'ffmpeg-tmp'"
  for /d %%d in (ffmpeg-tmp\ffmpeg-*) do (
    copy /y "%%d\bin\ffmpeg.exe" bin\ffmpeg.exe >nul
    copy /y "%%d\bin\ffprobe.exe" bin\ffprobe.exe >nul
  )
  rmdir /s /q ffmpeg-tmp ffmpeg-dl.zip
)

REM stamp the build so the GUI/--version show which exe is running
python -c "import datetime,pathlib;pathlib.Path('ytmp_dl/_buildinfo.py').write_text('BUILD_STAMP = '+repr(datetime.datetime.now().isoformat(timespec='minutes'))+'\n')"

pyinstaller ytmp-dl.spec --noconfirm --clean

REM ffmpeg must sit next to the exe — the frozen app looks in dist\bin\
if not exist dist\bin mkdir dist\bin
copy /y bin\ffmpeg.exe dist\bin\ffmpeg.exe >nul
copy /y bin\ffprobe.exe dist\bin\ffprobe.exe >nul

echo.
echo Done: dist\ytmp-dl.exe
echo Copy config.toml.example to config.toml and cookies.txt next to the exe,
echo then run:  dist\ytmp-dl.exe gui
endlocal
