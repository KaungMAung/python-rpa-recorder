# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

truststore_datas, truststore_binaries, truststore_hiddenimports = collect_all('truststore')

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=truststore_binaries,
    datas=[
        ('templates/sharepoint/rpa_run_log_template.xlsx', 'templates/sharepoint'),
    ] + truststore_datas,
    hiddenimports=[
        'pynput.keyboard._win32',
        'pynput.mouse._win32',
        'PIL.ImageGrab',
        'cv2',
        'numpy',
        'pyautogui',
    ] + truststore_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PythonRPARecorder',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='PythonRPARecorder',
)
