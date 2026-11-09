"""Load the shipped skill, never instructions from the user library."""
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent / 'skills' / 'research-visual-report'


def visual_skill_instructions():
    entry = (SKILL_ROOT / 'SKILL.md').read_text(encoding='utf-8')
    if entry.startswith('---\n'):
        entry = entry.split('---\n', 2)[-1]
    return entry + '\n\n' + '\n\n'.join((SKILL_ROOT / 'references' / name).read_text(encoding='utf-8')
        for name in ('chart-design.md', 'knowledge-base.md'))
