"""Run existing backend regressions against native business modules, not source copies."""
import importlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
import json
import hashlib
import faulthandler

faulthandler.enable(all_threads=True)

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / 'work/protected'
(WORK / 'native-tests-result.json').write_text(json.dumps({'ok': False, 'status': 'running'}), encoding='utf-8')
sys.path.insert(0, str(WORK / 'dist/YanZhi/_internal'))
import protected_core

with tempfile.TemporaryDirectory(dir=WORK) as temp:
    library = protected_core.initialize_library(Path(temp) / 'KnowledgeBase')
    os.environ['KB_LIBRARY_ROOT'] = str(library)
    import kb
    # The original tests read a default configuration before overriding all
    # provider values. Supply an empty synthetic library, never a developer library.
    kb.DEFAULT_ROOT = Path(library)
    names = ['kb','workspace','web', *('web.' + name for name in (
        'agent_runtime','charts','docx_index','extraction_model','extraction_source','full_extraction',
        'knowledge','material_index','network','server','service','verification','research_pages', 'visual_pages', 'page_recovery', 'model_diagnostics', 'visual_skill', 'page_scope', 'visual_supports'))]
    for name in names:
        module = importlib.import_module(name)
        if not hasattr(module, '__compiled__'):
            raise RuntimeError('Backend regression accidentally loaded unprotected source: ' + name)
    import web
    web.__path__.append(str(ROOT / 'web'))
    tests = []
    targets = sys.argv[1:] or ['web.' + path.stem for path in sorted((ROOT / 'web').glob('test_*.py'))]
    for target in targets:
        tests.append(unittest.defaultTestLoader.loadTestsFromName(target))
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(tests))
    native_files = list((WORK / 'dist/YanZhi/_internal').glob('protected_core*.pyd'))
    if len(native_files) != 1:
        raise RuntimeError('Expected one native module in tested distribution')
    (WORK / 'native-tests-result.json').write_text(json.dumps({'ok': result.wasSuccessful(),
        'status': 'finished', 'tests': result.testsRun, 'failures': len(result.failures),
        'errors': len(result.errors), 'skipped': len(result.skipped), 'python': sys.version,
        'native_sha256': hashlib.sha256(native_files[0].read_bytes()).hexdigest(),
        'exe_sha256': hashlib.sha256((WORK / 'dist/YanZhi/YanZhi.exe').read_bytes()).hexdigest()}), encoding='utf-8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
