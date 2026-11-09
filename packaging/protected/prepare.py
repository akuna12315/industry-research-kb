"""Allowlisted protected build staging; never copy a live knowledge library."""
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / 'work/protected'
STAGE = WORK / 'source'
MODULES = ('__init__', 'agent_runtime', 'charts', 'docx_index', 'extraction_model', 'extraction_source',
           'full_extraction', 'knowledge', 'material_index', 'network', 'server', 'service', 'verification',
           'research_pages', 'visual_pages', 'page_recovery', 'model_diagnostics', 'visual_skill', 'page_scope', 'visual_supports')
SOURCES = [*(f'web/{name}.py' for name in MODULES), 'scripts/kb.py', 'scripts/workspace.py',
           'packaging/desktop_app.py']


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    allowed = {*(name for name in SOURCES if name != 'packaging/desktop_app.py'), 'protected_core.py',
               'README.txt', 'THIRD-PARTY-NOTICES.txt'}
    allowed.update('web/static/' + p.name for p in (ROOT / 'web/static').iterdir() if p.is_file())
    for path in STAGE.rglob('*'):
        if path.is_file() and path.relative_to(STAGE).as_posix() not in allowed:
            raise ValueError('Unexpected staging file: ' + str(path))
    manifest = {}
    for name in SOURCES:
        src = ROOT / name
        target = STAGE / ('protected_core.py' if name == 'packaging/desktop_app.py' else name)
        target.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_text(encoding='utf-8')
        if name == 'web/network.py':
            # Keep validation semantics; avoid a native-generator exception-unwind
            # crash observed with canonical_url rejecting an encoded traversal.
            before = "        urls = list(dict.fromkeys(canonical_url(u) for u in urls))\n        if any(not allowed_source(u, catalog) for u in urls):\n            raise ValueError('采集入口超出限定来源目录')"
            after = "        _protected_seeds = {}\n        for _protected_url in urls:\n            _protected_seeds[canonical_url(_protected_url)] = None\n        urls = list(_protected_seeds)\n        for _protected_url in urls:\n            if not allowed_source(_protected_url, catalog):\n                raise ValueError('采集入口超出限定来源目录')"
            if before not in data:
                raise ValueError('Network compiler compatibility patch no longer matches')
            data = data.replace(before, after, 1)
        if name == 'packaging/desktop_app.py':
            data = data.replace("VERSION = '0.1.5'", "VERSION = '0.1.5-protected'")
            data = data.replace('BASE = Path(__file__).resolve().parents[1]',
                'sys.frozen = True\nBASE = Path(__file__).resolve().parent')
            marker = "            if not all(result[k] for k in ("
            native_check = '''            protected_functions = [kb.validate, ws.registry, MaterialIndex.search]
            result['backend_stub_sizes'] = [len(f.__code__.co_code) for f in protected_functions]
            result['native_protection'] = all(type(f).__name__ == 'compiled_function' for f in protected_functions)
            if not result['native_protection']:
                raise RuntimeError('Protected business functions must be native compiled')
'''
            data = data.replace(marker, native_check + marker)
        target.write_text(data, encoding='utf-8')
        manifest[name] = {'original_sha256': hashlib.sha256(src.read_bytes()).hexdigest(),
                          'staged_sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
                          'staged': target.relative_to(STAGE).as_posix()}
    static = STAGE / 'web/static'
    static.mkdir(parents=True, exist_ok=True)
    for path in (ROOT / 'web/static').iterdir():
        if path.is_file():
            shutil.copy2(path, static / path.name)
    readme = (ROOT / 'packaging/README.txt').read_text(encoding='utf-8')
    readme = readme.replace('0.1.5（Windows x64）', '0.1.5 代码保护版（Windows x64）')
    readme = readme.replace('YanZhi-Setup-0.1.5-x64.exe', 'YanZhi-Setup-0.1.5-Protected-x64.exe')
    readme += '''
代码保护范围
本版通过Nuitka原生编译后端业务模块，使用JavaScript Obfuscator混淆前端脚本；不提供可直接提取的业务Python字节码和JS源码映射。
这是提高代码提取与分析门槛的加固版本，不能保证绝对不可逆向。浏览器执行前端代码，HTML、CSS和接口信息仍可查看。
代码保护不等于资料或数据库加密；本版没有新增资料静态加密、密码锁或授权服务器。
与普通版功能、资料格式和模型授权规则一致，使用同一知识库目录。请关闭旧启动器后覆盖安装，不要让两版同时打开同一份库。
官方技术说明：https://nuitka.net/user-documentation/user-manual.html
前端保护工具：https://github.com/javascript-obfuscator/javascript-obfuscator
'''
    (STAGE / 'README.txt').write_text(readme, encoding='utf-8')
    notices = (ROOT / 'work/packaging/THIRD-PARTY-NOTICES.txt').read_text(encoding='utf-8')
    nuitka = metadata.distribution('nuitka')
    notices += '\n\nNuitka ' + nuitka.version + '\nhttps://nuitka.net/\n'
    for file in nuitka.files or []:
        if '.dist-info/' in str(file).replace('\\', '/') and 'license' in file.name.lower():
            path = nuitka.locate_file(file)
            if path.is_file():
                notices += path.read_text(encoding='utf-8', errors='replace') + '\n'
    js_root = ROOT / 'work/protected-tools/frontend/node_modules/javascript-obfuscator'
    for path in js_root.glob('LICENSE*'):
        if path.is_file():
            notices += '\nJavaScript Obfuscator\n' + path.read_text(encoding='utf-8')
    (STAGE / 'THIRD-PARTY-NOTICES.txt').write_text(notices, encoding='utf-8')
    (WORK / 'sources.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    iss = (ROOT / 'packaging/installer.iss').read_text(encoding='utf-8')
    iss = iss.replace('OutputBaseFilename=YanZhi-Setup-0.1.5-x64',
                      'OutputBaseFilename=YanZhi-Setup-0.1.5-Protected-x64')
    iss = iss.replace('AppName=研知 · 行业知识库', 'AppName=研知 · 行业知识库（代码保护版）')
    (WORK / 'installer.iss').write_text(iss, encoding='utf-8-sig')
    shutil.copy2(STAGE / 'README.txt', WORK / 'README.txt')
    for name in ('uninstall.cmd', 'ChineseSimplified.isl'):
        shutil.copy2(ROOT / 'packaging' / name, WORK / name)
    harness = (ROOT / 'web/test_frontend.cjs').read_text(encoding='utf-8')
    harness = harness.replace("path.join(__dirname, 'static',", 'path.join(' + json.dumps(str(static)) + ',')
    (WORK / 'frontend.test.cjs').write_text(harness, encoding='utf-8')
    (WORK / 'bootstrap.py').write_text('import protected_core\nraise SystemExit(protected_core.main())\n', encoding='utf-8')
    print(json.dumps({'source_files': len(manifest), 'stage': str(STAGE)}))


if __name__ == '__main__':
    main()
