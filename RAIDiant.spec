# Build on each target OS: python -m PyInstaller --clean --noconfirm RAIDiant.spec
import sys
import shutil
from pathlib import Path

assets = Path(SPECPATH) / 'raidiant' / 'assets'
icon = str(assets / ('app-icon.icns' if sys.platform == 'darwin' else 'app-icon.ico')) if sys.platform in ('darwin', 'win32') else None
a = Analysis(['main.py'], pathex=[], binaries=[], datas=[(str(assets), 'raidiant/assets')], hiddenimports=[],
             hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='RAIDiant',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False, icon=icon)
if sys.platform == 'darwin':
    app = BUNDLE(exe, name='RAIDiant.app', bundle_identifier='app.raidiant.desktop',
                 icon=icon, info_plist={'CFBundleDisplayName': 'RAIDiant',
                                       'CFBundleShortVersionString': '0.3.4',
                                       'CFBundleVersion': '0.3.4',
                                       'NSHighResolutionCapable': True})
elif sys.platform.startswith('linux'):
    # Optional desktop integration; the executable remains self-contained.
    shutil.copyfile(assets / 'app-icon-512.png', Path(DISTPATH) / 'RAIDiant.png')
    shutil.copyfile(assets / 'RAIDiant.desktop', Path(DISTPATH) / 'RAIDiant.desktop')
