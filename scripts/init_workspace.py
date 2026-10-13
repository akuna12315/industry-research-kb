"""Initialize local configuration from public templates without overwriting existing data."""
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]

def initialize(root=ROOT):
    root = Path(root)
    for name in ('data', 'evidence', 'history', 'industries', 'topics', 'reports', 'work', 'logs'):
        (root / name).mkdir(parents=True, exist_ok=True)
    created = []
    for source in sorted((root / 'config').glob('*.example.json')):
        target = source.with_name(source.name.replace('.example.json', '.json'))
        try:
            with target.open('xb') as handle:
                handle.write(source.read_bytes())
        except FileExistsError:
            continue
        created.append(target.relative_to(root).as_posix())
    return created

if __name__ == '__main__':
    for name in initialize():
        print('Created:', name)
