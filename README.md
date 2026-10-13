# 研知 · Industry Research KB

在本机运行的通用行业研究知识库：独立行业档案、可追溯证据、全文检索、时效提醒、证据简报、网页搜索、批量采集，以及基于 PydanticAI SDK 的研究助手。

公开仓库是空白框架，包含代码、测试和模板。首次启动没有真实行业、资料、信源、模型密钥或定时任务。

## 启动

需要 Python 3.12+。本项目在 Python 3.14 / Windows 上验证。

Windows PowerShell，在仓库根目录运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\start-web.ps1
```

脚本创建仓库内 `.venv`、安装固定版本依赖，并由 `scripts/init_workspace.py` 从模板生成本地配置；已有配置不覆盖。打开 [本地页面](http://127.0.0.1:8765/)。服务仅监听本机。

也可手动启动（跨平台）：

```sh
python -m venv .venv
# Activate the virtual environment using your shell
python -m pip install -r web/requirements.txt
python scripts/init_workspace.py
python -m web.server
```

## 使用

1. 在“行业与范围”创建行业，调整地区、分类与复核周期；新增行业的自动采集默认关闭。
2. 在“信息来源”添加公开入口。preferred 优先指定来源并允许发现；only 限定启用入口及其允许目录。
3. “网页搜索”默认用免密钥 DDGS/Bing，可切换 DuckDuckGo、Brave，或接入自有 SearXNG JSON API。搜索摘要只作线索。
4. 选中搜索结果送入“批量采集”，或输入多个入口。支持 HTML / 文本 / RSS、同站链接、1至50页、深度0至3、限速、robots.txt、停止、URL和正文指纹去重、增量识别及失败记录。
5. 在“Agent 设置”填写自己的 OpenAI 兼容接口或厂商 API 地址、协议、模型和密钥。启用单行业“研究并归档”，可以通过对话搜索、读取原文和分类归档；采集完成后也可点击“Agent整理归档”。
6. 证据库支持中文检索、分类/状态/时效筛选、原始链接、核验日期与修订历史。“报告与记录”可生成和下载本地 Markdown 简报。

PDF、浏览器动态渲染、登录或付费资料目前不支持。不绕过访问控制。搜索/抓取失败会保留原因，不能作为“没有资料”的结论。

## 隐私和事实边界

- API 密钥只在服务内存，或由自己的 `KB_MODEL_API_KEY` 环境变量加载；不写配置或浏览器存储。重启后页面填写的密钥需重填。
- 使用厂商模型会将本轮研究所需片段发送至用户配置的模型服务。搜索关键词发送至选定搜索服务；采集请求访问指定公开站点。
- 本地配置、行业、证据、历史、日志、报告、对话及采集数据均由 `.gitignore` 排除；公开模板仅为示例。不要强制添加这些文件或将密钥写入源码。
- 正文只临时保留在内存，磁盘候选只保存短摘录、链接和指纹。默认最近20批可在服务中查看，重启后整理需重新读原文。
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
python -m unittest web.test_web web.test_network -v
python -m unittest discover -s scripts -p 'test_*.py' -v
```

运行测试前需安装依赖并初始化配置。原始检查共62项，测试资料在临时目录中，不写入真实行业档案。更多操作见 [页面说明](web/README.md)。

## 依赖

使用 [PydanticAI](https://github.com/pydantic/pydantic-ai)、FastAPI、httpx、BeautifulSoup 与 [DDGS](https://github.com/deedy5/ddgs)。依赖版本见 `web/requirements.txt`，第三方依赖遵守其各自许可证。
