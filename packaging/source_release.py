"""Export only editable framework source; never walk a knowledge-library root."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.1.5'
OUTPUT = ROOT / 'dist/source'
SUFFIXES = {
    'scripts': {'.py'},
    'web': {'.py', '.cjs', '.txt'},
    'web/static': {'.js', '.css', '.html'},
    'web/skills/research-visual-report': {'.md'},
    'web/skills/research-visual-report/references': {'.md'},
    'web/skills/research-visual-report/agents': {'.yaml'},
    'packaging': {'.py', '.ps1', '.cs', '.manifest', '.spec', '.iss', '.isl', '.cmd', '.txt', '.md', '.lock'},
    'packaging/protected': {'.py', '.ps1', '.cjs', '.spec', '.md', '.json', '.lock'},
}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def source_files():
    files = {name: (ROOT / name).read_bytes() for name in
             ('LICENSE', 'OPEN_SOURCE.md', 'start-web.ps1')}
    for folder, suffixes in SUFFIXES.items():
        # Deliberately no rglob: caches, output, data and nested directories are excluded.
        for path in sorted((ROOT / folder).iterdir()):
            if path.is_file() and not path.is_symlink() and path.suffix in suffixes:
                if folder == 'web' and path.name != 'requirements.txt' and path.suffix == '.txt':
                    continue
                files[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    files['README.md'] = b'''# YanZhi 0.1.5 source

Original program: GPL-3.0-or-later. See LICENSE and OPEN_SOURCE.md.
No knowledge library, industry registry, user material or saved model config is included.

Windows / Python: install web/requirements.txt in a virtual environment.
To start with a separate empty library, run:
python packaging/desktop_app.py --library ./KnowledgeBase

Ordinary Windows build: packaging/BUILD.md.
Native/obfuscated build: packaging/protected/BUILD.md.
The editable originals and the protected build transformations are included.
Third-party dependencies retain their original licenses. This archive contains project
source, not downloaded dependency source; obtain and distribute the corresponding
non-system dependency sources as described in OPEN_SOURCE.md before binary release.
RELEASE.json binds this snapshot to any locally verified existing installers.
'''
    # A public overview must not reproduce the development README's private-library history.
    files['web/README.md'] = b'# Local web application\n\nSee ../README.md and ../OPEN_SOURCE.md.\n'
    license_text = files['LICENSE'].decode('utf-8')
    if 'Version 3, 29 June 2007' not in license_text or 'END OF TERMS AND CONDITIONS' not in license_text:
        raise ValueError('Missing or incomplete GPLv3 license')
    return files


def main():
    files = source_files()
    matched_protected_source = False
    staged_manifest = ROOT / 'work/protected/sources.json'
    if staged_manifest.exists():
        for name, entry in json.loads(staged_manifest.read_text(encoding='utf-8')).items():
            if name not in files or sha256(files[name]) != entry['original_sha256']:
                raise ValueError('Protected release source no longer matches: ' + name)
        frontend = json.loads((ROOT / 'work/protected/frontend-protection.json').read_text(encoding='utf-8'))
        for name, entry in frontend['files'].items():
            if sha256(files['web/static/' + name]) != entry['original_sha256']:
                raise ValueError('Protected release frontend no longer matches: ' + name)
        matched_protected_source = True
    binaries = []
    for name in ('YanZhi-Setup-0.1.5-x64.exe', 'protected/YanZhi-Setup-0.1.5-Protected-x64.exe'):
        path = ROOT / 'dist' / name
        if path.exists():
            binaries.append({'file': name, 'sha256': sha256(path.read_bytes()), 'bytes': path.stat().st_size})
    files['RELEASE.json'] = json.dumps({
        'version': VERSION, 'license': 'GPL-3.0-or-later',
        'license_declaration_date': '2026-11-05', 'binaries': binaries,
        'protected_originals_match_build_manifest': matched_protected_source,
        'dependency_sources_included': False,
        'scope': 'Editable project source; third-party source distribution must be completed separately.',
    }, ensure_ascii=False, indent=2).encode('utf-8')
    manifest = {name: {'bytes': len(data), 'sha256': sha256(data)} for name, data in sorted(files.items())}
    files['MANIFEST.json'] = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    target = OUTPUT / ('YanZhi-' + VERSION + '-source.zip')
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            entry = zipfile.ZipInfo('YanZhi-' + VERSION + '/' + name, date_time=(2026, 11, 5, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, data)
    # Read back actual ZIP bytes, validate member hashes and CRCs.
    with zipfile.ZipFile(target) as archive:
        if archive.testzip() is not None:
            raise ValueError('ZIP CRC verification failed')
        for name, record in manifest.items():
            if sha256(archive.read('YanZhi-' + VERSION + '/' + name)) != record['sha256']:
                raise ValueError('Archive content mismatch: ' + name)
    (OUTPUT / 'SHA256SUMS.txt').write_text(sha256(target.read_bytes()) + '  ' + target.name + '\n', encoding='ascii')
    report = {'ok': True, 'files': len(files), 'bytes': target.stat().st_size,
              'archive': target.name, 'sha256': sha256(target.read_bytes()),
              'dependency_sources_included': False,
              'protected_originals_match_build_manifest': matched_protected_source}
    (OUTPUT / 'source-audit.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
