"""Keep third-party dependencies conventional; proprietary modules are native only."""
from pathlib import Path
import sysconfig
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parents[1]
work = root / 'work/protected'
stage = work / 'source'
native = list((work / 'build').glob('protected_core' + sysconfig.get_config_var('EXT_SUFFIX')))
if len(native) != 1:
    raise RuntimeError('Expected exactly one native protected business module')
datas = [(str(stage / 'web/static'), 'web/static'), (str(stage / 'README.txt'), '.'),
         (str(root / 'web/skills/research-visual-report'), 'web/skills/research-visual-report'),
         (str(stage / 'THIRD-PARTY-NOTICES.txt'), '.')]
for package in ('ddgs', 'primp', 'genai_prices'):
    datas += collect_data_files(package)
for distribution in ('pydantic-ai-slim', 'ddgs', 'genai-prices'):
    datas += copy_metadata(distribution)
a = Analysis([str(stage / 'protected_core.py')], pathex=[str(stage), str(stage / 'scripts')],
    binaries=[], datas=datas,
    hiddenimports=collect_submodules('ddgs') + ['uvicorn.logging', 'uvicorn.loops.asyncio',
        'uvicorn.protocols.http.h11_impl', 'uvicorn.lifespan.on'],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['pytest', 'IPython', 'numpy', 'pandas', 'matplotlib', 'scipy', 'torch',
              'tensorflow', 'notebook', 'jupyter', 'web.test_web'], noarchive=False)
own = lambda name: name in {'kb','workspace','protected_core'} or name == 'web' or name.startswith('web.')
a.pure = [item for item in a.pure if not own(item[0])]
a.datas = [item for item in a.datas if not item[0].endswith('.pyi')]
a.binaries += [(native[0].name, str(native[0]), 'EXTENSION')]
a.scripts = [('protected_bootstrap', str(work / 'bootstrap.py'), 'PYSOURCE')
             if Path(item[1]) == stage / 'protected_core.py' else item for item in a.scripts]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='YanZhi', debug=False,
    manifest=str(root / 'packaging/app.manifest'), bootloader_ignore_signals=False,
    strip=False, upx=False, console=False, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='YanZhi')
