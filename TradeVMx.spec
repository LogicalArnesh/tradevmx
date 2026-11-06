# PyInstaller spec for TradeVMx (onedir native desktop app)
# Build:  python TradeVMx.spec
# Result: dist/TradeVMx/  (TradeVMx launcher + _internal/ with the full env)

import importlib.util
import os

block_cipher = None

# Only bundle the GUI backends that actually exist on this build host; asking
# PyInstaller for a missing platform module is a hard error on some versions.
_hidden = ['webview']
for _module in (
    'webview.platforms.winforms',
    'webview.platforms.edgechromium',
    'webview.platforms.gtk',
    'webview.platforms.qt',
    'webview.platforms.cef',
    'clr',
    'pythonnet',
):
    try:
        if importlib.util.find_spec(_module) is not None:
            _hidden.append(_module)
    except (ImportError, ValueError):
        pass

_icon = 'web/static/logo.ico'
if not os.path.exists(_icon):
    _icon = None


a = Analysis(
    ['desktop.py'],
    pathex=[],
    binaries=[],
    # Data files: the web UI (templates/ + static/, incl. logo) must be
    # bundled *next to* the launcher so the frozen app resolves /static/*
    # and /templates/ correctly (app.py's _resolve_web_roots checks
    # <exe_dir>/web/templates and <exe_dir>/_internal/templates).
    datas=[
        ('web/templates', 'web/templates'),
        ('web/static', 'web/static'),
    ],
    hidden_imports=_hidden + [
        'curio',
        'websockets',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        'pytest',
        'unittest',
        'tkinter',
        'PyQt5',
        'PyQt6',
        'PySide2',
        'PySide6',
    ],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name='TradeVMx',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    icon=_icon,
)

# Bundle the web/ UI (templates/ + static/, incl. logo) next to the launcher
# so the frozen app resolves /static/* and /templates/ correctly.
# (app.py's _resolve_web_roots checks x/web/templates and x/_internal/templates)
_coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    name='TradeVMx',
)
