"""Verify protected application code and exclude private library data."""
import hashlib
import json
import os
import marshal
from pathlib import Path
from xml.etree import ElementTree as ET
from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / 'work/protected'
BUNDLE = WORK / 'dist/YanZhi'
issues = []
expected = {'protected_core', 'kb', 'workspace', 'web', *('web.' + n for n in (
    'agent_runtime','charts','docx_index','extraction_model','extraction_source','full_extraction',
    'knowledge','material_index','network','server','service','verification','research_pages', 'visual_pages', 'page_recovery', 'model_diagnostics', 'visual_skill', 'page_scope', 'visual_supports'))}
report = ET.parse(WORK / 'compilation-report.xml').getroot()
modules = {item.get('name'): item.get('kind') for item in report.findall('module')}
for name in expected:
    if modules.get(name) not in ('CompiledPythonModule', 'CompiledPythonPackage'):
        issues.append('Business module not native compiled: ' + name)
if report.get('completion') != 'yes':
    issues.append('Compiler report does not confirm successful completion: ' + str(report.get('completion')))
pyzs = list((WORK / 'pyinstaller').rglob('PYZ-00.pyz'))
if len(pyzs) != 1:
    raise ValueError('Expected exactly one protected PYZ archive')
pyz = ZlibArchiveReader(str(pyzs[0]))
for name in pyz.toc:
    if name in expected or name == 'web' or name.startswith('web.'):
        issues.append('Business bytecode found in dependency archive: ' + name)
archive = CArchiveReader(str(BUNDLE / 'YanZhi.exe'))
entry = marshal.loads(archive.extract('protected_bootstrap'))
if set(entry.co_names) - {'protected_core', 'SystemExit', 'main'}:
    issues.append('Unexpected logic in unprotected entry loader')
if any(name in archive.toc for name in ('desktop_app', 'protected_core')):
    issues.append('Original business entry included in executable archive')
for name, record in json.loads((WORK / 'sources.json').read_text(encoding='utf-8')).items():
    if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != record['original_sha256']:
        issues.append('Source changed since staging: ' + name)
    if hashlib.sha256((WORK / 'source' / record['staged']).read_bytes()).hexdigest() != record['staged_sha256']:
        issues.append('Staged business source changed: ' + name)
frontend = json.loads((WORK / 'frontend-protection.json').read_text(encoding='utf-8'))
for name, values in frontend['files'].items():
    if hashlib.sha256((ROOT / 'web/static' / name).read_bytes()).hexdigest() != values['original_sha256']:
        issues.append('Frontend source changed since protection: ' + name)
    output = BUNDLE / '_internal/web/static' / name
    if hashlib.sha256(output.read_bytes()).hexdigest() != values['protected_sha256']:
        issues.append('Protected frontend hash mismatch: ' + name)
    if values['protected_sha256'] == values['original_sha256']:
        issues.append('Frontend not protected: ' + name)
for path in BUNDLE.rglob('*'):
    if not path.is_file():
        continue
    relative = path.relative_to(BUNDLE)
    if any(part in {'materials','evidence','history','reports','outputs','logs','config'} for part in relative.parts):
        issues.append('Library data/configuration in package: ' + str(relative))
    if path.suffix.lower() in {'.map','.pdb','.c','.cpp','.pyi'}:
        issues.append('Source/debug artifact in package: ' + str(relative))
    if path.suffix.lower() in {'.py','.pyc'} and (path.stem in expected or 'web' in relative.parts):
        issues.append('Unprotected business source in package: ' + str(relative))
    if path.suffix.lower() in {'.json','.txt','.js','.html','.exe','.pyd','.pyz'}:
        raw = path.read_bytes()
        for marker in tuple(json.loads(os.environ.get('KB_PACKAGE_PRIVATE_MARKERS', '[]'))):
            if marker.encode() in raw or marker.encode('utf-16-le') in raw:
                issues.append('Private material marker: ' + str(relative))
selftest = json.loads((WORK / 'self-test.json').read_text(encoding='utf-8'))
if not all(selftest.get(k) for k in ('ok','native_protection','fresh_blank','fulltext_search','synthetic_model_roundtrip')):
    issues.append('Protected runtime/clean library self-test failed')
result = {'ok': not issues, 'issues': sorted(set(issues)), 'native_modules': sorted(expected),
    'business_modules_in_pyz': [name for name in pyz.toc if name in expected],
    'clean_initial_state': {'profiles': selftest.get('profiles'), 'active_industry': selftest.get('active_industry'),
        'evidence_count': selftest.get('evidence_count'), 'fresh_blank': selftest.get('fresh_blank')},
    'bundled_files': sum(p.is_file() for p in BUNDLE.rglob('*'))}
(WORK / 'audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(result, ensure_ascii=False))
raise SystemExit(0 if result['ok'] else 1)
