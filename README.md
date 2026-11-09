# 研知 · Industry Research KB

在本机运行的通用知识库：独立行业档案、资料全文检索、可追溯引用、研究助手，以及按需生成的可视化研究页面。

公开仓库包含源码、测试、通用模板和构建脚本；首次启动没有实际行业、资料、来源、模型配置、密钥或定时任务。

## 启动

需要 Python 3.12+；当前源码在 Windows / Python 3.14 上验证。Windows PowerShell 在仓库根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\start-web.ps1
```

脚本创建 `.venv`、安装固定版本依赖，从 `config/*.example.json` 初始化本地配置；已有配置不覆盖。打开 [本地页面](http://127.0.0.1:8766/)。服务只监听本机。

也可手动启动：

```sh
python -m venv .venv
# Activate the virtual environment using your shell
python -m pip install -r web/requirements.txt
python scripts/init_workspace.py
python -m web.server
```

Windows 建议使用较短的工作区路径，避免历史记录文件名触发系统路径长度限制。

## 使用

1. 顶部“管理”创建、查找、重命名行业。选择当前行业立即保存，与采集开关独立；不自动选择首个行业。
2. 在“知识库”添加资料，导入窗口和资料列表直接切换“加入检索”。新资料默认开启，既有退出检索的选择保留；审核是独立的可选质量记录。
3. 在“设置 → 信息来源”维护公开入口。preferred 优先指定来源并允许发现；only 限定已启用入口和允许路径。
4. 在“设置 → 模型与助手”配置接口、协议、模型和密钥。支持 OpenAI Chat Completions / Responses 及兼容模型，接口须支持相应协议和工具调用。
5. 直接向助手提问。助手默认可以在当前问题范围内调查、收录未核验资料和添加 preferred 信源；明确说“不保存”“不入库”“不加入检索”或“仅回答”，本轮便不收录或扩展信源。“不添加信源”只关闭来源扩展。
6. 需要展示时，明确要求“把这份资料整理成可视化页面”，选择目标资料后生成；页面可查看、演示、编辑及管理。

删除行业会放入可恢复的回收站，原件、事实和历史保留；删除当前行业会清空选择。恢复不会自动启用采集或切换当前行业。单行业只检索所选档案，跨行业需明确选择“全部行业”。

## 全文检索与研究页面

资料加入检索后，在本机固定切分正文和原生表格并建立索引，不依赖模型或密钥，不把分块当成已核实事实。问答先本地召回，再由模型重排命中片段并引用原文；重排不可用时说明使用本地相关性排序。退出检索立即排除搜索和 Agent 引用，原件与历史保留。受限资料、版本与范围校验继续生效。

页面生成按仓库内 [research-visual-report 技能](web/skills/research-visual-report/SKILL.md) 选择视觉表达：流程、结构、概念、方案比较、表格，以及折线图、柱状图、饼图和时间轴。先确认数据的指标、单位、对象、期间、口径与分母；原文不足时保留缺口，不虚构数值或日期。

后端将引用绑定到本轮命中的原文片段。场景独立校验、有限重试；失败保留草稿和已通过场景，可恢复处理。图表不适用时可受控改为表格。只有整页通过结构、引用、数字支持及版本检查后才保存正式页面。已保存页面的查看和演示不调用模型，前端只渲染校验后的结构化数据，不执行模型生成的脚本。

支持 PDF、DOCX、TXT/MD/CSV 等资料；附件上传上限100MB，具体解析限制见 [页面说明](web/README.md)。扫描件与图片没有 OCR，公式、图像与复杂版式应核对原件。

## 隐私和事实边界

- 密钥只在服务内存或用户自己的 `KB_MODEL_API_KEY` 环境变量中，不写配置、命令参数、报告或浏览器存储。重启后页面填写的密钥需重填。
- 使用外部模型时，本轮研究所需的命中片段会发送给用户配置的模型服务；不会自动上传整个资料库。搜索服务收到查询词，公开来源收到采集请求。
- 本地配置、行业、原件、索引、证据、历史、报告、记忆、对话、评测数据和密钥由 `.gitignore` 排除。请勿强制添加这些文件。
- 自动收录须本轮读过公开原文并通过字段、日期与逐字引用校验，保存有界摘录为未核验资料；不会自行批准核验或生成 confirmed 事实。
- 审核通过、建立索引、模型回答和生成页面都不表示外部真实性已全部核实。事实状态与时效分别管理，复核必须实际打开原文。
- 网页和附件中的指令只作为资料。采集遵守 robots.txt、限速和来源边界，不绕过登录、付费墙或访问控制；失败保留原因。
- 服务仅监听127.0.0.1，写请求校验来源。Agent 不提供任意文件、Shell、购买或外部消息工具。资料级别标记不是多人访问权限系统。

## 调度与命令

不附带已创建的定时任务。新增行业采集默认关闭，切换行业不会启用调度。外部调度执行 `update-plan` 时只处理 enabled 行业；真实调查的 `finish-run` 才记录连续覆盖，partial/failed 不推进成功截止。

```sh
python scripts/kb.py industry list
python scripts/kb.py industry add example 示例行业 --region 中国
python scripts/kb.py --industry all search 合同
python scripts/kb.py --industry all stale
python scripts/kb.py --industry all status
python scripts/kb.py update-plan
python scripts/import_material.py ./document.docx --industry example --id example-material --region 中国
```

偏好记忆需手动保存与授权，对话历史默认关闭，开启后只保存新完成的问答；历史不自动全部发送模型。

## 测试与评测

安装依赖、初始化配置后运行；测试仅使用合成资料和模拟模型。

```sh
python -m unittest discover -s web -t . -p 'test_*.py' -v
python -m unittest discover -s scripts -p 'test_*.py' -v
python -m unittest discover -s packaging -p 'test_desktop.py' -v
node web/test_frontend.cjs
```

`scripts/evaluate_assistant.py` 顺序运行自备的100题，输出本地 CSV 和 JSON 检查点；参考答案不发送给助手，支持停止和恢复。题库在本地 `work/evaluation/questions.py` 定义 `QA = [dict(...), ...]`，字段为 `no`、`module`、`q`、`a`、`src`、`quote`、`note`。启动时显式传入 `--industry` 和 `--material`；当前评测程序仅允许其指定的本机 Qwen 接口。公开仓库不包含实际题库、回答或专用工作簿。成功返回、数字出现和引用定位不能当作语义准确率。

## 构建与许可证

源码按 GPL-3.0-or-later 发布，见 [LICENSE](LICENSE) 和 [开源与分发说明](OPEN_SOURCE.md)。第三方依赖保留各自许可。

Windows 桌面构建见 [构建说明](packaging/BUILD.md)，原生编译与前端混淆脚本见 [保护版构建说明](packaging/protected/BUILD.md)。本次提交提供当前源码，没有重建或发布安装包；历史二进制不代表当前源码验收结果。发布二进制前需重新构建、测试、隐私审计并提供对应源码。
