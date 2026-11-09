# Build only application code and public static assets. No live data/config trees.
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
datas = [(str(root / 'web/static'), 'web/static'),
         (str(root / 'web/skills/research-visual-report'), 'web/skills/research-visual-report'),
         (str(root / 'packaging/README.txt'), '.'),
         (str(root / 'work/packaging/THIRD-PARTY-NOTICES.txt'), '.')]
for package in ('ddgs', 'primp', 'genai_prices'):
    datas += collect_data_files(package)
for distribution in ('pydantic-ai-slim', 'ddgs', 'genai-prices'):
    datas += copy_metadata(distribution)

a = Analysis([str(root / 'packaging/desktop_app.py')],
    pathex=[str(root), str(root / 'scripts')], binaries=[], datas=datas,
    hiddenimports=collect_submodules('ddgs') + ['uvicorn.logging', 'uvicorn.loops.asyncio',
        'uvicorn.protocols.http.h11_impl', 'uvicorn.lifespan.on'],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['pytest', 'IPython', 'numpy', 'pandas', 'matplotlib', 'scipy', 'torch',
              'tensorflow', 'notebook', 'jupyter', 'web.test_web'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='YanZhi', debug=False,
          manifest=str(root / 'packaging/app.manifest'),
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='YanZhi')
