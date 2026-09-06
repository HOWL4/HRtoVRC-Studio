# -*- mode: python ; coding: utf-8 -*-
# One-file build (single portable .exe).
# Build:  pyinstaller HRtoVRC_Studio.spec   (run from this folder)
from PyInstaller.utils.hooks import collect_all

datas = [('../HRtoVRC.ico', '.')]
binaries = []
hiddenimports = []
tmp_ret = collect_all('bleak')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('pythonosc')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['HRtoVRC_Studio.py'],
    pathex=['..'],            # so the shared HRGUI10 backend is importable
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# Everything is packed into a single self-contained executable so the .exe can be
# moved/shared on its own (no _internal folder => no "Failed to load Python DLL").
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='HRtoVRC_Studio',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='../HRtoVRC.ico',
)
