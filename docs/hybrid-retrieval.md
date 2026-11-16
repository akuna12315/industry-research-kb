# 本机混合检索

2026-11-11。资料原件和已有1200字符/180字符重叠的全文块继续沿用。新增语义编码和专用重排不生成事实证据，不改变资料加入检索、核验或行业采集状态。

## 流程

1. 只枚举当前行业（或用户明确选择的全部行业）、指定资料范围内已加入检索且非restricted的资料。
2. SQLite FTS5/BM25与本机Embedding分别召回最多40块。各行业BM25统计不同，跨行业按排名合并；两路使用RRF融合，不混加原始分数。
3. 默认保留最多48个候选（可配置），专用本机Reranker读取完整块并评分，选择最多6块。取消此前聊天模型读取320字符预览再返回编号的步骤；问答模型配置不变。
4. 选中块在原存储块内扩展至最多2200字符，为附近限定条件提供上下文。逐字片段、字符范围和版本指纹重新绑定，不能拼造原文或声称已保全跨块表头。
5. 原回答链路读取选中原文并校验引用。检索只代表相关性，未核验资料仍未核验。

向量以float32保存到各行业已有`data/material-chunks.sqlite3`的派生表。绑定编码模型、权重版本、维度、查询指令和原文指纹；重建分块同步清理旧向量，整份资料的向量在一个事务内提交。模型改变或原文换版后重新编码，撤权立即排除，不删除原件或事实历史。

当前使用SQLite缓存与精确余弦扫描，不要求FAISS、PyTorch或向量数据库；适合当前千级分块的本机库。规模增长后再用相同评测决定FAISS或其他索引，不声称当前扫描适用于百万分块。

## 配置和启动

`config/retrieval.json`与`config/agent.json`独立。未配置检索文件的安装继续使用原关键词流程。

- `enabled`：是否启用新的检索/重排；关闭可恢复旧链路。
- `mode`：`hybrid`、`dense`或`lexical`；开启后专用重排与此召回选择独立。
- `embedding`/`reranker`：回环URL、模型别名、权重文件和版本；编码另有dimension。
- `runtime`：llama-server绝对路径；可配置本项目内CUDA DLL目录、自动启动及require_gpu。
- 权重和CUDA DLL放`work/`，不加入代码、安装器或资料发布。问答密钥不用于检索；客户端禁止代理、重定向及非数字回环地址。服务只监听127.0.0.1，使用无窗口子进程，应用关闭时只停止自己启动的进程。

当前选择Qwen3-Embedding-0.6B与Qwen3-Reranker-0.6B的Q8_0量化权重。前者来自Qwen官方，后者来自llama.cpp维护者ggml-org；SHA-256和仓库revision记录在`work/retrieval-models/manifest.json`。不把量化部署结果等同于官方全精度榜单。

```powershell
# 下载并校验公开权重；--cuda为既有b10767 Windows CUDA13.3运行程序补齐匹配DLL。
.venv/Scripts/python.exe scripts/setup_retrieval.py --cuda

# 各行业范围必须显式指定；只回填派生索引，不改变资料和采集开关。
.venv/Scripts/python.exe scripts/retrieval_cli.py build --industry all
.venv/Scripts/python.exe scripts/retrieval_cli.py status --industry all

# 正常应用入口。两项本机检索服务按需启动。
./start-web.ps1
```

新增或换版资料首次语义检索时增量编码，可先执行build消除首次检索等待。关键词索引仍立即可用。模型不可用时返回`lexical_fallback`并显示说明；专用重排失败保留召回排序，不调用付费聊天模型代替。若没有任何关键词命中，不伪造语义结果。

## 对照评测

```powershell
.venv/Scripts/python.exe scripts/retrieval_cli.py evaluate --industry <id> --cases <本机题库.json> --output <结果.json>
```

题库为JSON数组，每题包含`id`、`industry`、`query`、`relevant`；每条relevant含`material_id`和逐字`quote`。执行前核对全部标签确实存在于当前版本原文。测评固定资料与分块，对比原12候选关键词、48候选关键词、纯向量、混合召回、混合加专用重排；报告Recall@6、Recall@48、MRR、nDCG@6、耗时和回退次数。测试不调用回答模型，不测问答准确率；只标一个原文位置可能漏标其他同样有效的表述。

历史真实资料标签与结果仅留本地，不随源码发布。后续已新增OCR和表格结构解析，见[图表处理说明](document-processing.md)；没有声称采用Docling或完成全库大规模向量化。

官方参考：[Qwen模型](https://github.com/QwenLM/Qwen3-Embedding)、[Qwen GGUF](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B-GGUF)、[维护者重排转换](https://huggingface.co/ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF)、[llama.cpp服务接口](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)、[RRF](https://qdrant.tech/documentation/search/hybrid-queries/)。
