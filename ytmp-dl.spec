# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for ytmp-dl (Windows build via windows/build.bat)."""
import os
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None

datas = [
    ("ytmp_dl/static", "ytmp_dl/static"),
]
# runtime data files each package needs inside the frozen exe — most
# importantly ytmusicapi/locales/*.mo (missing -> "no translation file
# found for domain 'base'" crash on every YTMusic() call)
for _pkg in ("ytmusicapi", "yt_dlp", "flask", "jinja2",
             "requests", "certifi", "mutagen"):
    datas += collect_data_files(_pkg)

binaries = []
for name in ("ffmpeg.exe", "ffprobe.exe"):
    p = os.path.join("bin", name)
    if os.path.isfile(p):
        binaries.append((p, "bin"))

hiddenimports = collect_submodules("yt_dlp")

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="ytmp-dl",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
