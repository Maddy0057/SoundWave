# -*- mode: python ; coding: utf-8 -*-
# SoundWave.spec — PyInstaller spec for a lean single-file .exe build

import os
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

# Paths
project_dir = os.path.abspath('.')

# Collect ytmusicapi data files (locales, translations)
ytmusic_datas = collect_data_files('ytmusicapi')

a = Analysis(
    ['main.py'],
    pathex=[project_dir],
    binaries=[],
    datas=[
        ('templates', 'templates'),           # HTML template
    ] + ytmusic_datas,
    hiddenimports=[
        'waitress',
        'waitress.task',
        'waitress.channel',
        'waitress.server',
        'waitress.adjustments',
        'waitress.utilities',
        'ytmusicapi',
        'flask',
        'jinja2',
        'markupsafe',
        'requests',
        'urllib3',
        'charset_normalizer',
        'certifi',
        'idna',
        'bs4',
        'soupsieve',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # --- Exclude heavy/unused stdlib modules ---
        'tkinter', '_tkinter', 'Tkinter',
        'matplotlib', 'numpy', 'scipy', 'pandas',
        'PIL', 'Pillow',
        'PyQt5', 'PyQt6', 'PySide2', 'PySide6',
        'wx', 'gtk',
        'test', 'tests', 'unittest',
        'doctest', 'pydoc', 'pdb',
        'xmlrpc', 'ftplib', 'imaplib', 'nntplib', 'poplib', 'smtplib', 'telnetlib',
        'turtle', 'turtledemo',
        'curses', '_curses',
        'lib2to3',
        'ensurepip', 'venv', 'distutils',
        'asyncio',            # Not used (we use threading)
        'multiprocessing',    # Not used
        'concurrent',         # Not used
        'email.mime',
        'setuptools',
        'pkg_resources',
    ],
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
    name='SoundWave',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,                # Apply UPX compression if available
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,            # Windowed app — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='logo.ico',          # App icon
)
