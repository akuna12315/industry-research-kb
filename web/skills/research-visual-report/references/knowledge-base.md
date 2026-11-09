# 研知知识库执行约定

本约定针对内置PydanticAI Agent。运行代码中的schema和校验器是最终接口，文档示例不作为真实证据。

## 流程与结构

仅在明确的页面生成/修改任务中使用plan_visual_page→add_visual_scene→finish_visual_page；普通单图请求使用draw_research_chart，普通表格回答无需创建页面。

plan.scenes中的id、kind、title必须在add时保持相同。只设一个primary主视觉，其余emphasis=normal。先识别数据条件，再决定类型；不要因为后来想画柱状图，往已锁定的comparison场景里塞chart。

用户选定的page_materials是本轮资料边界；“我的毕设/这份资料”对象不明确时先通过页面选择，不根据当前行业猜测。仅引用所选资料，不能把检索到的其他项目代替目标文档。

检索结果的visual_supports提供后端生成的原文摘录编号。优先在节点、关系、表格单元格、图表点填写support_ref，省略quote和references；后端绑定逐字原话与来源。numbers是该摘录中的原始数字，不含段落定位标记，不代表这些数字可任意互换。选择对应行列/条件的摘录，不改写长引文。没有可用编号时仍可提供references和连续逐字quote。

- flow/architecture/concept：nodes与edges表达对象和关系；不使用cells。
- comparison：2至4个选项nodes，dimensions为共同维度，cells覆盖每个node×dimension且不重复。用于可切换的方案说明。
- table：nodes作行（最多12行），dimensions作列（最多6列，列标签写清单位），cells恰好覆盖所有行列组合；不携带edges或chart。数值须出现在对应逐字quote中。
- chart：scene只携带chart，不携带nodes/edges/cells/dimensions；chart.kind选择line/bar/pie/timeline。

来源记载与计划的节点、关系、表格单元格带references（本轮工具返回的chart_ref）及连续逐字quote。自己归纳的组织关系明确标inference，不借此把无依据数值变成推断结果。

## 数值图表

所有数值图提供metric、unit、geography（或实验对象范围）、scope、basis与nature。nature=reported表示原文报告值，不是独立实测；actual与planned沿用原有含义。

- 时间折线：axis=time；每个item提供真实date。不用上传/核验日期替代实验时间。
- 类别柱状图：axis=category；item.label为算法/类别，date可为null，period描述共同实验或统计期间。不为方法A/B虚构日期。
- 连续数值折线：axis=number；提供x_metric、x_unit与真实item.x，period描述实验范围。每个x和value均须有原文支持。
- 饼图：axis=category；whole说明整体，total为组成总量。非百分比total需要原文支持，百分比total为100。item.value是原始数量或原百分比，渲染器计算份额。不混放缺失、负数或未覆盖类别。
- 每个点提供references和原文quote，quote中有对应类别/期间、指标、单位、scope、basis和数值；上下文不足时补查原文或改用表格。不为通过校验补写原话。
- 引用位于chart.items[i].references；chart根节点没有references。数据、口径、日期错误查看fields中指向的chart字段或具体items索引，并用repair_sources核对原文。data错误不能当成单纯换引用编号。

数值来自已登记metrics时须匹配口径和期间；原文摘取按逐字引用与数值支持校验。缺测用null，不用0代替。

## 隔离与交付

每种场景方案最多3次失败；按fields/hint和repair_sources修复，不提交相同错误参数。已有失败的chart可调用revise_visual_scene一次改为table，再构建完整行列与引用；数值仍逐字核对，不能用推断绕过数据校验。已通过的场景保持原样。替代方案仍失败时保留草稿，不无限重试。SDK恢复最多2轮，与当前请求/时间预算共用；限流、访问问题、来源版本或授权变化时暂停。

生成页面时统计图通过add_visual_scene提交，不调用只用于聊天单图的draw_research_chart。图表无法满足同口径、分母或日期要求时，优先选择精确表格；不要为了图形凑日期、分母或改写原文。

完整校验后才能保存正式版本。打开、切换和演示已保存页面不请求模型。连接以当前页面配置为准，不自动换厂商、型号或协议；维护重启会清空内存密钥，须提前告知。
