"""Generate license notices from the isolated build environment, never the KB data."""
import importlib.metadata as metadata
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sections = ['研知 - Third-party software notices\nPython runtime: https://docs.python.org/3/license.html\n']
python_license = Path(sys.base_prefix) / 'LICENSE.txt'
if python_license.exists():
    sections.append(python_license.read_text(encoding='utf-8', errors='replace'))
for dist in sorted(metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
    sections.append('\n' + '=' * 72 + '\n' + dist.metadata['Name'] + ' ' + dist.version)
    sections.append('Home: ' + (dist.metadata.get('Home-page') or dist.metadata.get('Project-URL') or 'See package metadata'))
    sections.append('License: ' + (dist.metadata.get('License-Expression') or dist.metadata.get('License') or 'See license text'))
    for file in dist.files or []:
        if '.dist-info/' in str(file).replace('\\', '/') and ('license' in file.name.lower() or 'copying' in file.name.lower()):
            sections.append(dist.locate_file(file).read_text(encoding='utf-8', errors='replace'))
target = root / 'work/packaging/THIRD-PARTY-NOTICES.txt'
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text('\n'.join(sections), encoding='utf-8')
