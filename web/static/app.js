'use strict';
const $ = s => document.querySelector(s);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const safeURL = value => {try{const u = new URL(value,location.origin);return ['http:','https:'].includes(u.protocol)?u.href:'#';}catch{return '#';}};
const S = {view:'evidence', industry:'all', data:null, records:[], q:'', category:'', freshness:'', status:'', sources:null, conversation:null, job:null, stream:null};
const labels = {confirmed:'已核验',reported:'来源声称',planned:'计划 / 目标',needs_verification:'待核验',superseded:'已被替代'};
const titles = {evidence:'证据库',industries:'行业与范围',sources:'信息来源',websearch:'网页搜索',crawls:'批量采集',review:'待复核',reports:'报告与记录',settings:'Agent 设置'};
let webResults=null, crawlSeeds='', crawlPoll=null, lastCrawlId=null;
let webOptions={period:'',limit:10}, crawlOptions={max_pages:10,max_depth:'1'};
const crawlLabels={running:'采集中',completed:'采集完成',partial:'部分失败',failed:'采集失败',cancelled:'已停止',interrupted:'服务重启中断',new:'新增候选',changed:'内容变化',unchanged:'内容未变化',duplicate:'重复内容'};

async function api(path, method='GET', data){
  const response = await fetch('/api'+path,{method,headers:{'Content-Type':'application/json','X-KB-Request':'1'},body:data===undefined?undefined:JSON.stringify(data)});
  const result = await response.json();
  if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'填写内容未通过校验');
  return result;
}
let toastTimer;
function toast(message){$('#toast').textContent=message;$('#toast').style.display='block';clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').style.display='none',4500);}
function handleError(error){console.error(error);toast(error.message||'操作失败，请重试');}
function chooseIndustry(){return S.industry==='all'?S.data.industries[0]?.id:S.industry;}
function industryName(id){return S.data.industries.find(i=>i.id===id)?.name||id;}
function options(items,current){return items.map(([value,label])=>`<option value="${esc(value)}" ${value===current?'selected':''}>${esc(label)}</option>`).join('');}
function badge(state,label){return `<span class="badge ${['review_due','needs_verification','planned'].includes(state)?'warn':['expired'].includes(state)?'red':state==='superseded'?'grey':''}">${esc(label)}</span>`;}
function heading(title,subtitle,buttons=''){return `<div class="page-heading"><div><div class="eyebrow">RESEARCH WORKSPACE</div><h1>${title}</h1><p>${subtitle}</p></div><div class="actions">${buttons}</div></div>`;}
function empty(title,text,button=''){return `<div class="empty"><div class="empty-icon">◫</div><strong>${title}</strong><p>${text}</p>${button}</div>`;}
function dialog(title,html){$('#dialog-title').textContent=title;$('#dialog-body').innerHTML=html;if(!$('#dialog').open)$('#dialog').showModal();}
function split(value){return value.split(/[，,\n]/).map(x=>x.trim()).filter(Boolean);}
function markdown(raw){
  let text=esc(raw);
  text=text.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,(_,label,url)=>`<a href="${esc(safeURL(url.replaceAll('&amp;','&')))}" target="_blank" rel="noopener noreferrer">${label}</a>`);
  text=text.replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>').replace(/`([^`\n]+)`/g,'<code>$1</code>');
  return text.split('\n').map(line=>/^#{1,3} /.test(line)?`<h3>${line.replace(/^#+ /,'')}</h3>`:/^[-*] /.test(line)?`<div>• ${line.slice(2)}</div>`:`<div>${line||'<br>'}</div>`).join('');
}
async function refresh(){
  S.data=await api('/state');
  if(S.industry!=='all'&&!S.data.industries.some(i=>i.id===S.industry))S.industry='all';
  $('#industry-select').innerHTML=options([['all','全部行业档案'],...S.data.industries.map(i=>[i.id,i.name])],S.industry);
  $('#nav-count').textContent=S.data.industries.reduce((n,i)=>n+i.evidence_count,0);
  $('#review-count').textContent=S.data.industries.reduce((n,i)=>n+i.review_count,0);
  $('#server-date').textContent=S.data.as_of.slice(5).replace('-','.');
  $('#model-name').textContent=S.data.agent.display_name+' / '+S.data.agent.model;
  updateChatScope();
  await render();
}
function updateChatScope(){const research=$('#research-mode').checked;$('#chat-scope').textContent=(S.industry==='all'?'全部档案':industryName(S.industry))+' · '+(research?'研究归档':'只读');}
async function render(){
  clearTimeout(crawlPoll);
  $('#breadcrumb').textContent=titles[S.view];
  document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===S.view));
  if(['evidence','review'].includes(S.view))return renderEvidence();
  if(S.view==='industries')return renderIndustries();
  if(S.view==='sources')return renderSources();
  if(S.view==='websearch')return renderWebSearch();
  if(S.view==='crawls')return renderCrawls();
  if(S.view==='reports')return renderReports();
  if(S.view==='settings')return renderSettings();
}
function networkIndustry(title){
  if(S.industry!=='all')return true;
  $('#content').innerHTML=heading(title,'先选择一个行业，搜索和采集将遵守该行业的来源边界。')+empty('选择研究行业','使用右上角“浏览范围”选择行业；也可以先新建行业。','<button class="primary" data-action="add-industry">＋ 新建行业</button>');
  return false;
}
function renderWebSearch(){
  if(!networkIndustry('网页搜索'))return;
  const c=S.data.network;
  $('#content').innerHTML=heading('网页搜索','找到线索，再打开原文。搜索摘要不会直接进入证据库。')+
    `<form id="web-search-form" class="panel"><div class="form-grid"><label class="field full">搜索关键词<input name="query" required maxlength="500" placeholder="行业、机构、政策、产品或财报…" value="${esc(webResults?.query||'')}"></label><label class="field">时间范围<select name="period">${options([['','不限时间'],['d','最近一天'],['w','最近一周'],['m','最近一月'],['y','最近一年']],webOptions.period)}</select></label><label class="field">结果数量<input type="number" name="limit" min="1" max="30" value="${webOptions.limit}"></label></div><div class="actions"><button class="primary" type="submit">搜索网页</button><span class="description">${esc(c.provider==='ddgs'?'免密钥 · DDGS / '+c.backend:'SearXNG 实例')}</span></div></form>`+
    `<div class="notice"><span>⌕</span><div>仅限指定来源时，结果会按启用来源目录过滤。引擎时间筛选需要原文日期核验；没有结果不代表没有资料。</div></div>`+
    (webResults?`<div class="section-caption"><span>${webResults.items.length} 条线索 · ${esc(webResults.backend)}</span><button class="secondary" data-action="crawl-search">采集选中网页</button></div><div class="evidence-list">${webResults.items.map((r,i)=>`<article class="evidence-card web-result"><div class="card-top"><label><input type="checkbox" name="web-pick" value="${i}"> 选择采集</label>${badge('needs_verification',r.preferred?'指定来源 · 待读原文':'搜索线索 · 待读原文')}</div><h3><a href="${esc(safeURL(r.url))}" target="_blank" rel="noopener noreferrer">${esc(r.title)} ↗</a></h3><p class="summary">${esc(r.snippet)}</p><div class="card-foot"><small class="url-text">${esc(r.url)}</small><button class="link-button" data-action="source-from-search" data-id="${i}">加入信源</button></div></article>`).join('')||empty('没有匹配线索','可修改关键词、放宽时间，或检查限定来源。')}</div>`:'');
}
async function renderCrawls(){
  if(!networkIndustry('批量采集'))return;
  const industry=S.industry;
  const jobs=await api('/crawls?'+new URLSearchParams({industry}));
  if(S.view!=='crawls'||S.industry!==industry)return;
  $('#content').innerHTML=heading('批量采集','从公开入口沿同站链接抓取，按URL和内容去重；采集结果先保留为候选。')+
    `<form id="crawl-form" class="panel"><label class="field">入口URL（每行一个）<textarea name="urls" rows="3" placeholder="留空使用本行业所有已启用信源；也可粘贴搜索选中的地址">${esc(crawlSeeds)}</textarea></label><div class="form-grid"><label class="field">最多尝试页数<input type="number" name="max_pages" min="1" max="50" value="${crawlOptions.max_pages}"></label><label class="field">跟进深度<select name="max_depth">${options([['0','0 · 只读入口页面'],['1','1 · 入口及下一层链接'],['2','2 · 两层链接'],['3','3 · 三层链接']],crawlOptions.max_depth)}</select></label></div><div class="actions"><button class="primary" type="submit">开始批量采集</button><span class="description">HTML / 文本 / RSS · 遵守 robots.txt · 同站限速</span></div></form>`+
    `<div class="notice"><span>↧</span><div>正文临时保留在内存，重启后需重新读取；PDF、登录页面及动态渲染内容目前不支持。完成后可交给 Agent 分类归档，每轮最多12条。</div></div><div id="crawl-jobs">${jobs.map(j=>crawlCard(j)).join('')||empty('尚无采集任务','填写入口或添加信源后即可开始。')}</div>`;
  if(jobs.some(j=>j.status==='running'))pollCrawls(industry);
}
function crawlCard(j){
  const count=j.counts||{};
  return `<article class="panel crawl-job" data-crawl="${esc(j.id)}"><div class="industry-title"><h2>${esc(crawlLabels[j.status]||j.status)}</h2><div class="actions">${j.status==='running'?`<button class="secondary" data-action="stop-crawl" data-id="${esc(j.id)}">停止采集</button>`:j.items.some(i=>i.state!=='failed'&&i.state!=='duplicate')?`<button class="primary" data-action="organize-crawl" data-id="${esc(j.id)}">Agent 整理归档</button>`:''}</div></div><p class="description">${esc(j.started_at)} · 已尝试 ${j.attempted}/${j.max_pages} 页 · 深度 ${j.max_depth}${j.remaining?' · 未访问队列 '+j.remaining+' 项':''}</p><div class="category-tags">${['new','changed','unchanged','duplicate','failed'].map(k=>`<span class="tag">${esc(crawlLabels[k]||'失败')} ${count[k]||0}</span>`).join('')}</div>${j.error?`<div class="form-error">${esc(j.error)}</div>`:''}<details ${j.status==='running'?'open':''}><summary>查看采集结果与失败原因</summary>${j.items.map(i=>`<div class="crawl-item"><div>${badge(i.state,i.state==='failed'?'读取失败':crawlLabels[i.state])} <a href="${esc(safeURL(i.url))}" target="_blank" rel="noopener noreferrer">${esc(i.title||i.url)} ↗</a></div><p>${esc(i.error||i.excerpt||'')}${i.truncated?'（正文截短）':''}</p><small>${esc(i.url)}</small></div>`).join('')||'<p class="description">正在读取入口…</p>'}</details></article>`;
}
function pollCrawls(industry){
  crawlPoll=setTimeout(async()=>{try{if(S.view!=='crawls'||S.industry!==industry)return;const jobs=await api('/crawls?'+new URLSearchParams({industry}));if(S.view!=='crawls'||S.industry!==industry)return;$('#crawl-jobs').innerHTML=jobs.map(crawlCard).join('');if(jobs.some(j=>j.status==='running'))pollCrawls(industry);}catch(error){handleError(error);}},2000);
}
async function renderEvidence(){
  const query=new URLSearchParams({industry:S.industry,q:S.q,category:S.category,freshness:S.freshness,status:S.status,review_only:S.view==='review'?'true':'false'});
  let records=await api('/evidence?'+query);S.records=records;
  const selected=S.data.industries.filter(i=>S.industry==='all'||i.id===S.industry);
  const total=selected.reduce((n,i)=>n+i.evidence_count,0),due=selected.reduce((n,i)=>n+i.review_count,0),enabled=selected.filter(i=>i.enabled).length;
  const cats=[...new Set([...selected.flatMap(i=>i.scope.topics||[]),...records.map(r=>r.category)])];
  const review=S.view==='review';
  $('#content').innerHTML=heading(review?'待复核资料':'证据库',review?'复核期限是调查提醒；年代久远不等于事实错误。':'把分散的信息，变成有来源、可检索的研究依据。',`<button class="secondary" data-action="report">↗ 导出简报</button><button class="primary" data-action="manual-evidence">＋ 录入证据</button>`)+
  `<div class="stats"><div class="stat"><div class="stat-label">证据总数 <span>▤</span></div><div class="stat-num">${total}<small>条</small></div><div class="stat-note">按所选行业统计</div></div><div class="stat"><div class="stat-label">需要复核 <span>◷</span></div><div class="stat-num">${due}<small>条</small></div><div class="stat-note">状态与核验日期实时计算</div></div><div class="stat"><div class="stat-label">启用自动更新 <span>↻</span></div><div class="stat-num">${enabled}<small>个行业</small></div><div class="stat-note">每天 ${esc(S.data.automation.schedule.replace('每天',''))} · 北京时间</div></div></div>`+
  (!enabled?`<div class="notice"><span>◉</span><div><strong>当前范围未启用自动采集。</strong> 可以先查阅已有档案，准备好后在“行业与范围”中开启更新。</div></div>`:'')+
  `<form id="search-form" class="searchbox"><span>⌕</span><input id="search-input" type="search" placeholder="检索事实、机构或关键词…" value="${esc(S.q)}" aria-label="检索知识库"><kbd>Enter ↵</kbd></form><div class="filter-row"><div class="tabs">${[['','全部证据'],['confirmed','已核验'],['reported','来源声称'],['planned','计划 / 目标']].map(([state,label])=>`<button class="tab ${S.status===state?'active':''}" data-status="${state}">${label}</button>`).join('')}</div><div class="filters"><select id="category-filter" aria-label="分类筛选">${options([['','全部分类'],...cats.map(c=>[c,c])],S.category)}</select><select id="freshness-filter" aria-label="时效筛选">${options([['','全部时效'],['current','未到复核期限'],['review_due','待复核'],['needs_verification','待核验'],['expired','已过有效期'],['superseded','已被替代']],S.freshness)}</select></div></div><div class="section-caption"><span>${review?'复核清单':'证据记录'} · ${records.length} 条匹配</span><span>核验日期 ↓ · 最多展示100条</span></div><div class="evidence-list">${records.length?records.map(r=>`<article class="evidence-card" data-record="${esc(r.id)}" data-industry="${esc(r.industry_id)}" tabindex="0" role="button" aria-label="查看 ${esc(r.title)}"><div class="card-top"><div class="category"><span>${esc(r.category)}</span><span>/ ${esc(industryName(r.industry_id))}</span></div>${badge(r.status,labels[r.status])}</div><h3>${esc(r.title)}</h3><p class="summary">${esc(r.summary)}</p><div class="card-foot"><div class="card-foot-left"><span>◈ ${esc(r.publisher)}</span><span>核验 ${esc(r.checked_date)}</span>${r.freshness!=='current'?badge(r.freshness,r.freshness_label):''}</div><div class="actions">${(r.tags||[]).slice(0,1).map(t=>`<span class="tag">${esc(t)}</span>`).join('')}<a href="${esc(safeURL(r.url))}" target="_blank" rel="noopener noreferrer">原始来源 ↗</a></div></div></article>`).join(''):empty(review?'没有待复核的匹配资料':'没有匹配的证据',review?'未到复核期限不代表事实持续成立。':'试试减少关键词，或先建立行业并添加公开来源。')}</div>`;
}
function renderIndustries(){
  $('#content').innerHTML=heading('行业与范围','各行业独立归档；切换当前行业不会删除原有资料。','<button class="primary" data-action="add-industry">＋ 新建行业</button>')+
  `<div class="notice"><span>◫</span><div>默认行业：<strong>${S.data.active_industry?esc(industryName(S.data.active_industry)):'暂未选择'}</strong>。浏览范围只影响本页面；采集开关决定每天更新哪些行业。</div></div>`+
  (S.data.industries.length?S.data.industries.map(i=>`<article class="industry-card"><div class="industry-title"><h2>${esc(i.name)} ${i.id===S.data.active_industry?badge('current','默认行业'):''}</h2>${badge(i.enabled?'current':'superseded',i.enabled?'自动采集已启用':'采集已暂停')}</div><div class="industry-meta">${esc(i.scope.primary_region)} · ${i.evidence_count}条证据 · 连续成功覆盖：${esc(i.update_state.covered_through||'尚无完整扫描记录')}</div><div class="category-tags">${(i.scope.topics||[]).map(c=>`<span class="tag">${esc(c)}</span>`).join('')}</div><div class="actions"><button class="secondary" data-action="browse-industry" data-id="${esc(i.id)}">查看资料</button><button class="secondary" data-action="scope" data-id="${esc(i.id)}">编辑范围与分类</button><button class="link-button" data-action="set-active" data-id="${esc(i.id)}">设为默认</button><span class="industry-meta">每日采集</span><label class="switch"><input type="checkbox" data-industry-enabled="${esc(i.id)}" ${i.enabled?'checked':''} aria-label="启用${esc(i.name)}每日采集"><span></span></label></div></article>`).join(''):empty('创建第一个研究行业','确定地区和重点问题后，再启用自动采集。'));
}
async function renderSources(){
  const id=chooseIndustry();
  if(!id){$('#content').innerHTML=heading('信息来源','管理公开信息入口与采集边界。')+empty('先创建一个行业','信息来源按行业独立管理。','<button class="primary" data-action="add-industry">新建行业</button>');return;}
  S.sources=await api('/sources/'+id);
  $('#content').innerHTML=heading('信息来源',`正在管理：${esc(industryName(id))}。入口加入时不会自动视为已经核验。`,'<button class="primary" data-action="add-source">＋ 添加来源</button>')+
  `<div class="panel"><h2>采集边界</h2><p class="description">优先使用原始公告、监管文件和正式披露。</p><label class="field">来源模式<select id="source-mode">${options([['preferred','指定来源优先，允许发现其他公开原始来源'],['only','仅使用指定来源入口及其允许目录中的原文']],S.sources.mode||'preferred')}</select></label></div><div class="panel"><h2>公开信息入口 <span class="badge grey">${S.sources.sources.length}</span></h2><p class="description">优先级1最高；暂停来源不会删除已入库证据。</p>${S.sources.sources.length?`<table class="source-table"><thead><tr><th>来源 / 地址</th><th>优先级</th><th>状态</th><th>启用</th></tr></thead><tbody>${S.sources.sources.map(source=>`<tr><td><button class="link-button" data-action="edit-source" data-id="${esc(source.id)}">${esc(source.name)}</button><small>${esc(source.url)}</small></td><td>${source.priority||3}</td><td>${badge(source.verified?'current':'needs_verification',source.verified?'有核验记录':'待核验')}</td><td><label class="switch"><input type="checkbox" data-source-enabled="${esc(source.id)}" ${source.enabled!==false?'checked':''} aria-label="启用${esc(source.name)}"><span></span></label></td></tr>`).join('')}</tbody></table>`:empty('还没有指定来源','添加官网、监管公告或财报公开入口。')}</div>`;
}
async function renderReports(){
  const reports=await api('/reports');
  $('#content').innerHTML=heading('报告与记录','证据汇编、调查记录与失败情况都可以回溯。','<button class="primary" data-action="report">＋ 生成证据简报</button>')+`<div class="notice"><span>▧</span><div>证据简报由本地资料汇编，不会自动宣称联网核验成功。Agent 调查过程会另外保留运行记录。</div></div><div class="reports-list">${reports.map(r=>`<div class="report-row"><div><h3>${esc(r.title)}</h3><small>${esc(r.path)}</small></div><div class="actions"><button class="link-button" data-action="open-doc" data-path="${esc(r.path)}">阅读 ↗</button><a class="link-button" href="/api/download?${new URLSearchParams({path:r.path})}">下载</a></div></div>`).join('')||empty('暂无报告','可以先生成一份带来源和核验日期的本地简报。')}</div>`;
}
function renderSettings(){
  const c=S.data.agent;
  $('#content').innerHTML=heading('Agent 设置','开源PydanticAI SDK · 本机接口与厂商API可自由切换。')+`<div class="panel"><h2>模型连接</h2><p class="description">使用支持工具调用的模型；API协议必须与服务端匹配。</p><form id="settings-form"><div class="form-grid"><label class="field">Provider ID<input name="provider_id" required value="${esc(c.provider_id)}"></label><label class="field">显示名称<input name="display_name" required value="${esc(c.display_name)}"></label><label class="field full">API Base URL<input name="base_url" type="url" required value="${esc(c.base_url)}" placeholder="http://127.0.0.1:8000/v1"></label><label class="field">API协议<select name="protocol">${options([['chat_completions','OpenAI Chat Completions'],['responses','OpenAI Responses']],c.protocol)}</select></label><label class="field">模型 ID<input name="model" required value="${esc(c.model)}"></label><label class="field full">API 密钥<input name="api_key" type="password" autocomplete="off" placeholder="${c.key_configured?'已有服务端密钥，留空保留':'输入密钥；本机接口可填 local'}"><small>输入后仅在服务端内存使用，不写浏览器存储或配置文件。重启时可通过 KB_MODEL_API_KEY 环境变量加载。</small></label><label class="field">每轮模型请求上限<input name="request_limit" type="number" min="1" max="30" value="${c.request_limit}"></label><label class="field">单轮超时（秒）<input name="timeout_seconds" type="number" min="30" max="900" value="${c.timeout_seconds}"></label></div><div class="provider-note">本机接口使用当前模型；厂商API会接收本次问答所用的证据片段。资料文件仍保存在本地。工具包括本地检索、网页搜索、批量采集、公开原文读取、验证入库和简报。</div><div class="actions"><button class="primary" type="submit">保存连接</button><button class="secondary" type="button" id="test-model">测试模型列表</button><a class="link-button" href="https://github.com/pydantic/pydantic-ai" target="_blank" rel="noopener noreferrer">SDK 源码 ↗</a></div><div id="connection-result" class="connection-result"></div></form></div><div class="panel"><h2>每天更新</h2><p class="description">${S.data.automation.status==='ACTIVE'?'外部调度配置：'+esc(S.data.automation.schedule)+'，北京时间。请确认对应调度已真实创建。':'尚未配置定时任务。可自行配置外部调度，按启用行业运行调查；行业开关不会安装定时任务。'} 页面Agent和批量采集按需启动。</p><div class="actions">${badge('current',S.data.automation.status)}<button class="link-button" data-action="plan">查看当前调查计划 ↗</button></div></div>`;
  const n=S.data.network;
  $('#content').innerHTML+=`<div class="panel"><h2>网页搜索后端</h2><p class="description">默认免密钥DDGS。也可接入支持JSON搜索的自有SearXNG实例；引擎不可用时可以切换，不会伪造结果。</p><form id="network-settings-form"><div class="form-grid"><label class="field">搜索服务<select name="provider">${options([['ddgs','DDGS · 无需API密钥'],['searxng','SearXNG · 自有实例']],n.provider)}</select></label><label class="field">DDGS搜索引擎<select name="backend">${options([['bing','Bing'],['duckduckgo','DuckDuckGo'],['brave','Brave']],n.backend)}</select></label><label class="field full">SearXNG Base URL<input name="searxng_url" type="url" value="${esc(n.searxng_url)}" placeholder="http://127.0.0.1:8080"></label><label class="field">搜索地区<select name="region">${options([['cn-zh','中国 · 中文'],['us-en','美国 · 英文'],['wt-wt','全球']],n.region)}</select></label></div><button class="primary" type="submit">保存搜索后端</button></form></div>`;
}
function addIndustry(){dialog('新建研究行业',`<form id="industry-form"><div class="form-grid"><label class="field">行业名称<input name="name" placeholder="例如：半导体" required></label><label class="field">行业编号<input name="id" placeholder="例如：semiconductor" required pattern="[a-z0-9][a-z0-9_-]{2,79}"><small>小写英文、数字和连字符，至少3位。</small></label><label class="field full">主要地区<input name="region" value="中国" required></label><label class="field full">分类（逗号或换行分隔）<textarea name="categories" placeholder="政策与监管，企业与竞争，产品与技术，供应链，市场与财务，风险"></textarea></label></div><div class="provider-note">新行业独立归档，自动采集默认暂停。准备好范围和来源后再开启。</div><div class="dialog-actions"><button class="primary" type="submit">创建行业</button></div></form>`);}
function editScope(id){const entry=S.data.industries.find(i=>i.id===id), c=entry.scope;dialog('编辑 '+entry.name+' 的研究范围',`<form id="scope-form" data-id="${esc(id)}"><div class="form-grid"><label class="field">主要地区<input name="primary_region" value="${esc(c.primary_region)}" required></label><label class="field">对照地区<input name="comparison_regions" value="${esc((c.comparison_regions||[]).join('，'))}"></label><label class="field full">分类<textarea name="topics" required>${esc(c.topics.join('，'))}</textarea><small>修改分类不自动重写已有事实；旧证据可单独编辑归档。</small></label><label class="field">默认复核周期（天）<input name="default_days" type="number" min="1" max="36500" value="${c.freshness?.default_days||30}"></label><label class="field">最近检索范围（天）<input name="lookback_days" type="number" min="1" value="${c.lookback_days}"></label><label class="field full">按分类设置复核周期（JSON对象）<textarea name="category_days">${esc(JSON.stringify(c.freshness?.category_days||{},null,2))}</textarea></label></div><div class="dialog-actions"><button class="primary" type="submit">保存范围</button></div></form>`);}
function sourceDialog(id){const c=S.sources.sources.find(s=>s.id===id)||{};dialog(id?'编辑信息来源':'添加公开来源',`<form id="source-form" data-original="${esc(id||'')}"><div class="form-grid"><label class="field">名称<input name="name" required value="${esc(c.name)}"></label><label class="field">来源编号<input name="id" required pattern="[a-z0-9][a-z0-9_-]{2,79}" value="${esc(c.id)}" ${id?'readonly':''}></label><label class="field full">公开入口URL<input type="url" name="url" required value="${esc(c.url)}"></label><label class="field">来源类型<select name="type">${options([['primary','官方原始来源'],['government','政府与监管'],['company','企业公告'],['agency_report','机构报告'],['media','媒体线索']],c.type||'primary')}</select></label><label class="field">优先级<input type="number" name="priority" min="1" max="5" value="${c.priority||1}"></label><label class="field full">适用分类<input name="categories" value="${esc((c.categories||[]).join('，'))}" placeholder="逗号分隔"></label><label class="field full">备注<textarea name="notes">${esc(c.notes)}</textarea></label></div><div class="provider-note">新增或更改地址会标记为待核验。暂停来源不会删除旧证据。</div><div class="dialog-actions"><button class="primary" type="submit">保存来源</button></div></form>`);}
async function showRecord(industry,id){const r=await api(`/evidence/${industry}/${id}`);dialog(r.title,`<div class="actions">${badge(r.status,labels[r.status])}${badge(r.freshness,r.freshness_label)}</div><div class="detail-meta"><div>发布机构<strong>${esc(r.publisher)}</strong></div><div>主分类<strong>${esc(r.category)}</strong></div><div>发布日期<strong>${esc(r.published_date||'未知')}</strong></div><div>事件日期<strong>${esc(r.event_date||'未知 / 不适用')}</strong></div><div>核验日期<strong>${esc(r.checked_date)}</strong></div><div>复核期限<strong>${esc(r.review_due_date)}</strong></div></div><div class="detail-summary">${esc(r.summary)}</div><p><a href="${esc(safeURL(r.url))}" target="_blank" rel="noopener noreferrer">阅读原始来源 ↗</a></p><div class="detail-limit"><strong>适用范围与限制</strong><br>${esc(r.limitations||'未填写，需补充。')}</div>${r.metrics?.length?`<details><summary>数字与统计口径 (${r.metrics.length})</summary><pre class="json">${esc(JSON.stringify(r.metrics,null,2))}</pre></details>`:''}<details><summary>历史版本 (${r.history.length})</summary>${r.history.length?r.history.map(h=>`<p>${esc(h.updated_at)} · ${esc(labels[h.status])}</p><div class="detail-limit">${esc(h.summary)}</div>`).join(''):'<p>尚无修订，原记录仍保留。</p>'}</details><div class="dialog-actions"><button class="secondary" id="edit-evidence">编辑归档信息</button><button class="primary" id="ask-record">询问这条证据</button></div>`);$('#edit-evidence').onclick=()=>manualEvidence(r,industry);$('#ask-record').onclick=()=>{$('#dialog').close();$('#chat-input').value=`解释证据 ${r.id}：${r.title}，说明原始来源、核验日期、限制和可继续调查的问题。`;$('#chat-input').focus();};}
function manualEvidence(record=null,industry=chooseIndustry()){
  if(!industry){toast('请先建立一个行业');return;}
  const clean=record?Object.fromEntries(Object.entries(record).filter(([k])=>!['industry_id','freshness','freshness_label','review_due_date','as_of','path','history'].includes(k))):{title:'',summary:'',url:'',publisher:'',region:'中国',category:S.data.industries.find(i=>i.id===industry).scope.topics[0],status:'needs_verification',checked_date:S.data.as_of,published_date:null,event_date:null,tags:[],limitations:'',metrics:[]};
  dialog('录入证据 · '+industryName(industry),`<form id="evidence-form" data-industry="${esc(industry)}"><label class="field">证据JSON<textarea name="record" required rows="18">${esc(JSON.stringify(clean,null,2))}</textarea><small>核验日期必须来自真实核对。修订已有事实应保留id；保存后自动留历史。</small></label><div class="dialog-actions"><button class="primary" type="submit">校验并保存</button></div></form>`);
}
async function openDoc(path){const doc=await api('/document?'+new URLSearchParams({path}));dialog('阅读报告',`<div class="doc-content">${esc(doc.text)}</div><div class="dialog-actions"><a class="primary" href="/api/download?${new URLSearchParams({path})}">下载 Markdown</a></div>`);}
function appendMessage(role,text){const el=document.createElement('div');el.className='message '+role;el.innerHTML=`<div class="message-label">${role==='user'?'你':'✳ 研究助手'}</div><div class="message-content">${esc(text)}</div>`;$('#chat-log').append(el);$('#chat-log').scrollTop=$('#chat-log').scrollHeight;return el;}
async function sendAgent(crawlId=null){
  const prompt=$('#chat-input').value.trim();if(!prompt||S.job)return;
  const research=$('#research-mode').checked;
  if(research&&S.industry==='all'){toast('研究归档需要先选择单个行业');return;}
  $('.agent-welcome')?.remove();appendMessage('user',prompt);$('#chat-input').value='';
  const answer=appendMessage('assistant','正在连接模型…');const body=answer.querySelector('.message-content');
  try{
    const job=await api('/agent/jobs','POST',{prompt,industry:S.industry,research,conversation:S.conversation,allow_source_changes:$('#source-changes').checked,crawl_id:crawlId});
    S.conversation=job.conversation;S.job=job.id;$('#send-agent').disabled=true;$('#stop-agent').hidden=false;body.innerHTML='';
    const eventLog=document.createElement('div'),text=document.createElement('div');text.className='answer-markdown';body.append(eventLog,text);
    S.stream=new EventSource('/api/agent/jobs/'+job.id+'/events');
    S.stream.addEventListener('update',async e=>{
      const event=JSON.parse(e.data);
      if(['tool','status','archived','tool_error','error'].includes(event.type)){const line=document.createElement('div');line.className='tool-event'+(['error','tool_error'].includes(event.type)?' error':'');line.textContent=event.title+(event.detail?' · '+event.detail:'');eventLog.append(line);}
      if(event.type==='citations'){event.items.slice(0,4).forEach(c=>{const link=document.createElement('a');link.className='citation';link.href=safeURL(c.url);link.target='_blank';link.rel='noopener noreferrer';link.innerHTML=esc(c.title)+`<small>核验 ${esc(c.checked_date)} · ${esc(c.freshness_label)}</small>`;eventLog.append(link);});}
      if(event.type==='answer')text.innerHTML=markdown(event.text);
      if(event.type==='report'){const button=document.createElement('button');button.className='citation';button.textContent='阅读生成的证据简报 ↗';button.onclick=()=>openDoc(event.path).catch(handleError);eventLog.append(button);}
      if(event.type==='done'){S.stream.close();S.stream=null;S.job=null;$('#send-agent').disabled=false;$('#stop-agent').hidden=true;if(event.archived_ids?.length)await refresh();}
      $('#chat-log').scrollTop=$('#chat-log').scrollHeight;
    });
    S.stream.onerror=()=>{if(S.job){text.textContent='事件连接中断，正在尝试恢复；可点击停止取消任务。';}};
  }catch(error){body.textContent=error.message;S.job=null;$('#send-agent').disabled=false;$('#stop-agent').hidden=true;}
}
document.addEventListener('click',async e=>{try{
  const nav=e.target.closest('[data-view]');if(nav){S.view=nav.dataset.view;await render();return;}
  const tab=e.target.closest('[data-status]');if(tab){S.status=tab.dataset.status;await render();return;}
  const prompt=e.target.closest('[data-prompt]');if(prompt){$('#chat-input').value=prompt.dataset.prompt;$('#chat-input').focus();return;}
  if(e.target.closest('a'))return;
  const card=e.target.closest('[data-record]');if(card){await showRecord(card.dataset.industry,card.dataset.record);return;}
  const action=e.target.closest('[data-action]');if(!action)return;
  const id=action.dataset.id;
  switch(action.dataset.action){
    case'add-industry':addIndustry();break;
    case'scope':editScope(id);break;
    case'set-active':await api('/industries/'+id,'PUT',{active:true});await refresh();toast('默认行业已更新');break;
    case'browse-industry':S.industry=id;S.view='evidence';S.q='';S.category='';S.status='';S.freshness='';await refresh();break;
    case'add-source':sourceDialog();break;
    case'edit-source':sourceDialog(id);break;
    case'crawl-search':{const picks=[...document.querySelectorAll('[name="web-pick"]:checked')].map(el=>webResults.items[Number(el.value)].url);if(!picks.length){toast('先勾选要采集的网页');break;}crawlSeeds=picks.join('\n');S.view='crawls';await render();break;}
    case'source-from-search':{const item=webResults.items[Number(id)];S.sources=await api('/sources/'+S.industry);sourceDialog();$('#source-form').elements.namedItem('name').value=item.title.slice(0,100);$('#source-form').elements.namedItem('url').value=item.url;$('#source-form').elements.namedItem('id').value='web-'+crypto.randomUUID().slice(0,8);break;}
    case'stop-crawl':await api('/crawls/'+id+'/stop','POST',{});toast('正在停止采集');break;
    case'organize-crawl':{if(S.job){toast('先等待当前Agent任务完成');break;}const job=await api('/crawls/'+id);const items=job.items.filter(i=>i.state!=='failed'&&i.state!=='duplicate').slice(0,12);$('#research-mode').checked=true;S.conversation=null;updateChatScope();$('#chat-input').value='请整理这批已抓取的候选页面。先查看行业范围和现有证据，再逐一使用read_public_source读取原文（有缓存时使用缓存），筛出有价值且属于该行业的事实，忠实摘要、分类并归档，最多12条。同一事实沿用已有id，不要把搜索摘要、入口导航、重复内容或计划当成已实现事实。日期未知填null，数字保留完整口径。最后列原始链接和未解决问题。候选：\n'+items.map(i=>i.title+' '+i.url).join('\n');$('#agent-panel').classList.add('show');await sendAgent(id);break;}
    case'manual-evidence':manualEvidence();break;
    case'report':{const result=await api('/reports','POST',{industry:S.industry,query:S.q});toast('简报已生成');await openDoc(result.path);break;}
    case'open-doc':await openDoc(action.dataset.path);break;
    case'plan':{const plan=await api('/update-plan');dialog('当前调查计划',`<div class="doc-content">${esc(JSON.stringify(plan,null,2))}</div>`);break;}
  }
}catch(error){handleError(error);}});
document.addEventListener('change',async e=>{try{
  if(e.target.id==='industry-select'){S.industry=e.target.value;S.category='';S.q='';webResults=null;crawlSeeds='';S.conversation=null;updateChatScope();await render();return;}
  if(e.target.id==='category-filter'){S.category=e.target.value;await render();return;}
  if(e.target.id==='freshness-filter'){S.freshness=e.target.value;await render();return;}
  if(e.target.id==='research-mode'){S.conversation=null;updateChatScope();return;}
  if(e.target.dataset.industryEnabled){await api('/industries/'+e.target.dataset.industryEnabled,'PUT',{enabled:e.target.checked});await refresh();toast('每日采集开关已保存');return;}
  if(e.target.dataset.sourceEnabled){S.sources.sources.find(s=>s.id===e.target.dataset.sourceEnabled).enabled=e.target.checked;await api('/sources/'+chooseIndustry(),'PUT',S.sources);toast('来源开关已保存');return;}
  if(e.target.id==='source-mode'){S.sources.mode=e.target.value;await api('/sources/'+chooseIndustry(),'PUT',S.sources);toast('来源模式已保存');}
}catch(error){handleError(error);await refresh();}});
document.addEventListener('submit',async e=>{e.preventDefault();
  const form=e.target;
  // Named form controls can shadow properties such as form.id.
  const formId=form.getAttribute('id');
  form.querySelector('.form-error')?.remove();
  try{
    const values=Object.fromEntries(new FormData(form));
    if(formId==='search-form'){S.q=$('#search-input').value;await render();}
    if(formId==='web-search-form'){const button=form.querySelector('[type="submit"]');button.disabled=true;button.textContent='搜索中…';const industry=S.industry;try{const result=await api('/network/search','POST',{industry,query:values.query,period:values.period||null,limit:Number(values.limit)});if(S.industry===industry){webResults=result;webOptions={period:values.period,limit:Number(values.limit)};if(S.view==='websearch')renderWebSearch();}}finally{if(button.isConnected){button.disabled=false;button.textContent='搜索网页';}}}
    if(formId==='crawl-form'){crawlSeeds=values.urls;crawlOptions={max_pages:Number(values.max_pages),max_depth:values.max_depth};const job=await api('/crawls','POST',{industry:S.industry,urls:values.urls.split('\n').map(s=>s.trim()).filter(Boolean),max_pages:Number(values.max_pages),max_depth:Number(values.max_depth)});lastCrawlId=job.id;await renderCrawls();toast('采集任务已启动');}
    if(formId==='network-settings-form'){S.data.network=await api('/network/config','PUT',values);toast('搜索后端已保存');}
    if(formId==='industry-form'){await api('/industries','POST',{...values,categories:split(values.categories).length?split(values.categories):null});S.industry=values.id;$('#dialog').close();await refresh();toast('行业已创建，采集默认暂停');}
    if(formId==='scope-form'){await api('/scope/'+form.dataset.id,'PUT',{primary_region:values.primary_region,comparison_regions:split(values.comparison_regions),topics:split(values.topics),lookback_days:Number(values.lookback_days),freshness:{default_days:Number(values.default_days),category_days:JSON.parse(values.category_days)}});$('#dialog').close();await refresh();toast('研究范围已保存');}
    if(formId==='source-form'){const original=form.dataset.original;const candidate=structuredClone(S.sources);const index=candidate.sources.findIndex(s=>s.id===original);const item={...values,priority:Number(values.priority),categories:split(values.categories),enabled:index>=0?candidate.sources[index].enabled!==false:true};if(index>=0)candidate.sources[index]={...candidate.sources[index],...item};else candidate.sources.push(item);const saved=await api('/sources/'+chooseIndustry(),'PUT',candidate);S.sources=saved;$('#dialog').close();await refresh();toast('信息来源已保存');}
    if(formId==='evidence-form'){await api('/evidence/'+form.dataset.industry,'POST',JSON.parse(values.record));$('#dialog').close();await refresh();toast('证据已校验保存');}
    if(formId==='settings-form'){values.request_limit=Number(values.request_limit);values.timeout_seconds=Number(values.timeout_seconds);if(!values.api_key)delete values.api_key;const result=await api('/agent/config','PUT',values);S.data.agent=result;$('#model-name').textContent=result.display_name+' / '+result.model;form.elements.namedItem('api_key').value='';toast('模型配置已保存');}
    if(formId==='chat-form')await sendAgent();
  }catch(error){
    const message=document.createElement('div');message.className='form-error notice';message.setAttribute('role','alert');message.textContent=error.message||'保存失败，请检查填写内容。';form.prepend(message);
    handleError(error);
  }
});
document.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.closest('[data-record]')){const c=e.target.closest('[data-record]');showRecord(c.dataset.industry,c.dataset.record).catch(handleError);}});
$('#chat-input').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendAgent().catch(handleError);}});
$('#close-dialog').onclick=()=>$('#dialog').close();
$('#refresh').onclick=()=>refresh().then(()=>toast('资料已刷新')).catch(handleError);
$('#agent-settings').onclick=()=>{S.view='settings';$('#agent-panel').classList.remove('show');render().catch(handleError);};
$('#new-chat').onclick=()=>{if(S.job){toast('请先停止当前任务');return;}S.conversation=null;$('#chat-log').innerHTML='';appendMessage('assistant','已开始新的研究对话。');};
$('#show-agent').onclick=()=>$('#agent-panel').classList.toggle('show');
$('#stop-agent').onclick=()=>S.job&&api('/agent/jobs/'+S.job+'/stop','POST',{}).catch(handleError);
document.addEventListener('click',async e=>{if(e.target.id==='test-model'){e.target.disabled=true;try{const result=await api('/agent/test','POST',{});$('#connection-result').textContent=result.message;$('#connection-result').classList.toggle('bad',!result.ok);}catch(error){handleError(error);}finally{e.target.disabled=false;}}});
refresh().catch(handleError);
