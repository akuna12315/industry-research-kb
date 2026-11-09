"""Check the final release manifests and prove ordinary release preservation."""
import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree as ET
import pefile

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / 'work/protected'
SETUP = ROOT / 'dist/protected/YanZhi-Setup-0.1.5-Protected-x64.exe'
APP = WORK / 'dist/YanZhi/YanZhi.exe'


def level(path):
    pe = pefile.PE(str(path))
    try:
        for resource in pe.DIRECTORY_ENTRY_RESOURCE.entries:
            if resource.id == pefile.RESOURCE_TYPE['RT_MANIFEST']:
                for name in resource.directory.entries:
                    for language in name.directory.entries:
                        entry = language.data.struct
                        document = ET.fromstring(pe.get_data(entry.OffsetToData, entry.Size).rstrip(b'\0'))
                        for node in document.iter():
                            if node.tag.endswith('requestedExecutionLevel'):
                                return node.attrib['level']
    finally:
        pe.close()
    raise ValueError('No execution manifest: ' + str(path))


ordinary = ROOT / 'dist/YanZhi-Setup-0.1.5-x64.exe'
ordinary_hash = hashlib.sha256(ordinary.read_bytes()).hexdigest()
setup_hash = hashlib.sha256(SETUP.read_bytes()).hexdigest()
runtime = json.loads((WORK / 'self-test.json').read_text(encoding='utf-8'))
audit = json.loads((WORK / 'audit.json').read_text(encoding='utf-8'))
tests = json.loads((WORK / 'native-tests-result.json').read_text(encoding='utf-8'))
result = {'setup_level': level(SETUP), 'app_level': level(APP),
    'protected_sha256': setup_hash, 'ordinary_sha256': ordinary_hash,
    'ordinary_unchanged': ordinary_hash == 'be56172fc036edb2a7ca9f654e87b9de5474831dca8cfb2284c22d3fa93f470f',
    'native_regression': tests, 'privacy_audit_ok': audit['ok'],
    'clean_initial_state': audit['clean_initial_state'], 'runtime_self_test_ok': runtime['ok']}
result['ok'] = (result['setup_level'] == 'requireAdministrator' and result['app_level'] == 'asInvoker'
    and result['ordinary_unchanged'] and audit['ok'] and runtime['ok'] and runtime['fresh_blank']
    and tests['ok'] and tests['tests'] >= 271)
(WORK / 'release-verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(result, ensure_ascii=False))
raise SystemExit(0 if result['ok'] else 1)
