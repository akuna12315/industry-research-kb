"""Audit the actual PyInstaller input manifest against application allowlists."""
import ast
import hashlib
import json
import os
import marshal
from pathlib import Path
from types import CodeType
from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

root = Path(__file__).resolve().parents[1]
manifest = ast.literal_eval((root / 'work/packaging/build/yanzhi/Analysis-00.toc').read_text(encoding='utf-8'))
allowed_code = {f'web/{name}.py' for name in (
    '__init__', 'agent_runtime', 'charts', 'docx_index', 'extraction_model', 'extraction_source',
    'full_extraction', 'knowledge', 'material_index', 'network', 'server', 'service', 'verification', 'research_pages', 'visual_pages', 'page_recovery', 'model_diagnostics', 'visual_skill', 'page_scope', 'visual_supports')}
allowed_code |= {'scripts/kb.py', 'scripts/workspace.py', 'packaging/desktop_app.py'}
allowed_data = {'packaging/README.txt', 'work/packaging/THIRD-PARTY-NOTICES.txt',
                'work/packaging/build/yanzhi/base_library.zip'}
issues, sources = [], {}


def normalized_code(code):
    return code.replace(co_filename='<application>', co_consts=tuple(
        normalized_code(value) if isinstance(value, CodeType) else value for value in code.co_consts))


frozen_modules = ZlibArchiveReader(str(root / 'work/packaging/build/yanzhi/PYZ-00.pyz'))
for name, source, kind in manifest[14]:
    path = Path(source)
    if path.is_relative_to(root) and path.relative_to(root).as_posix() in allowed_code:
        original = compile(path.read_bytes(), str(path), 'exec', dont_inherit=True)
        if normalized_code(original) != normalized_code(frozen_modules.extract(name)):
            issues.append(name + ': frozen module differs from current source')
entry_archive = CArchiveReader(str(root / 'work/packaging/dist/YanZhi/YanZhi.exe'))
entry = marshal.loads(entry_archive.extract('desktop_app'))
original = compile((root / 'packaging/desktop_app.py').read_bytes(), 'desktop_app', 'exec', dont_inherit=True)
if normalized_code(original) != normalized_code(entry):
    issues.append('desktop_app: frozen entry differs from current source')
for rows in manifest:
    if not isinstance(rows, list):
        continue
    for item in rows:
        source = item[1] if isinstance(item, tuple) and len(item) >= 3 else item if isinstance(item, str) else None
        if not source or not Path(source).is_absolute():
            continue
        path = Path(source).resolve()
        if not path.is_file() or not path.is_relative_to(root):
            continue
        relative = path.relative_to(root).as_posix()
        if relative.startswith('work/packaging-tools/venv/'):
            continue
        if relative in allowed_code or relative in allowed_data or relative.startswith(('web/static/', 'web/skills/research-visual-report/')):
            if path.is_file():
                sources[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            issues.append(relative)

bundle = root / 'work/packaging/dist/YanZhi'
for path in bundle.rglob('*'):
    relative = path.relative_to(bundle).as_posix()
    if path.is_file() and any(part in {'materials', 'evidence', 'history', 'reports', 'outputs', 'logs', 'config'}
                              for part in path.relative_to(bundle).parts):
        issues.append(relative)
    if path.is_file() and path.suffix.lower() in ('.exe', '.pyz', '.json', '.txt', '.html', '.js', '.css', '.zip'):
        raw = path.read_bytes()
        for private_marker in tuple(json.loads(os.environ.get('KB_PACKAGE_PRIVATE_MARKERS', '[]'))):
            if private_marker.encode('utf-8') in raw or private_marker.encode('utf-16-le') in raw:
                issues.append(relative + ': private material marker')

result = {'ok': not issues, 'issues': sorted(set(issues)), 'application_sources': sources,
          'bundled_files': sum(p.is_file() for p in bundle.rglob('*'))}
output = root / 'work/packaging/bundle-audit.json'
output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'ok': result['ok'], 'issues': result['issues'], 'application_sources': len(sources),
                  'bundled_files': result['bundled_files']}, ensure_ascii=False))
raise SystemExit(0 if result['ok'] else 1)
