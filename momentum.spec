# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from pathlib import Path

block_cipher = None

# 项目根目录
project_root = Path('.')
src_dir = project_root / 'src'

a = Analysis(
    [str(src_dir / 'momentum_agent' / '__main__.py')],
    pathex=[str(project_root), str(src_dir)],
    binaries=[],
    datas=[
        (str(src_dir / 'momentum_agent' / 'static'), 'momentum_agent/static')],
    hiddenimports=[
        'openai_agents',
        'pydantic',
        'dotenv',
        'momentum_agent',
        'momentum_agent.agent_app',
        'momentum_agent.config',
        'momentum_agent.logger',
        'momentum_agent.models',
        'momentum_agent.storage',
        'momentum_agent.web',
        'momentum_agent.context',
        'momentum_agent.planner',
        'momentum_agent.parser',
        'momentum_agent.auth',
    ],
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
    name='momentum-agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
