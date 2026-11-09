"""Check the executable manifests that Windows actually reads, without launching UAC."""
import hashlib
import json
from pathlib import Path
from xml.etree import ElementTree as ET

import pefile

root = Path(__file__).resolve().parents[1]


def level(path):
    pe = pefile.PE(str(path))
    try:
        for resource in pe.DIRECTORY_ENTRY_RESOURCE.entries:
            if resource.id != pefile.RESOURCE_TYPE['RT_MANIFEST']:
                continue
            for name in resource.directory.entries:
                for lang in name.directory.entries:
                    data = lang.data.struct
                    xml = ET.fromstring(pe.get_data(data.OffsetToData, data.Size))
                    for node in xml.iter():
                        if node.tag.rsplit('}', 1)[-1] == 'requestedExecutionLevel':
                            return node.attrib['level']
    finally:
        pe.close()
    raise ValueError('Missing execution-level manifest: ' + str(path))


setup = root / 'dist/YanZhi-Setup-0.1.5-x64.exe'
app = root / 'work/packaging/dist/YanZhi/YanZhi.exe'
inner = root / 'work/packaging/inner-setup/YanZhi-Setup-0.1.5-x64.exe'
result = {'setup_level': level(setup), 'inner_setup_level': level(inner), 'app_level': level(app),
          'setup_sha256': hashlib.sha256(setup.read_bytes()).hexdigest(),
          'admin_install_configured': 'PrivilegesRequired=admin' in (root / 'packaging/installer.iss').read_text(encoding='utf-8'),
          'automatic_elevated_app_launch': '[Run]' in (root / 'packaging/installer.iss').read_text(encoding='utf-8')}
result['ok'] = (result['setup_level'] == 'requireAdministrator' and result['app_level'] == 'asInvoker'
                and result['admin_install_configured'] and not result['automatic_elevated_app_launch'])
(root / 'work/packaging/setup-015-privileges.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(result))
raise SystemExit(0 if result['ok'] else 1)
