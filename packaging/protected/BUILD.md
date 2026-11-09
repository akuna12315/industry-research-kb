# 额外代码保护版

本版原创程序也采用 GPL-3.0-or-later。原始 Python 和 JavaScript 以及本目录的构建、兼容改写、混淆和安装脚本均须作为对应源码提供；原生编译和混淆不取消 GPL 权利。参见根目录 `OPEN_SOURCE.md`。禁止只以“加密版”为由拒绝向接收者提供源码。

此分支以0.1.5业务源码为输入，不覆盖普通版安装包或项目业务源码。后端17个业务模块经Nuitka原生编译，普通PyInstaller依赖归档中剔除这些业务模块；只保留极小的入口加载器和公开第三方依赖。前端两个JavaScript文件经混淆处理，不附带source map。

这是代码加固，不是资料数据库加密，也不是不可逆向保证。HTML、CSS、接口和运行中的前端仍可查看，原生机器码也可被分析。保护范围不能作为防复制授权系统使用。

## 构建

保护工具环境使用本机已有的Python 3.11.16创建`work/protected-tools/venv311`，沿用`packaging/requirements-build.lock`的业务依赖，另安装Nuitka 4.3.1、ordered-set、zstandard、setuptools和wheel。前端工具安装到`work/protected-tools/frontend`，版本JavaScript Obfuscator 5.5.1（`--ignore-scripts`），完整npm锁文件在该工具目录。需要Visual Studio 2022的MSVC工具链、Inno Setup 6及Windows内置.NET Framework编译器。

源码包另保留 `requirements-protected.lock` 以及 `frontend-package.json` / `frontend-package-lock.json`。新环境先用 Python 3.11 创建上述虚拟环境并安装该 Python 锁文件；将两份前端文件分别复制为 `work/protected-tools/frontend/package.json` 和 `package-lock.json`，在该工具目录执行 `npm ci --ignore-scripts`。普通版构建会生成第三方声明，保护版随后在此基础上追加工具声明。

```powershell
powershell -ExecutionPolicy Bypass -File packaging/protected/build.ps1
$env:TEMP = Join-Path (Get-Location).Path 'work/protected-tools/temp'
$env:TMP = $env:TEMP
& work/protected-tools/venv311/Scripts/python.exe packaging/protected/test_native.py
powershell -ExecutionPolicy Bypass -File packaging/protected/assemble.ps1
& work/protected-tools/venv311/Scripts/python.exe packaging/protected/verify_release.py
```

`build.ps1 -NativeOnly`仅重新编译原生模块；不会组装运行目录。手动更新运行目录中的原生模块后，必须重新执行冻结EXE自检、原生后端回归及审计。正式组装前要求全部271项原生后端回归通过，并绑定被测试EXE和原生模块的SHA256，防止拿旧结果放行新文件。

## 兼容和验证

构建的TEMP/TMP与Nuitka缓存限于工作区，不修改系统环境变量或目录权限。保护编译过程中发现采集入口生成器在拒绝非法编码路径时发生访问冲突；仅在保护版暂存源码中将该处规范化去重和边界检查改写为等价的显式循环。原始源码不变，URL校验、来源限制、顺序及去重语义保留，由完整回归验证。

资料和模型授权规则不变；不附带用户库、行业注册表、已有来源、真实资料、附件、索引、报告、日志、偏好、对话、模型配置或密钥。新空库必须是0行业、无默认行业、空模型配置。升级或同机运行仍会复用用户自己原有的库，不擅自删除旧资料。

输出仅在全部检查通过后进入`dist/protected/`。失败候选留在`work/protected/`，不得作为发布安装包。程序自检、原生回归和PE权限检查不等于真实管理员安装/卸载验收，不替代人工实际模型和联网研究验收。

技术来源：[Nuitka官方手册](https://nuitka.net/user-documentation/user-manual.html)、[JavaScript Obfuscator官方项目](https://github.com/javascript-obfuscator/javascript-obfuscator)。
