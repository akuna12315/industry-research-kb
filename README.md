# 研知 · Industry Research KB

在本机运行的通用行业研究知识库：独立行业档案、可追溯证据、全文检索、时效提醒、证据简报、网页搜索、批量采集，以及基于 PydanticAI SDK 的研究助手。

公开仓库是空白框架，包含代码、测试和模板。首次启动没有真实行业、资料、信源、模型密钥或定时任务。

## 启动

需要 Python 3.12+。本项目在 Python 3.14 / Windows 上验证。

Windows PowerShell，在仓库根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\start-web.ps1
```

脚本创建仓库内 `.venv`、安装固定版本依赖，并由 `scripts/init_workspace.py` 从模板生成本地配置；已有配置不覆盖。打开 [本地页面](http://127.0.0.1:8766/)。服务仅监听本机。

也可手动启动（跨平台）：

```sh
python -m venv .venv
# Activate the virtual environment using your shell
python -m pip install -r web/requirements.txt
python scripts/init_workspace.py
python -m web.server
```

## 使用

1. 在右上角“设置 → 档案与范围”创建行业或林草地区，调整地区、分类与复核周期；新增行业的采集默认关闭。
2. 在“设置 → 信息来源”添加公开入口。preferred 优先指定来源并允许发现；only 限定启用入口及其允许目录。
3. 在“设置 → 模型与助手”填写兼容接口或厂商 API、协议、模型和密钥。默认对话只读，单行业“研究并归档”才开放网络调查和事实写入。
4. “研究”是主界面，对话直接融入整页。右侧“研究工具”按需展开，包含找资料、采集与产出；桌面占右半屏，手机占满屏，收起保留输入和任务。
5. “知识库”集中导入、搜索和查阅资料、知识证据；支持附件、分段原文、状态/时效筛选、来源链接及修订历史。
6. 直接让助手“整理某问题的发展时间线并画图”，或“绘制某指标按年的变化趋势”。图表显示在回答中，不设独立时间线入口，节点可点击追溯引用。

公开网页采集支持 HTML、文本和 RSS；本地专业资料另支持 PDF、DOCX 等附件。浏览器动态渲染、登录或付费资料不支持，不绕过访问控制。搜索/抓取失败会保留原因，不能作为“没有资料”的结论。

## 林草专业知识库

在工作台使用“新增地区”，或通过以下命令建立地区档案：

```sh
python scripts/kb.py industry add forestry-example 林草示例档案 --region 示例地区 --specialty forestry
```

地区、分类、查询和来源可以调整，不绑定某个省份。模板包含规划项目、政策标准、资源成果、社会经济指标、案例等分类，以及待核验的全国公开来源入口；不预置真实地方项目或已核验事实，也不自动启用采集。

专业资料支持原附件、元数据、适用地区、标准版本、项目阶段和历史修订，并与核验事实分别管理。普通上传每份最多10MB。大型 DOCX 可使用本地导入命令（最高100MB）：

```sh
python scripts/import_material.py ./document.docx --industry forestry-example --id example-project --region 示例地区
```

全文按段落和表格单元格建立索引，详情可检索分段原文并下载原件。扫描件和图片不做 OCR，不提供 GIS 计算或外部专业数据库接入。资料默认内部、待核验且不发送模型；模型引用必须逐项授权。级别标记不构成多人访问权限。

资料支持单份和批量通过核验，模型辅助检查后仍由用户作决定。通过后在本机按1200字符硬切分、相邻重叠180字符建立全文检索索引，不调用模型或逐批提炼事实。已通过资料提供“重建全文索引”；原件、逐字定位、版本指纹、旧证据与历史保留，旧模型提取任务不再自动续跑。图片尚未OCR，索引完成不表示外部事实已核实。

普通资料问答先在本地召回，再由模型选择命中片段并据完整片段回答；重排失败会说明使用本地相关性排序。授权、地域隔离和版本检查在检索与引用时执行。支持本机Qwen兼容接口，保持轻量思考；公开版仍从空白模型配置启动，不附带个人接口配置。

全国基础档案可设置 `specialty=forestry` 和 `library_role=national-foundation`，地区检索会引用其中的共同依据并标明所属档案。地方资料保持独立；公开版不附带实际全国或地方资料。偏好记忆需手动保存及授权，对话历史默认关闭。

## 隐私和事实边界

- API 密钥只在服务内存，或由自己的 `KB_MODEL_API_KEY` 环境变量加载；不写配置或浏览器存储。重启后页面填写的密钥需重填。
- 使用厂商模型会将本轮研究所需片段发送至用户配置的模型服务。搜索关键词发送至选定搜索服务；采集请求访问指定公开站点。
- 本地配置、行业、专业资料、附件、索引、证据、历史、日志、报告、记忆、对话及采集数据均由 `.gitignore` 排除；公开模板仅为示例。不要强制添加这些文件或将密钥写入源码。
- 公开网页采集正文只临时保留在内存，磁盘候选只保存短摘录、链接和指纹。默认最近20批可在服务中查看，重启后整理需重新读原文。
- 来源读取拒绝内网和本机地址，公网 IP 经校验后固定连接并保留域名 TLS 校验；本机 DNS 返回198.18/15映射时会通过 Cloudflare HTTPS DNS确认真实公网地址。
- 自动归档须先读对应原文且通过日期/字段/数字口径校验；单源使用 reported / planned / needs_verification，不自动宣称交叉确认。
- 事实状态与时效分开：review_due 表示待复核，current 仅未到期限。复核需真实核对，不能直接改日期消除提醒。

## 调度

本项目不自带已创建的定时任务。可自行配置 Codex 或其他外部调度，执行 `scripts/kb.py update-plan`，只处理 enabled 行业。普通软件运行和采集任务不推进全行业连续覆盖；真实调查结果通过 `finish-run` 记录，部分失败 partial、全失败 failed，完整连续 success 才推进截止。

## 命令与测试

```sh
python scripts/kb.py industry list
python scripts/kb.py industry add example 示例行业 --region 中国
python scripts/kb.py --industry all search 合同
python scripts/kb.py --industry all stale
python scripts/kb.py --industry all status
python scripts/kb.py update-plan
python -m unittest discover -s web -t . -p 'test_*.py' -v
python -m unittest discover -s scripts -p 'test_*.py' -v
node web/test_frontend.cjs
```

运行测试前需安装依赖并初始化配置。测试资料在临时目录中，不写入真实行业档案。更多操作见 [页面说明](web/README.md)。
Windows 下建议使用较短的工作区路径，避免历史记录文件名触发系统路径长度限制。

## 本机助手评测

`scripts/evaluate_assistant.py` 可顺序运行自备的100题，并持续输出CSV明细、JSON检查点和原始回答。每题独立只读会话，参考答案不发送给助手；支持停止及断点续跑。公开仓库不包含真实题库或回答。本脚本当前仅接受本机Qwen接口，防止误用外部收费模型；不修改平台模型设置。

题库在本地 `work/evaluation/questions.py` 定义 `QA = [dict(...), ...]`，每项包含 `no`（1—100）、`module`、`q`、`a`、`src`、`quote`、`note`。读取器只解析字面量数据，不执行该文件。示例字段：`dict(no=1, module='示例', q='示例问题', a='参考答案', src='章节', quote='原文摘录', note='说明')`。参考答案也需要核对。

```sh
python scripts/evaluate_assistant.py --industry example --material example-material
python scripts/evaluate_assistant.py --status
python scripts/evaluate_assistant.py --stop
```

服务和本机模型需先启动；重复启动会被锁阻止。系统失败与回答错误分别记录，数字出现及定位有效仅为辅助诊断，不能当作准确率。JSON与CSV保存前强制落盘，仍需自行保障机器和存储的稳定运行。

## 依赖

使用 [PydanticAI](https://github.com/pydantic/pydantic-ai)、FastAPI、httpx、BeautifulSoup 与 [DDGS](https://github.com/deedy5/ddgs)。依赖版本见 `web/requirements.txt`，第三方依赖遵守其各自许可证。
