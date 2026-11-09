# Windows 安装包构建

原创程序按 GPL-3.0-or-later 发布，见根目录 `LICENSE` 和 `OPEN_SOURCE.md`。安装脚本将许可证和源码说明安装到程序目录。发布前另运行 `python packaging/source_release.py`，把对应源码和第三方源码获取材料放在安装器的同一下载位置；代码保护版同样适用。源码导出不下载第三方依赖源码，不能将其结果单独当成整个二进制所有依赖的完整源码。

当前安装器和应用版本为0.1.5，默认管理员安装：外层EXE的实际Windows清单为requireAdministrator，内层Inno设置PrivilegesRequired=admin。安装完成后由用户从桌面/开始菜单启动普通权限的应用，不从已提权的安装器自动启动研究服务。`check_setup_privileges.py`读取最终EXE的PE资源核对权限声明；该检查不触发UAC，也不等同于管理员安装验收。完整安装验收脚本须从用户已批准的管理员测试终端运行，禁止自动绕过UAC。0.1.1本地安装回退是历史行为，当前正式安装要求正常写入系统卸载注册和快捷方式。

0.1.5将知识库统一为资料检索入口，移除独立知识证据页面和录入表单。新资料（含网页来源）开启“加入检索”后只建立本地索引，审核为独立可选质量记录，搜索结果返回原文和位置。旧证据与历史保留；模型授权和版本校验不放宽。仍包含页面附件上传100MB改动，前端校验、文件读取和请求体上限已同步；新建资料与已有资料更换附件均适用。升级保留用户库，首次配置仍为空白。

在 Windows x64、Python 3.14 环境运行：

```powershell
powershell -ExecutionPolicy Bypass -File packaging/build.ps1
```

需要本机 Inno Setup 6和Windows内置.NET Framework编译器。脚本会创建隔离打包环境、按 `requirements-build.lock` 安装依赖，使用 PyInstaller 构建程序，运行冻结EXE自检和输入清单审计，再编译 Inno Setup 安装程序和独立启动层。输出 `dist/YanZhi-Setup-0.1.5-x64.exe`、使用说明和SHA256校验文件。`-Iscc` 和 `-Python` 可显式指定构建工具；`-SkipDependencies` 仅用于已安装相同依赖的构建环境。只重新构建安装器时可运行 `packaging/build-setup.ps1`。

应用复用原 FastAPI 页面和 SDK，桌面启动器通过 `create_app(root)` 指定用户资料目录。`KB_LIBRARY_ROOT` 用于模块初始应用创建时指定数据根；未设置时保留源码版原路径。只监听127.0.0.1，保留原核验和全文索引规则。0.1.5新库为零行业、无默认行业，模型提供方、名称、接口地址和型号均为空；不预置任何行业、来源或示例数据。未配置模型时不绑定环境密钥或创建模型客户端，页面显示“未配置模型”，用户可自行保存配置。普通资料管理和全文索引不依赖模型。

升级不清理现有用户库：旧版本已生成的档案和配置也会保留，以免删除用户已导入的资料。需要空白环境时，停止服务后使用启动器“新建空白库”，仅允许空目录；原库可通过“选择知识库目录”切回。首次状态、保存首个模型、创建首个行业和现有配置原样保留均有回归检查。

只有 `web/` 业务模块、`scripts/kb.py`、`workspace.py`、静态资源及桌面入口进入程序；依赖从隔离环境收集。不能把整个工作目录、现有 `config/`、`industries/`、`data/`、`materials/`、`reports/` 或 `work/` 递归放入包。`audit_bundle.py` 检查实际Analysis清单、应用输入白名单和分发目录；报告留在 `work/packaging/bundle-audit.json`，不随包分发。

资料目录默认独立于安装目录。用户数据目录不可写时，桌面版使用安装目录内的UserData；卸载保留该目录。升级安装仅更新程序，不递归删除用户资料。已有源码服务与桌面版不要同时连接一份库；先停止原服务，避免并发建索引。

安装器由不需自解压的CLR入口启动，验证内嵌安装程序SHA256后运行。缓存优先选择用户目录内的YanZhi/InstallerTemp，失败时使用安装包旁的.yanzhi-installer-temp，仅为子进程设置TEMP/TMP。独立卸载入口同样使用可写临时目录。安装器不修改系统TEMP/TMP、目录ACL或安全设置。0.1.1曾在系统集成不可写时省略注册和快捷方式；该历史回退已由当前管理员安装取代。

自检：

```powershell
python -m unittest discover -s packaging -p test_desktop.py -v
YanZhi.exe --self-test C:\path\self-test.json
```

EXE自检先验证临时中文目录中的全空初始状态，再创建合成行业和资料，检查Tk窗口组件、HTML服务、资料保存、核验后全文索引/召回、SQLite FTS5、PDF/网页搜索依赖导入，以及用MockTransport模拟的通用模型接口。不会联系真实模型，不能代替实际模型或联网搜索验收。GUI自检创建并销毁隐藏窗口，不是人工视觉验收。

每次更改源码均用 `--clean` 清除打包缓存，避免旧模块残留。构建和测试日志留在 `work/packaging/`，正式变更说明记入 `reports/maintenance/`。

桌面版使用Windows扩展路径保存资料，同时声明longPathAware，避免在系统LongPathsEnabled未开启时因资料历史目录较深而失败。资料目录选择和显示仍使用普通路径，不修改系统长路径设置。

安装验收脚本：`powershell -ExecutionPolicy Bypass -File packaging/test_installer.ps1`。仅在未安装研知的管理员测试环境运行；默认保留真实TEMP环境，在工作目录安装、覆盖安装和卸载，并验证外部资料文件保留。结果位于 `work/packaging/installer-test-015.json`。`-IsolatedTemp`只用于显式隔离临时目录。卸载入口异步交给原生卸载器，测试等待真实卸载成功日志和程序文件消失。0.1.5尚未运行实际管理员安装/升级/卸载验收，不能把旧版结果算作本次验收。

0.1.0时期的SmokeTestOnly临时验收选项仍保留供诊断，**不等于正式安装验收通过**，不得分发临时验收包。0.1.1正式安装包已直接进行验收，不依赖该选项。

构建参考：[PyInstaller资源路径](https://pyinstaller.org/en/stable/runtime-information.html)、[Inno Setup命令行编译](https://jrsoftware.org/ishelp/topic_compilercmdline.htm)。`ChineseSimplified.isl` 来自[官方翻译目录](https://jrsoftware.org/files/istrans/)指向的[源文件](https://raw.githubusercontent.com/jrsoftware/issrc/refs/heads/main/Files/Languages/ChineseSimplified.isl)，保留译者声明。Python及依赖许可文本自动汇入安装包的第三方软件声明。

## 当前源码边界

本次公开提交未重建或发布安装包。构建脚本、版本标签和历史安装验收说明不表示当前源码已有对应二进制；发布前须重新构建、自检和审计。隐私审计可通过 KB_PACKAGE_PRIVATE_MARKERS 环境变量传入 JSON 字符串数组，在本地指定需排除的资料标识，不把真实标识写入公开源码。
