'use strict';
const $ = s => document.querySelector(s);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const safeURL = value => {try{const u = new URL(value,location.origin);return ['http:','https:'].includes(u.protocol)?u.href:'#';}catch{return '#';}};
const S = {view:'research', toolsOpen:false, toolContext:null, renderSeq:0, toolSeq:0, researchTool:'search', reviewOnly:false, sending:false, sourceIndustry:null, industry:'all', data:null, records:[], q:'', category:'', freshness:'', status:'', sources:null, conversation:null, job:null, stream:null};
const labels = {confirmed:'已核验',reported:'来源声称',planned:'计划 / 目标',needs_verification:'待核验',superseded:'已被替代'};
let webResults=null, crawlSeeds='', crawlPoll=null, lastCrawlId=null;
let webOptions={period:'',limit:10}, crawlOptions={max_pages:10,max_depth:'1'};
const crawlLabels={running:'采集中',completed:'采集完成',partial:'部分失败',failed:'采集失败',cancelled:'已停止',interrupted:'服务重启中断',new:'新增候选',changed:'内容变化',unchanged:'内容未变化',duplicate:'重复内容'};
let booted=false, refreshSeq=0, materialKind='', materialRecords=[], conversationRecords=[];
let verificationQueue=[], verificationReviews=[], materialSelection=new Set(), verificationPreparing=false;
const extractionJobs=new Map(),extractionLabels={queued:'等待提取',running:'正在提取',paused:'已暂停',partial:'部分批次失败',completed:'可解析内容处理完成',waiting_model:'等待模型配置',waiting_permission:'等待资料模型授权',invalidated:'资料已变化，任务已停止'};
let extractionPoll=null,extractionPolling=false;
const extractionKey=j=>JSON.stringify([j.industry_id,j.id]);
const extractionActive=j=>['queued','running'].includes(j?.status);
const extractionNumber=value=>Number.isFinite(Number(value))?Math.max(0,Number(value)):0;
const verificationLabels={unverified:'尚未核验',pending:'待审核',reviewed:'核验已通过',approved:'核验已通过',rejected:'核验未通过',invalidated:'资料已变化，需重新核验'};
const materialKey=r=>JSON.stringify([r.industry_id,r.id||r.material_id]);
const verificationState=r=>r.verification_status||r.verification?.status||'unverified';
const materialValidity=r=>r.status==='unknown'?'有效性待确认':r.status_label;
const materialApproved=r=>['reviewed','approved'].includes(verificationState(r));
function materialVerificationText(r){
  const state=verificationState(r);if(!materialApproved(r))return verificationLabels[state]||'尚未核验';
  if(r.extraction_job?.mode==='hard_chunk_index')return '已通过 · '+(r.extraction_job.status==='completed'?'全文已索引':'索引需重建');
  if(r.extraction_job)return '已通过 · '+(extractionLabels[r.extraction_job.status]||'全文提取待处理');
  return r.verification?.extraction_status==='failed'?'已通过 · 证据提取失败':r.verification?.extraction_status==='pending'?'已通过 · 尚无全文提取任务':verificationLabels[state]||'核验已通过';
}
function verificationBadge(r){const warning=!materialApproved(r)||['partial','waiting_model','waiting_permission','invalidated'].includes(r.extraction_job?.status)||r.verification?.extraction_status==='failed';return badge(warning?'needs_verification':'current',materialVerificationText(r));}
function materialVerificationAction(r,detail=false){
  const style=detail?'secondary':'link-button';
  if(materialApproved(r))return `<button class="${style}" data-action="restart-material-extraction" data-id="${esc(r.id)}" data-industry="${esc(r.industry_id)}" ${extractionActive(r.extraction_job)?'disabled':''}>重建全文索引</button>`;
  return `<button class="${style}" ${detail?'id="verify-material-manual"':`data-action="verify-material-manual" data-id="${esc(r.id)}" data-industry="${esc(r.industry_id)}"`}>通过核验</button>`;
}

function extractionSummary(job){
  if(job.mode==='hard_chunk_index')return job.status==='completed'?`全文已索引 · ${extractionNumber(job.indexed_chunks)} 个固定分块 · ${extractionNumber(job.source_characters).toLocaleString()} 个原文字符 · 原文可检索`:'原文版本已变化，请重建全文索引';
  const p=job.progress||{},done=extractionNumber(p.completed),total=extractionNumber(p.total),failed=extractionNumber(p.failed),images=extractionNumber(p.unprocessed_images),blankTables=extractionNumber(p.tables_without_text??p.table_without_text??job.tables_without_text??job.table_without_text),tables=extractionNumber(p.total_tables),processedTables=extractionNumber(p.processed_tables);
  if(job.status==='completed'){
    const textDone=processedTables<Math.max(0,tables-blankTables)?`全文文字处理完成；表格已处理 ${processedTables} / ${tables}（尚有表格未处理）`:blankTables?'全文可解析文字及原生文字表格处理完成':'全文文字及表格处理完成';
    return `${textDone}${blankTables?`；无原生文字表格 ${blankTables} 个未识别`:''}；${images?`图片 ${images} 张未处理`:'图片处理范围见覆盖说明'}。已入库 ${extractionNumber(p.evidence_count)} 条证据`;
  }
  return `${extractionLabels[job.status]||'待处理'} · 已处理 ${done} / ${total} 批${failed?` · 失败 ${failed} 批`:''} · 已入库 ${extractionNumber(p.evidence_count)} 条`;
}
function extractionMarkup(job,detailed=false){
  if(job.mode==='hard_chunk_index')return `<div class="extraction-status" role="status"><p class="extraction-summary">${esc(extractionSummary(job))}</p>${detailed?`<p class="description">按 ${extractionNumber(job.chunk_size)} 字符硬切分，相邻重叠 ${extractionNumber(job.overlap)} 字符，保留原文逐字引用位置。原生表格 ${extractionNumber(job.progress?.total_tables)} 个；未识别媒体 ${extractionNumber(job.progress?.unprocessed_images)} 个。</p><p class="description">${esc(job.coverage_note)}</p><p class="description">导入和建索引不调用模型。提问后先召回片段，再由模型重排与分析；已有事实证据保留。</p>`:`<button class="link-button" data-action="view-extraction" data-industry="${esc(job.industry_id)}" data-id="${esc(job.id)}">查看索引详情</button>`}</div>`;
  const p=job.progress||{},total=extractionNumber(p.total),done=extractionNumber(p.completed),characters=extractionNumber(p.total_characters),percent=Math.min(100,Math.floor(characters?extractionNumber(p.processed_characters)/characters*100:total?done/total*100:0));
  const identity=`data-industry="${esc(job.industry_id)}" data-id="${esc(job.id)}"`,controls=[];
  if(extractionActive(job))controls.push(`<button class="link-button" data-action="control-extraction" data-control="pause" ${identity}>暂停</button>`);
  if(['paused','waiting_model','waiting_permission'].includes(job.status))controls.push(`<button class="link-button" data-action="control-extraction" data-control="resume" ${identity}>继续提取</button>`);
  if(job.status==='partial')controls.push(`<button class="link-button" data-action="control-extraction" data-control="retry" ${identity}>重试失败批次</button>`);
  if(!detailed)controls.push(`<button class="link-button" data-action="view-extraction" ${identity}>查看提取详情</button>`);
  const batches=Array.isArray(job.batches)?job.batches:[],batchList=detailed&&batches.length?`<details class="extraction-batches"><summary>批次处理清单 (${batches.length})</summary>${batches.slice(0,100).map((batch,index)=>`<p><strong>批次 ${extractionNumber(batch.index??(index+1))}</strong> · ${esc(({pending:'待处理',running:'正在处理',completed:'已处理',no_evidence:'已处理，未发现可提取证据',failed:'失败',queued:'等待处理'})[batch.status]||batch.status||'待处理')}${batch.error?`<br><span class="extraction-problem">${esc(batch.error)}</span>`:''}</p>`).join('')}${batches.length>100?`<p class="description">显示前 100 批，任务总进度涵盖全部 ${batches.length} 批。</p>`:''}</details>`:'';
  return `<div class="extraction-status ${job.status==='partial'?'extraction-error':''}" role="status"><p class="extraction-summary">${esc(extractionSummary(job))}</p><progress max="100" value="${percent}" aria-label="全文证据提取进度 ${percent}%"></progress>${detailed?`<div class="extraction-counts"><span>字符 ${extractionNumber(p.processed_characters).toLocaleString()} / ${extractionNumber(p.total_characters).toLocaleString()}</span><span>表格 ${extractionNumber(p.processed_tables)} / ${extractionNumber(p.total_tables)}</span><span>未处理图片 ${extractionNumber(p.unprocessed_images)}</span>${extractionNumber(p.tables_without_text??p.table_without_text)?`<span>无原生文字表格 ${extractionNumber(p.tables_without_text??p.table_without_text)} 个未识别</span>`:''}</div>`:''}${job.status==='waiting_model'?'<p class="description">请在设置中配置模型密钥，然后继续提取。资料已通过，尚未完成全文证据提取。</p>':''}${job.status==='waiting_permission'?'<p class="description">请在资料编辑中允许模型使用片段，然后继续提取。当前不会把资料发送给模型。</p>':''}${job.error?`<p class="extraction-problem">${esc(job.error)}</p>`:''}${detailed?`<p class="description">${esc(job.coverage_note||job.scope_note||'处理范围为可解析文字与表格；图片尚未识别，不代表外部事实已核实。')}</p><p class="description">任务在本机后台运行，关闭页面后仍会继续；服务重启后可继续未完成批次。</p>`:''}${batchList}<div class="material-review-actions">${controls.join('')}</div></div>`;
}
function historicalExtraction(job){return {...job,status:'invalidated',coverage_note:'当前资料尚未通过有效版本核验。以下进度属于历史提取任务，不能代表当前资料已经完成证据提取。'};}
function extractionHost(job,detailed=false,stale=false){if(!job)return '';extractionJobs.set(extractionKey(job),job);return `<div data-extraction-id="${esc(job.id)}" data-extraction-industry="${esc(job.industry_id)}" data-extraction-detail="${detailed?'true':'false'}" data-extraction-stale="${stale?'true':'false'}">${extractionMarkup(stale?historicalExtraction(job):job,detailed)}</div>`;}
function materialExtractionHost(record,detailed=false){return extractionHost(record.extraction_job,detailed,record.extraction_job?.mode!=='hard_chunk_index'&&!materialApproved(record));}
function updateExtractionHosts(job){
  extractionJobs.set(extractionKey(job),job);
  for(const r of materialRecords)if(r.industry_id===job.industry_id&&r.id===job.material_id&&(!r.extraction_job||r.extraction_job.id===job.id))r.extraction_job=job;
  document.querySelectorAll('[data-extraction-id]').forEach(host=>{if(host.dataset.extractionId!==job.id||host.dataset.extractionIndustry!==job.industry_id)return;const batchesOpen=host.querySelector('.extraction-batches')?.open;host.innerHTML=extractionMarkup(host.dataset.extractionStale==='true'?historicalExtraction(job):job,host.dataset.extractionDetail==='true');if(batchesOpen&&host.querySelector('.extraction-batches'))host.querySelector('.extraction-batches').open=true;const card=host.closest('.evidence-card');if(card){const badge=card.querySelector('.verification-badges')?.children[0];if(badge){const record=materialRecords.find(r=>r.id===job.material_id&&r.industry_id===job.industry_id);badge.textContent=record?materialVerificationText(record):'提取任务 · '+(extractionLabels[job.status]||'待处理');}}});
  document.querySelectorAll('[data-action="restart-material-extraction"]').forEach(button=>{if(button.dataset.id===job.material_id&&button.dataset.industry===job.industry_id)button.disabled=extractionActive(job);});
}
function visibleActiveExtractions(){
  const unique=new Map();document.querySelectorAll('[data-extraction-id]').forEach(host=>{if(host.dataset.extractionStale==='true'||host.closest('#agent-parking')||host.closest('#dialog')&&!$('#dialog').open)return;const key=JSON.stringify([host.dataset.extractionIndustry,host.dataset.extractionId]),job=extractionJobs.get(key);if(extractionActive(job))unique.set(key,job);});return [...unique.values()];
}
function scheduleExtractionPoll(){clearTimeout(extractionPoll);extractionPoll=null;if(!extractionPolling&&visibleActiveExtractions().length)extractionPoll=setTimeout(pollExtractions,3000);}
async function pollExtractions(){
  extractionPoll=null;if(extractionPolling)return;const jobs=visibleActiveExtractions();if(!jobs.length)return;extractionPolling=true;
  try{const results=await Promise.allSettled(jobs.map(j=>api('/extractions/'+encodeURIComponent(j.industry_id)+'/'+encodeURIComponent(j.id))));results.forEach(result=>{if(result.status==='fulfilled')updateExtractionHosts(result.value.job||result.value);});}
  finally{extractionPolling=false;scheduleExtractionPoll();}
}
function showExtractionJob(job){const record=materialRecords.find(r=>r.id===job.material_id&&r.industry_id===job.industry_id),stale=record&&job.mode!=='hard_chunk_index'&&!materialApproved(record);dialog((stale?'历史提取任务 · ':'')+(job.title||'全文证据提取'),`<div class="extraction-detail">${extractionHost(job,true,stale)}<div class="dialog-actions"><button class="secondary" data-action="view-material" data-industry="${esc(job.industry_id)}" data-id="${esc(job.material_id)}">查看资料</button><button class="secondary" data-action="refresh-extraction" data-industry="${esc(job.industry_id)}" data-id="${esc(job.id)}">刷新状态</button></div></div>`);scheduleExtractionPoll();}
async function openExtraction(industry,id){const result=await api('/extractions/'+encodeURIComponent(industry)+'/'+encodeURIComponent(id));showExtractionJob(result.job||result);}
async function startMaterialExtraction(industry,id,button){
  if(button.disabled)return;button.disabled=true;const previousBody=$('#dialog-body').innerHTML,previousOpen=$('#dialog').open;
  try{const result=await api('/materials/'+encodeURIComponent(industry)+'/'+encodeURIComponent(id)+'/extractions','POST',{restart:true}),job=result.job||result;for(const r of materialRecords)if(r.id===id&&r.industry_id===industry)r.extraction_job=job;updateExtractionHosts(job);if(S.view==='materials')await render();if($('#dialog-body').innerHTML===previousBody&&$('#dialog').open===previousOpen)showExtractionJob(job);toast(job.mode==='hard_chunk_index'?'全文索引已建立，可以检索':job.status==='waiting_model'?'资料已通过，等待配置模型后提取':'全文证据提取任务已创建');}
  finally{if(button.isConnected)button.disabled=false;scheduleExtractionPoll();}
}
async function controlExtraction(industry,id,action,button){
  if(button.disabled)return;button.disabled=true;try{const result=await api('/extractions/'+encodeURIComponent(industry)+'/'+encodeURIComponent(id)+'/control','POST',{action});updateExtractionHosts(result.job||result);}
  finally{if(button.isConnected)button.disabled=false;scheduleExtractionPoll();}
}

async function api(path, method='GET', data){
  const response = await fetch('/api'+path,{method,headers:{'Content-Type':'application/json','X-KB-Request':'1'},body:data===undefined?undefined:JSON.stringify(data)});
  const result = await response.json();
  if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'填写内容未通过校验');
  return result;
}
let toastTimer;
function toast(message){$('#toast').textContent=message;$('#toast').style.display='block';clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').style.display='none',4500);}
function handleError(error){console.error(error);toast(error.message||'操作失败，请重试');}
function chooseIndustry(){return S.industry==='all'?(S.data.active_industry||S.data.industries[0]?.id):S.industry;}
function industryName(id){return S.data.industries.find(i=>i.id===id)?.name||id;}
function options(items,current){return items.map(([value,label])=>`<option value="${esc(value)}" ${value===current?'selected':''}>${esc(label)}</option>`).join('');}
function badge(state,label){return `<span class="badge ${['review_due','needs_verification','planned'].includes(state)?'warn':['expired'].includes(state)?'red':state==='superseded'?'grey':''}">${esc(label)}</span>`;}
function heading(title,subtitle,buttons=''){return `<div class="page-heading"><div><h1>${title}</h1><p>${subtitle}</p></div><div class="actions">${buttons}</div></div>`;}
function updateModelLabel(config){$('#model-name').textContent=(config.key_configured?'':'待配置密钥 · ')+config.display_name+' / '+config.model;$('#model-name').setAttribute('title',config.key_configured?'密钥已配置，实际连接以模型调用结果为准。':'请在模型与助手设置中填写API密钥，密钥仅保存在服务端内存。');}
function empty(title,text,button=''){return `<div class="empty"><div class="empty-icon">◫</div><strong>${title}</strong><p>${text}</p>${button}</div>`;}
function dialog(title,html){$('#dialog-title').textContent=title;$('#dialog-body').innerHTML=html;if(!$('#dialog').open)$('#dialog').showModal();scheduleExtractionPoll();}
function split(value){return value.split(/[，,\n]/).map(x=>x.trim()).filter(Boolean);}
function markdownCodeEnd(text,start,size){
  for(let i=text.indexOf('`',start+size);i>=0;i=text.indexOf('`',i)){
    let end=i;while(text[end]==='`')++end;
    if(end-i===size)return i;i=end;
  }
  return -1;
}
function markdownInline(raw){
  const markup=[],protect=html=>'\0'+(markup.push(html)-1)+'\0';
  const input=String(raw).replaceAll('\0','');let plain='';
  for(let i=0;i<input.length;){
    if(input[i]==='`'){
      let end=i;while(input[end]==='`')++end;
      const close=markdownCodeEnd(input,i,end-i);
      if(close>=0){plain+=protect('<code>'+esc(input.slice(end,close))+'</code>');i=close+end-i;continue;}
    }
    plain+=input[i++];
  }
  plain=plain.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,(match,label,url)=>{
    if(url.includes('\0'))return match;
    return protect(`<a href="${esc(safeURL(url))}" target="_blank" rel="noopener noreferrer">${esc(label).replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>')}</a>`);
  });
  let html=esc(plain).replace(/\*\*([^*\n]+)\*\*/g,'<strong>$1</strong>');
  // A link label may contain a protected code span; source NULs cannot forge tokens.
  for(let pass=0;pass<2;++pass)html=html.replace(/\0(\d+)\0/g,(_,n)=>markup[Number(n)]||'');
  return html;
}
function markdownTableCells(raw){
  const line=raw.trim(),cells=[];let cell='',hasPipe=false,lastPipe=-1;
  for(let i=0;i<line.length;){
    if(line[i]==='\\'){
      let end=i;while(line[end]==='\\')++end;
      if(line[end]==='|'){
        cell+='\\'.repeat(Math.floor((end-i)/2));
        if((end-i)%2){cell+='|';i=end+1;continue;}
        i=end;continue;
      }
      cell+=line.slice(i,end);i=end;continue;
    }
    if(line[i]==='`'){
      let end=i;while(line[end]==='`')++end;
      const close=markdownCodeEnd(line,i,end-i);
      if(close>=0){const finish=close+end-i;cell+=line.slice(i,finish).replace(/\\\|/g,'|');i=finish;continue;}
    }
    if(line[i]==='|'){hasPipe=true;lastPipe=i;cells.push(cell.trim());cell='';++i;}
    else cell+=line[i++];
  }
  if(!hasPipe)return null;
  cells.push(cell.trim());if(line[0]==='|')cells.shift();if(lastPipe===line.length-1)cells.pop();
  return cells;
}
function markdown(raw){
  const lines=String(raw??'').replace(/\r\n?/g,'\n').split('\n'),blocks=[];
  for(let i=0;i<lines.length;){
    const fence=/^\s{0,3}(`{3,}|~{3,})[^\n]*$/.exec(lines[i]);
    if(fence){
      const marker=fence[1],body=[];let end=i+1;
      for(;end<lines.length;++end){
        const close=/^\s{0,3}(`+|~+)\s*$/.exec(lines[end]);
        if(close&&close[1][0]===marker[0]&&close[1].length>=marker.length)break;
        body.push(lines[end]);
      }
      blocks.push('<pre><code>'+esc(body.join('\n'))+'</code></pre>');i=end<lines.length?end+1:end;continue;
    }
    const header=markdownTableCells(lines[i]),divider=i+1<lines.length?markdownTableCells(lines[i+1]):null;
    if(header?.length&&divider?.length===header.length&&divider.every(cell=>/^:?-+:?$/.test(cell))){
      const align=divider.map(cell=>cell.startsWith(':')&&cell.endsWith(':')?'center':cell.endsWith(':')?'right':'left');
      const row=(cells,tag)=>'<tr>'+header.map((_,n)=>`<${tag}${tag==='th'?' scope="col"':''} class="align-${align[n]}">${markdownInline(cells[n]||'')}</${tag}>`).join('')+'</tr>';
      const body=[];i+=2;
      while(i<lines.length){
        if(!lines[i].trim()||/^\s{0,3}(?:`{3,}|~{3,})/.test(lines[i]))break;
        const cells=markdownTableCells(lines[i]);if(!cells)break;body.push(row(cells,'td'));++i;
      }
      blocks.push('<div class="markdown-table-wrap" role="region" aria-label="表格" tabindex="0"><table class="markdown-table"><thead>'+row(header,'th')+'</thead><tbody>'+body.join('')+'</tbody></table></div>');continue;
    }
    const line=lines[i++],html=markdownInline(line);
    blocks.push(/^#{1,3} /.test(line)?`<h3>${markdownInline(line.replace(/^#+ /,''))}</h3>`:/^[-*] /.test(line)?`<div>• ${markdownInline(line.slice(2))}</div>`:`<div>${html||'<br>'}</div>`);
  }
  return blocks.join('');
}
async function refresh(){
  const request=++refreshSeq, data=await api('/state');
  if(request!==refreshSeq)return;
  S.data=data;
  if(!booted){booted=true;if(S.data.active_industry)S.industry=S.data.active_industry;}
  if(S.industry!=='all'&&!S.data.industries.some(i=>i.id===S.industry))S.industry='all';
  $('#industry-select').innerHTML=options([['all','全部行业档案'],...S.data.industries.map(i=>[i.id,i.name])],S.industry);
  $('#server-date').textContent=S.data.as_of.slice(5).replace('-','.');
  updateModelLabel(S.data.agent);
  updateChatScope();
  await render();
}
function updateChatScope(){const research=$('#research-mode').checked;$('#chat-scope').textContent=(S.industry==='all'?'全部档案':industryName(S.industry))+' · '+(research?'研究归档':'只读');}
async function render(){
  if(S.view==='timeline')S.view='research';
  ++S.renderSeq;
  ++S.toolSeq;
  clearTimeout(crawlPoll);clearTimeout(extractionPoll);extractionPoll=null;
  // Keep the one live Agent tree (and its listeners/stream targets) across pages.
  $('#agent-parking').append($('#agent-panel'));
  document.body.dataset.currentView=S.view;
  const workspace=['materials','evidence'].includes(S.view)?'library':S.view;
  document.querySelectorAll('[data-workspace]').forEach(b=>{
    const active=b.dataset.workspace===workspace;
    b.classList.toggle('active',active);
    b.setAttribute('aria-current',active?'page':'false');
  });
  if(S.view==='materials')return renderMaterials();
  if(S.view==='evidence')return renderEvidence();
  if(S.view==='research')return renderResearch();
  if(S.view==='settings')return renderSettings();
}

function researchProfiles(){
  let entries=S.data.industries.filter(i=>S.industry==='all'||i.id===S.industry);
  const selected=entries[0];
  if(S.industry!=='all'&&selected?.scope.specialty==='forestry'&&selected.scope.library_role!=='national-foundation'){
    entries=[...entries,...S.data.industries.filter(i=>i.scope.specialty==='forestry'&&i.scope.library_role==='national-foundation'&&i.id!==selected.id)];
  }
  return entries;
}
function openAgent(){
  if(S.view!=='research'){S.view='research';render().catch(handleError);}
}
function localConnectionError(error){
  handleError(error);
  if(!S.data)$('#content').innerHTML=empty('暂时无法连接知识库','请确认本机服务正在运行，然后重新连接。','<button class="primary" data-action="reconnect">重新连接</button>');
}
function libraryHeader(){
  return `<div class="page-heading library-heading"><div><h1>知识库</h1><p>在同一个空间查阅原始资料和知识证据。</p></div><button class="primary" data-action="add-material">＋ 导入资料</button></div><nav class="library-switch" aria-label="知识库视图">${[['materials','资料'],['evidence','知识证据']].map(([view,label])=>`<button class="${S.view===view?'active':''}" data-view="${view}" aria-current="${S.view===view?'page':'false'}">${label}</button>`).join('')}</nav>`;
}
function librarySearch(){return `<form id="library-search" class="searchbox"><span aria-hidden="true">⌕</span><input id="library-query" name="q" type="search" value="${esc(S.q)}" placeholder="搜索标题、正文、对象或关键词…" aria-label="检索知识库"><button class="secondary" type="submit">搜索</button></form>`;}
function renderResearch(){
  $('#content').innerHTML=`<div class="research-workspace"><section class="research-conversation" id="research-agent-slot" aria-label="研究对话"></section><aside id="research-tools-drawer" class="research-tools research-drawer" aria-labelledby="research-tools-title" hidden><div class="research-drawer-header"><h2 id="research-tools-title">研究工具</h2><button class="icon-button" id="close-research-tools" aria-label="收起研究工具">×</button></div><nav class="library-switch" aria-label="研究工具">${[['search','找资料'],['crawl','采集'],['outputs','产出']].map(([tool,label])=>`<button class="${S.researchTool===tool?'active':''}" data-research-tool="${tool}">${label}</button>`).join('')}</nav><div id="research-tool-content"></div></aside></div>`;
  S.toolContext=null;
  $('#research-agent-slot').append($('#agent-panel'));
  syncResearchTools();
  if(S.toolsOpen)return renderResearchTool();
}
function syncResearchTools(){
  const drawer=$('#research-tools-drawer');if(!drawer)return;
  drawer.hidden=!S.toolsOpen;
  drawer.inert=!S.toolsOpen;
  $('.research-workspace').classList.toggle('tools-open',S.toolsOpen);
  $('#research-tools-toggle').setAttribute('aria-expanded',String(S.toolsOpen));
}
async function setResearchTools(open){
  if(S.view!=='research')return;
  S.toolsOpen=open;syncResearchTools();
  if(!open){
    clearTimeout(crawlPoll);
    $('#research-tools-toggle').focus();
    return;
  }
  $('#close-research-tools').focus();
  const context=S.industry+':'+S.researchTool;
  if(S.toolContext!==context)return renderResearchTool();
  if(S.researchTool==='crawl')pollCrawls(S.industry);
}
async function renderResearchTool(){
  if(S.view!=='research')return;
  const ticket=++S.toolSeq,context=S.industry+':'+S.researchTool;
  clearTimeout(crawlPoll);S.toolContext=null;
  S.toolsOpen=true;syncResearchTools();
  document.querySelectorAll('[data-research-tool]').forEach(b=>b.classList.toggle('active',b.dataset.researchTool===S.researchTool));
  $('#research-tool-content').innerHTML='<p class="description" role="status">正在读取…</p>';
  if(S.researchTool==='search')await renderWebSearch();
  else if(S.researchTool==='crawl')await renderCrawls();
  else await renderReports();
  if(ticket===S.toolSeq&&S.view==='research'&&context===S.industry+':'+S.researchTool)S.toolContext=context;
}
async function renderMaterials(){
  const ticket=S.renderSeq;
  const industry=S.industry;
  const [pending,records]=await Promise.all([api('/verifications?'+new URLSearchParams({industry,status:'pending'})),api('/materials?'+new URLSearchParams({industry,q:S.q,kind:materialKind}))]);
  if(ticket!==S.renderSeq||S.view!=='materials'||S.industry!==industry)return;
  materialRecords=records;records.forEach(r=>{if(r.extraction_job)extractionJobs.set(extractionKey(r.extraction_job),r.extraction_job);});
  verificationQueue=Array.isArray(pending)?pending:pending.items||[];
  materialSelection=new Set([...materialSelection].filter(key=>records.some(r=>materialKey(r)===key)));
  $('#content').innerHTML=libraryHeader()+librarySearch()+
    `<div class="filter-row"><select id="material-kind" aria-label="资料类型">${options([['','全部资料'],...Object.entries(S.data.material_kinds)],materialKind)}</select><span class="description">${records.length}份 · 资料核验与有效性分别管理</span></div><div class="verification-toolbar"><label><input id="material-select-all" type="checkbox" ${records.length&&records.every(r=>materialSelection.has(materialKey(r)))?'checked':''}> 全选本页</label><span id="material-selection-count">已选 ${materialSelection.size} 份 / 最多10份</span><button class="secondary" data-action="verify-selected-manual">批量通过核验</button><button class="secondary" data-action="verify-selected-automatic">批量自动核验</button><button class="secondary" data-action="verification-queue">待核验资料（${verificationQueue.length}）</button></div><p class="description">确认资料后点击“通过核验”，系统自动建立全文检索索引；也可先用自动核验检查资料。</p><div class="evidence-list">${records.map(r=>`<article class="evidence-card"><div class="card-top"><label class="material-selection"><input name="material-pick" type="checkbox" value="${esc(materialKey(r))}" ${materialSelection.has(materialKey(r))?'checked':''} aria-label="选择 ${esc(r.title)}"> ${esc(r.kind_label)} / ${esc(r.region||'地域待填')}</label><div class="verification-badges">${verificationBadge(r)}${r.status&&r.status!=='unknown'?badge(r.status==='obsolete'?'superseded':'current',materialValidity(r)):''}${r.review_due?badge('review_due','待复核'):''}</div></div><h3><button class="link-button material-title" data-action="view-material" data-id="${esc(r.id)}" data-industry="${esc(r.industry_id)}">${esc(r.title)}</button></h3><p class="summary">${esc(r.summary||'暂无摘要，可登记资料说明或上传文字版文档。')}</p><div class="card-foot"><span>${esc(r.number||r.issuer||'来源待填')} · ${r.has_attachment?'已有附件':'目录/文字条目'} · ${esc(industryName(r.industry_id))}</span><span class="tag">${r.model_allowed?'可供模型引用':'仅本地管理'}</span></div>${materialExtractionHost(r)}<div class="material-review-actions">${materialVerificationAction(r)}<button class="link-button" data-action="verify-material-automatic" data-id="${esc(r.id)}" data-industry="${esc(r.industry_id)}">自动核验</button></div></article>`).join('')||empty('当前分类没有资料','先登记目录，或上传已有文件。缺失的项目与空间数据保留为待补充项。')}</div>`;
  scheduleExtractionPoll();
}
function materialDialog(record={}){
  const industry=record.industry_id||chooseIndustry();if(!industry){toast('请先创建或选择行业');return;}
  const region=record.region||S.data.industries.find(i=>i.id===industry)?.scope.primary_region||'';
  dialog(record.id?(S.data.industries.find(i=>i.id===industry)?.scope.library_role==='national-foundation'?'编辑全国基础资料 · 各地区共用':'编辑专业资料'):'添加专业资料',`<form id="material-form" data-industry="${esc(industry)}" data-id="${esc(record.id||'')}"><div class="form-grid"><label class="field full">资料标题<input name="title" value="${esc(record.title)}" required maxlength="300"></label><label class="field">资料类型<select name="kind">${options(Object.entries(S.data.material_kinds),record.kind||materialKind||'policy')}</select></label><label class="field">地域<input name="region" value="${esc(region)}"></label><label class="field">发布/责任机构<input name="issuer" value="${esc(record.issuer)}"></label><label class="field">文号/标准编号<input name="number" value="${esc(record.number)}" placeholder="GB/T 15776-2023"></label><label class="field">版本<input name="version" value="${esc(record.version)}"></label><label class="field">有效性<select name="status">${options(Object.entries(S.data.material_states).map(([key,label])=>[key,key==='unknown'?'有效性待确认':label]),record.status||'unknown')}</select></label><label class="field">发布日期<input name="published_date" type="date" value="${esc(record.published_date)}"></label><label class="field">实施日期<input name="effective_date" type="date" value="${esc(record.effective_date)}"></label><label class="field">资料核对日期<input name="checked_date" type="date" value="${esc(record.checked_date)}"></label><label class="field">下次复核日期<input name="review_after" type="date" value="${esc(record.review_after)}"></label><label class="field full">来源URL<input name="url" type="url" value="${esc(record.url)}"></label><label class="field full">简明摘要<textarea name="summary" rows="3">${esc(record.summary)}</textarea></label><label class="field full">${record.extraction?.text_index?'附件提取正文（只读）':'可检索正文/资料说明'}<textarea name="content" rows="6" ${record.extraction?.text_index?'readonly':''}>${esc(record.content)}</textarea>${record.extraction?.text_index?'<small>原文需重新上传附件更新；补充说明请填写简明摘要或适用范围与限制。</small>':''}</label><label class="field full">附件（可选）<input name="file" type="file" accept=".txt,.md,.csv,.pdf,.docx,.png,.jpg,.jpeg,.tif,.tiff,.geojson,.zip"><small>10MB以内；扫描PDF未做OCR。影像和空间附件只登记元数据，不做GIS计算。</small></label><label class="field">资料级别<select name="sensitivity">${options([['public','公开资料'],['internal','内部资料'],['restricted','受限资料（仅本地）']],record.sensitivity||'internal')}</select></label><label class="field"><span>模型引用</span><span><input name="model_allowed" type="checkbox" ${record.model_allowed?'checked':''}>允许模型使用检索片段</span><small>厂商模型会接收引用片段，受限资料不送模型。</small></label><label class="field">调查年度/时相<input name="coverage_period" value="${esc(record.coverage_period)}"></label><label class="field">项目阶段<input name="project_stage" value="${esc(record.project_stage)}" placeholder="未批复 / 已批复 / 在建 / 已验收"></label><label class="field">比例尺<input name="scale" value="${esc(record.scale)}"></label><label class="field">坐标系<input name="coordinate_system" value="${esc(record.coordinate_system)}"></label><label class="field">影像分辨率<input name="resolution" value="${esc(record.resolution)}"></label><label class="field">标签<input name="tags" value="${esc((record.tags||[]).join('，'))}"></label><label class="field full">关联证据ID<input name="related_evidence_ids" value="${esc((record.related_evidence_ids||[]).join('，'))}" placeholder="逗号分隔，须为本行业已有证据"></label><label class="field full">适用范围与限制<textarea name="notes">${esc(record.notes)}</textarea></label></div><div class="dialog-actions"><button class="primary" type="submit">保存资料</button></div></form>`);  arrangeMaterialForm();
}
function arrangeMaterialForm(){
  const form=$('#material-form'),grid=form.querySelector('.form-grid');
  const fields=[...grid.children],basic=['title','file','kind','region','summary','content','sensitivity','model_allowed'];
  const advanced=document.createElement('details');advanced.className='material-advanced';
  const summary=document.createElement('summary');summary.textContent='来源、版本与专业信息（可选）';
  const extra=document.createElement('div');extra.className='form-grid';
  advanced.append(summary,extra);
  for(const name of basic){const field=fields.find(label=>label.querySelector('[name="'+name+'"]'));if(field)grid.append(field);}
  for(const field of fields){if(!basic.some(name=>field.querySelector('[name="'+name+'"]')))extra.append(field);}
  form.insertBefore(advanced,form.querySelector('.dialog-actions'));
}

async function saveMaterialForm(form,values){
  if(form.dataset.saving==='true')return;
  form.dataset.saving='true';
  const button=form.querySelector('[type="submit"]');
  button.disabled=true;button.textContent='保存中…';
  try{
    const industry=form.dataset.industry,file=form.elements.namedItem('file').files[0];
    if(file&&file.size>10000000)throw new Error('普通上传限10MB；更大的DOCX请使用本地导入命令。');
    delete values.file;
    const payload={...values,id:form.dataset.id||undefined,tags:split(values.tags),related_evidence_ids:split(values.related_evidence_ids),model_allowed:form.elements.namedItem('model_allowed').checked};
    let record;
    if(file){
      const body=new FormData();body.append('metadata',JSON.stringify(payload));body.append('file',file);
      const response=await fetch('/api/materials/'+industry+'/import',{method:'POST',headers:{'X-KB-Request':'1'},body});
      const result=await response.json();
      if(!response.ok)throw new Error(result.detail||'附件上传失败，资料未保存');
      record=result;
    }else record=await api('/materials/'+industry,'POST',payload);
    form.dataset.id=record.id;
    if(form.isConnected)$('#dialog').close();
    await render();toast('专业资料已保存');
  }finally{
    form.dataset.saving='false';button.disabled=false;button.textContent='保存资料';
  }
}

function selectedMaterials(){return materialRecords.filter(r=>materialSelection.has(materialKey(r)));}
function syncMaterialSelection(){
  const count=$('#material-selection-count');if(count)count.textContent=`已选 ${materialSelection.size} 份 / 最多10份`;
  const all=$('#material-select-all');if(all)all.checked=materialRecords.length>0&&materialRecords.every(r=>materialSelection.has(materialKey(r)));
}
function verificationMessages(errors=[]){return errors.length?`<div class="verification-errors" role="alert"><strong>以下资料未完成：</strong>${errors.map(e=>`<p>${esc(e.title||e.material_id||'资料')}：${esc(e.error||e.message||'操作失败')}</p>`).join('')}</div>`:'';}
async function prepareVerification(items,mode){
  if(verificationPreparing)return;
  if(!items.length){toast('先选择需要核验的资料');return;}
  if(items.length>10){toast('每批最多核验10份，请减少选择');return;}
  let authorizationErrors=[];
  if(mode==='automatic'){
    const denied=items.filter(r=>r.model_allowed!==true||r.sensitivity==='restricted');
    authorizationErrors=denied.map(r=>({title:r.title,error:r.sensitivity==='restricted'?'受限资料不能发送模型，请改用手动核验':'尚未允许模型引用，请在编辑资料中授权后重试，或改用手动核验'}));
    items=items.filter(r=>r.model_allowed===true&&r.sensitivity!=='restricted');
    if(!items.length){dialog('自动核验需要资料授权',`<p>自动核验会将本次所需原文片段发送给已配置的模型。请先在资料编辑中勾选“允许模型使用检索片段”；受限资料只能手动核验。</p>${verificationMessages(authorizationErrors)}`);return;}
  }
  verificationPreparing=true;
  const controls=[...document.querySelectorAll('[data-action]')].filter(b=>b.dataset.action.startsWith('verify-'));controls.forEach(b=>b.disabled=true);
  toast(mode==='automatic'?'正在检查资料，请稍候…':'正在准备资料核验…');
  try{
    const result=await api('/verifications/materials','POST',{items:items.map(r=>({industry_id:r.industry_id,material_id:r.id||r.material_id})),mode});
    const reviews=result.items||[],errors=[...authorizationErrors,...(result.errors||[])];
    if(S.view==='materials')await render();
    if(reviews.length){showVerificationReviews(reviews,errors);if(mode==='manual')await submitVerificationDecisions($('#verification-decision-form'),'approve');}
    else dialog('未生成核验任务',verificationMessages(errors)||'<p>没有可供审核的核验任务。</p>');
  }finally{verificationPreparing=false;controls.forEach(b=>b.disabled=false);}
}
function verificationNeedsExtraction(review){return !review.extraction_job&&review.status==='approved'&&review.extraction_status==='failed';}
function verificationExtracting(review){return review.status==='approved'&&extractionActive(review.extraction_job);}
function verificationCanApprove(review){return review.status==='pending'||verificationNeedsExtraction(review);}
function verificationResult(review){
  if(review.status!=='approved')return review.status==='rejected'?'暂不通过，未入库':verificationLabels[review.status]||'待确认';
  if(review.extraction_job)return '资料已通过，'+extractionSummary(review.extraction_job);
  if(verificationNeedsExtraction(review))return '资料已通过，证据提取失败，可重试'+(review.extraction_error?'：'+review.extraction_error:'');
  if(review.extraction_status==='pending')return '资料已通过，尚无全文提取任务，请在资料中重新提取证据';
  if(review.status==='approved')return review.evidence_ids?.length?(review.extraction_method==='source_excerpt'?`核验已通过，已保存${review.evidence_ids.length}条原文摘录证据`:`核验已通过，已提取并入库${review.evidence_ids.length}条证据`):'资料已通过，未提取到可入库证据';
  return review.status==='rejected'?'暂不通过，未入库':verificationLabels[review.status]||'待确认';
}
function verificationReviewCard(review,compact=false){
  const source=review.source||{},sourceText=typeof source==='string'?source:[source.title,source.url].filter(Boolean).join(' · ');
  const summary=review.summary||source.summary||review.excerpts?.[0]?.text||'';
  return `<section class="verification-review" data-review-id="${esc(review.id)}" data-review-industry="${esc(review.industry_id)}"><h3>${esc(review.title||'资料核验')}</h3>${!compact&&sourceText?`<p class="description">来源：${esc(sourceText)}</p>`:''}${!compact&&summary?`<p class="description">${esc(summary.slice(0,180))}${summary.length>180?'…':''}</p>`:''}${review.status==='pending'&&review.mode==='automatic'&&review.findings?.length?`<div class="verification-findings">${review.findings.slice(0,5).map(f=>`<p>${esc(typeof f==='string'?f:f.detail||f.message||'请核对资料内容')}</p>`).join('')}</div>`:''}${review.mode==='automatic'?`<p class="description">${esc(review.coverage_note||'模型仅检查已授权的部分片段，不代表全文及外部真实性已核实。')}</p>`:''}<p class="description" data-review-result>${esc(verificationResult(review))}</p>${extractionHost(review.extraction_job,false,review.status!=='approved')}</section>`;
}
function finishManualVerification(form,reviews,errors=[]){
  if(!form.isConnected||errors.length||!reviews.length||reviews.some(r=>r.mode!=='manual'||r.status!=='approved'||(!r.extraction_job&&r.extraction_status!=='completed')||r.decision_error))return;
  if(reviews.some(r=>r.extraction_job)){ $('#dialog').close();toast(reviews.some(r=>r.extraction_job?.mode==='hard_chunk_index')?'资料核验已通过，全文索引已建立':'资料核验已通过，全文证据提取已交给后台处理');scheduleExtractionPoll();return; }
  const count=reviews.reduce((total,r)=>total+(r.evidence_ids?.length||0),0);
  $('#dialog').close();toast(count?`核验已通过，已入库${count}条证据`:'核验已通过，未提取到可入库证据');
}
function showVerificationReviews(reviews,errors=[]){
  verificationReviews=reviews;
  const pending=reviews.filter(r=>r.status==='pending'),actionable=reviews.some(verificationCanApprove);
  dialog(reviews.length>1?`批量核验资料 · ${reviews.length}份`:'核验资料',`${verificationMessages(errors)}<form id="verification-decision-form">${reviews.map(r=>verificationReviewCard(r,reviews.length>1)).join('')}<p class="description">通过后自动切分全文并建立检索索引。资料核验不代表项目已批复或已实施。</p><div id="verification-results" role="status"></div><div class="dialog-actions"><button type="button" class="secondary" data-action="refresh-verifications" ${reviews.some(verificationExtracting)?'':'hidden'}>刷新提取状态</button>${actionable?`<button type="button" class="secondary" data-action="reject-verifications" ${pending.length?'':'disabled'}>暂不通过</button><button type="submit" class="primary">${pending.length?(reviews.length>1?'全部通过':'通过核验'):'重试提取'}</button>`:''}</div></form>`);
  $('#verification-decision-form').verificationReviews=reviews;
  $('#verification-decision-form').verificationErrors=errors;
}
async function openVerification(industry,id){const review=await api('/verifications/'+encodeURIComponent(industry)+'/'+encodeURIComponent(id));showVerificationReviews([review]);}
async function refreshVerificationStatus(form){
  if(!form||form.dataset.saving==='true')return;
  form.dataset.saving='true';const controls=[...form.querySelectorAll('button')];controls.forEach(b=>b.disabled=true);
  try{
    const reviews=form.verificationReviews||[],active=reviews.filter(verificationExtracting),errors=[...(form.verificationErrors||[])];
    const results=await Promise.allSettled(active.map(r=>api('/verifications/'+encodeURIComponent(r.industry_id)+'/'+encodeURIComponent(r.id))));
    results.forEach((result,index)=>result.status==='fulfilled'?Object.assign(active[index],result.value):errors.push({title:active[index].title,error:result.reason.message}));
    if(form.isConnected){showVerificationReviews(reviews,errors);finishManualVerification($('#verification-decision-form'),reviews,errors);}
    if(S.view==='materials')await render();
  }finally{form.dataset.saving='false';controls.forEach(b=>b.disabled=false);}
}
async function showVerificationQueue(){
  const result=await api('/verifications?'+new URLSearchParams({industry:S.industry,status:'pending'}));
  verificationQueue=Array.isArray(result)?result:result.items||[];
  dialog('待核验资料',`<p class="description">选择资料确认通过，系统自动建立全文检索索引。</p><div class="verification-queue">${verificationQueue.map((r,index)=>`<div class="report-row"><label><input name="verification-pick" type="checkbox" value="${index}"> ${esc(r.title||r.material_id)}</label><button class="secondary" data-action="open-verification" data-id="${esc(r.id)}" data-industry="${esc(r.industry_id)}">核验资料</button></div>`).join('')||empty('暂无待核验资料','从资料的“核验资料”或“自动核验”开始。')}</div>${verificationQueue.length?'<div class="dialog-actions"><button class="primary" data-action="preview-verifications">核验所选资料</button></div>':''}`);
}
async function previewSelectedVerifications(){
  const items=[...document.querySelectorAll('[name="verification-pick"]')].filter(b=>b.checked).map(b=>verificationQueue[Number(b.value)]);
  if(!items.length){toast('先选择待审核任务');return;}if(items.length>10){toast('每批最多审核10份');return;}
  const results=await Promise.allSettled(items.map(r=>api('/verifications/'+encodeURIComponent(r.industry_id)+'/'+encodeURIComponent(r.id))));
  const reviews=[],errors=[];results.forEach((r,i)=>r.status==='fulfilled'?reviews.push(r.value):errors.push({title:items[i].title,error:r.reason.message}));
  if(reviews.length)showVerificationReviews(reviews,errors);else dialog('核验任务读取失败',verificationMessages(errors));
}
async function submitVerificationDecisions(form,decision){
  if(form.dataset.saving==='true')return;
  const reviews=form.verificationReviews||verificationReviews;
  const requests=reviews.filter(r=>decision==='approve'?verificationCanApprove(r):r.status==='pending');
  if(!requests.length){toast('这些资料已处理');return;}
  form.dataset.saving='true';const controls=[...form.querySelectorAll('button')];controls.forEach(b=>b.disabled=true);
  try{
    for(const review of requests){
      try{
        const result=await api('/verifications/'+encodeURIComponent(review.industry_id)+'/'+encodeURIComponent(review.id)+'/decision','POST',{decision}),saved=result.review||result,expected=decision==='approve'?'approved':'rejected';
        if(saved.status!==expected)throw new Error('未取得核验结果，请重新打开资料查看状态。');
        Object.assign(review,saved);delete review.decision_error;
      }
      catch(error){review.decision_error=error.message;}
      const card=[...form.querySelectorAll('.verification-review')].find(c=>c.dataset.reviewId===review.id&&c.dataset.reviewIndustry===review.industry_id);
      if(card){card.querySelector('[data-review-result]').textContent='';card.querySelector('.verification-findings')?.remove();if(review.extraction_job){const host=document.createElement('div');host.innerHTML=extractionHost(review.extraction_job);card.append(host);scheduleExtractionPoll();}}
    }
    form.querySelector('#verification-results').textContent=reviews.map(r=>`${r.title}：${r.decision_error?'失败，'+r.decision_error:verificationResult(r)}`).join('\n');
    if(S.view==='materials')await render();
    finishManualVerification(form,reviews,form.verificationErrors||[]);
  }finally{
    form.dataset.saving='false';
    const pending=reviews.some(r=>r.status==='pending'),extracting=reviews.some(verificationExtracting);
    controls.forEach(b=>{if(b.dataset.action==='refresh-verifications'){b.hidden=!extracting;b.disabled=false;}else b.disabled=b.dataset.action==='reject-verifications'?!pending:!reviews.some(verificationCanApprove);});
    const approve=form.querySelector('[type="submit"]');if(approve)approve.textContent=pending?(reviews.length>1?'全部通过':'通过核验'):reviews.some(verificationNeedsExtraction)?'重试提取':extracting?'正在提取证据':decision==='reject'?'暂不通过':'核验已通过';
  }
}
function appendAgentVerification(container,event,industry){
  const review={id:event.id,industry_id:event.industry_id||industry,title:event.title||'资料核验',status:'pending'};
  const card=document.createElement('div');card.className='citation verification-required';
  const title=document.createElement('strong');title.textContent=review.title;
  const question=document.createElement('p');question.textContent='是否通过资料核验？通过后自动建立全文检索索引。';
  const actions=document.createElement('div');actions.className='material-review-actions';
  const approve=document.createElement('button');approve.className='primary';approve.textContent='通过核验并入库';
  const reject=document.createElement('button');reject.className='secondary';reject.textContent='暂不通过';
  const result=document.createElement('p');result.className='description';result.setAttribute('role','status');
  let saving=false;
  const decide=async decision=>{
    if(saving||!(decision==='approve'?verificationCanApprove(review):review.status==='pending'))return;
    saving=true;approve.disabled=true;reject.disabled=true;result.textContent=decision==='approve'?'正在通过核验并建立全文索引…':'正在保存…';
    try{
      const response=await api('/verifications/'+encodeURIComponent(review.industry_id)+'/'+encodeURIComponent(review.id)+'/decision','POST',{decision}),saved=response.review||response;
      if(saved.status!==(decision==='approve'?'approved':'rejected'))throw new Error('未取得核验结果，请重试。');
      Object.assign(review,saved);result.textContent=verificationResult(review);if(review.extraction_job){const host=document.createElement('div');host.innerHTML=extractionHost(review.extraction_job);card.append(host);scheduleExtractionPoll();}if(S.view==='materials')await render();
    }catch(error){result.textContent='操作失败：'+error.message;}
    finally{saving=false;approve.disabled=!verificationCanApprove(review);reject.disabled=review.status!=='pending';approve.textContent=verificationNeedsExtraction(review)?'重试提取':review.status==='approved'?(review.extraction_job||review.extraction_status==='pending'?'核验已通过':'已完成'):'通过核验并入库';}
  };
  approve.onclick=()=>decide('approve');reject.onclick=()=>decide('reject');
  actions.append(approve,reject);card.append(title,question,actions,result);container.append(card);
}
async function viewMaterial(industry,id){
  const r=await api('/materials/'+industry+'/'+id);
  const fields=[['归属档案',industryName(r.industry_id)],['类型',r.kind_label],['编号',r.number],['版本',r.version],['地域',r.region],['发布机构',r.issuer],['有效性',materialValidity(r)],['核验状态',materialVerificationText(r)],['发布日期',r.published_date],['实施日期',r.effective_date],['资料核对日期',r.checked_date||'尚未核对'],['复核日期',r.review_after],['项目阶段',r.project_stage],['调查年度/时相',r.coverage_period],['比例尺',r.scale],['坐标系',r.coordinate_system],['影像分辨率',r.resolution],['模型引用',r.model_allowed?'允许引用片段':'仅本地管理'],['原件大小',r.attachment?.bytes?(r.attachment.bytes/1000000).toFixed(1)+' MB':''],['提取正文',r.extraction?.text_index?r.extraction.characters+' 字符 / '+r.extraction.sections+' 个分段':''],['段落与表格',r.extraction?.paragraphs?r.extraction.paragraphs+' 段落 / '+r.extraction.tables+' 表格':'']];
  dialog(r.title,`<div class="detail-meta">${fields.filter(([,v])=>v).map(([k,v])=>`<div>${esc(k)}<strong>${esc(v)}</strong></div>`).join('')}</div>${r.review_due?'<p class="notice">已到复核期限，需重新核对原文。</p>':''}<div class="detail-summary">${esc(r.summary)}</div>${r.url?`<a href="${esc(safeURL(r.url))}" target="_blank" rel="noopener noreferrer">原始来源 ↗</a>`:''}<p class="description">${esc(r.notes)} ${esc(r.extraction?.note||'')}</p>${r.related_evidence_ids?.length?`<p class="description">关联证据：${r.related_evidence_ids.map(eid=>`<button class="link-button" data-action="view-related-evidence" data-id="${esc(eid)}" data-industry="${esc(industry)}">${esc(eid)}</button>`).join('、')}</p>`:''}<details><summary>查看正文 / 可引用位置</summary><pre class="material-body">${esc(r.content||'尚未提供正文')}</pre></details>${materialExtractionHost(r,true)}<div class="material-review-actions">${materialVerificationAction(r,true)}<button class="secondary" id="verify-material-automatic">自动核验</button><button class="secondary" data-action="verification-queue">待核验资料</button></div><div class="dialog-actions">${r.has_attachment?`<a class="secondary" href="/api/materials/${encodeURIComponent(industry)}/${encodeURIComponent(id)}/file">下载原附件</a>`:''}${r.extraction?.text_index?'<button class="secondary" id="read-material-sections">检索分段原文</button>':''}<button class="primary" id="edit-material">编辑资料</button></div>`);
  $('#edit-material').onclick=()=>materialDialog(r);
  if($('#verify-material-manual'))$('#verify-material-manual').onclick=()=>prepareVerification([r],'manual').catch(handleError);
  $('#verify-material-automatic').onclick=()=>prepareVerification([r],'automatic').catch(handleError);
  if(r.extraction?.text_index)$('#read-material-sections').onclick=()=>showMaterialSections(industry,id,r.title).catch(handleError);
}
async function renderMemory(){
  const ticket=S.renderSeq;
  const industry=S.industry, [memories,conversations]=await Promise.all([api('/memories?'+new URLSearchParams({industry})),api('/conversations?'+new URLSearchParams({industry}))]);
  if(ticket!==S.renderSeq||S.view!=='settings'||S.industry!==industry)return;
  conversationRecords=conversations;
  $('#settings-memory').innerHTML=`<div class="actions"><button class="secondary" data-action="add-memory">＋ 保存偏好</button></div>`+`<div class="panel"><label><input id="save-conversations" type="checkbox" ${S.data.knowledge.save_conversations?'checked':''}>保存今后的历史对话到本机</label><p class="description">默认关闭；开启后保存提问、回答及图表引用短摘录，不保存完整工具原文。记忆需单独勾选允许模型引用。</p></div><div class="panel"><h2>长期记忆</h2>${memories.map(r=>`<div class="report-row"><div><h3>${esc(r.title)}</h3><p>${esc(r.content)}</p><small>${r.model_allowed?'供模型参考':'仅本地保存'} · ${esc(r.updated_at)}</small></div><button class="link-button" data-action="archive-memory" data-id="${r.id}">停用</button></div>`).join('')||empty('尚无已保存偏好','可记住关注地区、回答格式、面积单位和项目背景。')}</div><div class="panel"><h2>历史对话</h2>${conversations.map((c,i)=>`<div class="report-row"><div><h3>${esc(c.turns[0]?.prompt.slice(0,70)||'研究对话')}</h3><small>${esc(c.updated_at)} · ${c.turns.length}轮</small></div><button class="link-button" data-action="view-conversation" data-id="${i}">阅读</button></div>`).join('')||empty('暂无历史对话','开启保存后，新完成的对话会出现在这里。')}</div>`;
}
function memoryDialog(){dialog('记住用户偏好',`<form id="memory-form"><label class="field">标题<input name="title" required maxlength="100" placeholder="例如：研究与回答偏好"></label><label class="field">内容<textarea name="content" required maxlength="3000" placeholder="例如：关注当前研究地区，面积同时使用亩和公顷，优先引用官方原文。"></textarea></label><label class="field">范围<select name="industry">${options([['all','通用偏好'],...S.data.industries.map(i=>[i.id,i.name])],S.industry)}</select></label><label><input name="model_allowed" type="checkbox">允许研究助手读取这条记忆</label><div class="dialog-actions"><button class="primary">保存记忆</button></div></form>`);}
function networkIndustry(title){
  if(S.industry!=='all')return true;
  $('#research-tool-content').innerHTML=heading(title,'先选择一个行业，搜索和采集将遵守该行业的来源边界。')+empty('选择研究行业','使用右上角“浏览范围”选择行业；也可以先新建行业。','<button class="primary" data-action="add-industry">＋ 新建行业</button>');
  return false;
}
function renderWebSearch(){
  if(!networkIndustry('网页搜索'))return;
  const c=S.data.network;
  $('#research-tool-content').innerHTML='<p class="description">搜索结果是线索，读取原文后再核验归档。</p>'+
    `<form id="web-search-form" class="panel"><div class="form-grid"><label class="field full">搜索关键词<input name="query" required maxlength="500" placeholder="行业、机构、政策、产品或财报…" value="${esc(webResults?.query||'')}"></label><label class="field">时间范围<select name="period">${options([['','不限时间'],['d','最近一天'],['w','最近一周'],['m','最近一月'],['y','最近一年']],webOptions.period)}</select></label><label class="field">结果数量<input type="number" name="limit" min="1" max="30" value="${webOptions.limit}"></label></div><div class="actions"><button class="primary" type="submit">搜索网页</button><span class="description">${esc(c.provider==='ddgs'?'免密钥 · DDGS / '+c.backend:'SearXNG 实例')}</span></div></form>`+
    `<div class="notice"><span>⌕</span><div>仅限指定来源时，结果会按启用来源目录过滤。引擎时间筛选需要原文日期核验；没有结果不代表没有资料。</div></div>`+
    (webResults?`<div class="section-caption"><span>${webResults.items.length} 条线索 · ${esc(webResults.backend)}</span><button class="secondary" data-action="crawl-search">采集选中网页</button></div><div class="evidence-list">${webResults.items.map((r,i)=>`<article class="evidence-card web-result"><div class="card-top"><label><input type="checkbox" name="web-pick" value="${i}"> 选择采集</label>${badge('needs_verification',r.preferred?'指定来源 · 待读原文':'搜索线索 · 待读原文')}</div><h3><a href="${esc(safeURL(r.url))}" target="_blank" rel="noopener noreferrer">${esc(r.title)} ↗</a></h3><p class="summary">${esc(r.snippet)}</p><div class="card-foot"><small class="url-text">${esc(r.url)}</small><button class="link-button" data-action="source-from-search" data-id="${i}">加入信源</button></div></article>`).join('')||empty('没有匹配线索','可修改关键词、放宽时间，或检查限定来源。')}</div>`:'');
}
async function renderCrawls(){
  const ticket=S.renderSeq,tool=S.toolSeq;
  if(!networkIndustry('批量采集'))return;
  const industry=S.industry;
  const jobs=await api('/crawls?'+new URLSearchParams({industry}));
  if(tool!==S.toolSeq||ticket!==S.renderSeq||(S.view!=='research'||S.researchTool!=='crawl')||S.industry!==industry)return;
  $('#research-tool-content').innerHTML='<p class="description">从公开网址读取候选资料，再交给助手核验归档。</p>'+
    `<form id="crawl-form" class="panel"><label class="field">入口URL（每行一个）<textarea name="urls" rows="3" placeholder="留空使用本行业所有已启用信源；也可粘贴搜索选中的地址">${esc(crawlSeeds)}</textarea></label><div class="form-grid"><label class="field">最多尝试页数<input type="number" name="max_pages" min="1" max="50" value="${crawlOptions.max_pages}"></label><label class="field">跟进深度<select name="max_depth">${options([['0','0 · 只读入口页面'],['1','1 · 入口及下一层链接'],['2','2 · 两层链接'],['3','3 · 三层链接']],crawlOptions.max_depth)}</select></label></div><div class="actions"><button class="primary" type="submit">开始批量采集</button><span class="description">HTML / 文本 / RSS · 遵守 robots.txt · 同站限速</span></div></form>`+
    `<div class="notice"><span>↧</span><div>正文临时保留在内存，重启后需重新读取；PDF、登录页面及动态渲染内容目前不支持。完成后可交给 Agent 分类归档，每轮最多12条。</div></div><div id="crawl-jobs">${jobs.map(j=>crawlCard(j)).join('')||empty('尚无采集任务','填写入口或添加信源后即可开始。')}</div>`;
  if(jobs.some(j=>j.status==='running'))pollCrawls(industry,tool);
}
function crawlCard(j){
  const count=j.counts||{};
  return `<article class="panel crawl-job" data-crawl="${esc(j.id)}"><div class="industry-title"><h2>${esc(crawlLabels[j.status]||j.status)}</h2><div class="actions">${j.status==='running'?`<button class="secondary" data-action="stop-crawl" data-id="${esc(j.id)}">停止采集</button>`:j.items.some(i=>i.state!=='failed'&&i.state!=='duplicate')?`<button class="primary" data-action="organize-crawl" data-id="${esc(j.id)}">Agent 整理归档</button>`:''}</div></div><p class="description">${esc(j.started_at)} · 已尝试 ${j.attempted}/${j.max_pages} 页 · 深度 ${j.max_depth}${j.remaining?' · 未访问队列 '+j.remaining+' 项':''}</p><div class="category-tags">${['new','changed','unchanged','duplicate','failed'].map(k=>`<span class="tag">${esc(crawlLabels[k]||'失败')} ${count[k]||0}</span>`).join('')}</div>${j.error?`<div class="form-error">${esc(j.error)}</div>`:''}<details ${j.status==='running'?'open':''}><summary>查看采集结果与失败原因</summary>${j.items.map(i=>`<div class="crawl-item"><div>${badge(i.state,i.state==='failed'?'读取失败':crawlLabels[i.state])} <a href="${esc(safeURL(i.url))}" target="_blank" rel="noopener noreferrer">${esc(i.title||i.url)} ↗</a></div><p>${esc(i.error||i.excerpt||'')}${i.truncated?'（正文截短）':''}</p><small>${esc(i.url)}</small></div>`).join('')||'<p class="description">正在读取入口…</p>'}</details></article>`;
}
function pollCrawls(industry,tool=S.toolSeq){
  if(!S.toolsOpen)return;
  crawlPoll=setTimeout(async()=>{try{if(!S.toolsOpen||tool!==S.toolSeq||(S.view!=='research'||S.researchTool!=='crawl')||S.industry!==industry)return;const jobs=await api('/crawls?'+new URLSearchParams({industry}));if(!S.toolsOpen||tool!==S.toolSeq||(S.view!=='research'||S.researchTool!=='crawl')||S.industry!==industry)return;if(!$('#crawl-jobs'))return;$('#crawl-jobs').innerHTML=jobs.map(crawlCard).join('');if(jobs.some(j=>j.status==='running'))pollCrawls(industry,tool);}catch(error){handleError(error);}},2000);
}
async function renderEvidence(){
  const ticket=S.renderSeq,industry=S.industry;
  const query=new URLSearchParams({industry:S.industry,q:S.q,category:S.category,freshness:S.freshness,status:S.status,review_only:S.reviewOnly?'true':'false'});
  const records=await api('/evidence?'+query);
  if(ticket!==S.renderSeq||industry!==S.industry)return;
  S.records=records;
  const selected=researchProfiles();
  const cats=[...new Set([...selected.flatMap(i=>i.scope.topics||[]),...records.map(r=>r.category)])];
  const review=S.reviewOnly;
  $('#content').innerHTML=libraryHeader()+librarySearch()+`<div class="library-actions"><label><input id="review-only" type="checkbox" ${review?'checked':''}> 只看待核验与需复核</label><button class="link-button" data-action="manual-evidence">＋ 录入知识证据</button></div><div class="filter-row"><div class="tabs">${[['','全部证据'],['confirmed','已核验'],['reported','来源声称'],['planned','计划 / 目标']].map(([state,label])=>`<button class="tab ${S.status===state?'active':''}" data-status="${state}">${label}</button>`).join('')}</div><div class="filters"><select id="category-filter" aria-label="分类筛选">${options([['','全部分类'],...cats.map(c=>[c,c])],S.category)}</select><select id="freshness-filter" aria-label="时效筛选">${options([['','全部时效'],['current','未到复核期限'],['review_due','待复核'],['needs_verification','待核验'],['expired','已过有效期'],['superseded','已被替代']],S.freshness)}</select></div></div><div class="section-caption"><span>${review?'复核清单':'证据记录'} · ${records.length} 条匹配</span><span>核验日期 ↓ · 最多展示100条</span></div><div class="evidence-list">${records.length?records.map(r=>`<article class="evidence-card" data-record="${esc(r.id)}" data-industry="${esc(r.industry_id)}" tabindex="0" role="button" aria-label="查看 ${esc(r.title)}"><div class="card-top"><div class="category"><span>${esc(r.category)}</span><span>/ ${esc(industryName(r.industry_id))}</span></div>${badge(r.status,labels[r.status])}</div><h3>${esc(r.title)}</h3><p class="summary">${esc(r.summary)}</p><div class="card-foot"><div class="card-foot-left"><span>◈ ${esc(r.publisher)}</span><span>核验 ${esc(r.checked_date)}</span>${r.freshness!=='current'?badge(r.freshness,r.freshness_label):''}</div><div class="actions">${(r.tags||[]).slice(0,1).map(t=>`<span class="tag">${esc(t)}</span>`).join('')}${r.url?`<a href="${esc(safeURL(r.url))}" target="_blank" rel="noopener noreferrer">原始来源 ↗</a>`:r.material_source?'<span class="tag">本地资料证据</span>':''}</div></div></article>`).join(''):empty(review?'没有待复核的匹配资料':'没有匹配的证据',review?'未到复核期限不代表事实持续成立。':'试试减少关键词，或先建立行业并添加公开来源。')}</div>`;
}
function renderIndustries(){
  $('#settings-archives').innerHTML=`<div class="actions"><button class="secondary" data-action="add-industry">＋ 新建档案</button><button class="secondary" data-action="add-forestry-region">＋ 新增林草地区</button></div>`+
  `<div class="notice"><span>◫</span><div>默认行业：<strong>${S.data.active_industry?esc(industryName(S.data.active_industry)):'暂未选择'}</strong>。档案选择用于查阅和研究；采集开关与定时调度分别管理。</div></div>`+
  (S.data.industries.length?S.data.industries.map(i=>`<article class="industry-card"><div class="industry-title"><h2>${esc(i.name)} ${i.id===S.data.active_industry?badge('current','默认行业'):''}</h2>${badge(i.enabled?'current':'superseded',i.enabled?'采集开关已开':'采集开关已关')}</div><div class="industry-meta">${esc(i.scope.primary_region)} · ${i.evidence_count}条证据 · 连续成功覆盖：${esc(i.update_state.covered_through||'尚无完整扫描记录')}</div><div class="category-tags">${(i.scope.topics||[]).map(c=>`<span class="tag">${esc(c)}</span>`).join('')}</div><div class="actions"><button class="secondary" data-action="browse-industry" data-id="${esc(i.id)}">查看资料</button><button class="secondary" data-action="scope" data-id="${esc(i.id)}">编辑范围与分类</button><button class="link-button" data-action="set-active" data-id="${esc(i.id)}">设为默认</button><span class="industry-meta">每日采集</span><label class="switch"><input type="checkbox" data-industry-enabled="${esc(i.id)}" ${i.enabled?'checked':''} aria-label="启用${esc(i.name)}每日采集"><span></span></label></div></article>`).join(''):empty('创建第一个研究行业','确定地区和重点问题后，再启用自动采集。'));
}
async function renderSources(){
  const id=chooseIndustry();
  if(!id){$('#settings-sources').innerHTML=empty('先创建一个行业','信息来源按行业独立管理。','<button class="primary" data-action="add-industry">新建行业</button>');return;}
  const ticket=S.renderSeq,industry=S.industry,catalog=await api('/sources/'+id);
  if(ticket!==S.renderSeq||industry!==S.industry||S.view!=='settings')return;
  S.sources=catalog;S.sourceIndustry=id;
  $('#settings-sources').innerHTML=`<div class="section-caption"><span>当前档案：${esc(industryName(id))}</span><button class="secondary" data-action="add-source">＋ 添加信源</button></div>`+
  `<div class="panel"><h2>采集边界</h2><p class="description">优先使用原始公告、监管文件和正式披露。</p><label class="field">来源模式<select id="source-mode">${options([['preferred','指定来源优先，允许发现其他公开原始来源'],['only','仅使用指定来源入口及其允许目录中的原文']],S.sources.mode||'preferred')}</select></label></div><div class="panel"><h2>公开信息入口 <span class="badge grey">${S.sources.sources.length}</span></h2><p class="description">优先级1最高；暂停来源不会删除已入库证据。</p>${S.sources.sources.length?`<table class="source-table"><thead><tr><th>来源 / 地址</th><th>优先级</th><th>状态</th><th>启用</th></tr></thead><tbody>${S.sources.sources.map(source=>`<tr><td><button class="link-button" data-action="edit-source" data-id="${esc(source.id)}">${esc(source.name)}</button><small>${esc(source.url)}</small></td><td>${source.priority||3}</td><td>${badge(source.verified?'current':'needs_verification',source.verified?'有核验记录':'待核验')}</td><td><label class="switch"><input type="checkbox" data-source-enabled="${esc(source.id)}" ${source.enabled!==false?'checked':''} aria-label="启用${esc(source.name)}"><span></span></label></td></tr>`).join('')}</tbody></table>`:empty('还没有指定来源','添加官网、监管公告或财报公开入口。')}</div>`;
}
async function renderReports(){
  const ticket=S.renderSeq,tool=S.toolSeq,all=await api('/reports');
  const reports=all.filter(r=>!/reports[\\/]maintenance[\\/]|reports[\\/]plans[\\/]/.test(r.path));
  if(ticket!==S.renderSeq||tool!==S.toolSeq||S.view!=='research'||S.researchTool!=='outputs')return;
  $('#research-tool-content').innerHTML=`<div class="actions"><button class="secondary" data-action="report">＋ 生成证据简报</button></div>`+`<div class="notice"><span>▧</span><div>证据简报由本地资料汇编，不会自动宣称联网核验成功。列表包含各档案的报告，使用前请核对适用范围。</div></div><div class="reports-list">${reports.map(r=>`<div class="report-row"><div><h3>${esc(r.title)}</h3><small>${esc(r.path)}</small></div><div class="actions"><button class="link-button" data-action="open-doc" data-path="${esc(r.path)}">阅读 ↗</button><a class="link-button" href="/api/download?${new URLSearchParams({path:r.path})}">下载</a></div></div>`).join('')||empty('暂无报告','可以先生成一份带来源和核验日期的本地简报。')}</div>`;
}
async function renderSettings(){
  $('#content').innerHTML=heading('设置','模型、资料范围与授权集中在这里管理。')+`<div class="settings-stack"><details class="settings-section"><summary>模型与助手</summary><div id="settings-model"></div></details><details class="settings-section"><summary>档案与范围</summary><div id="settings-archives"></div></details><details class="settings-section"><summary>信息来源</summary><div id="settings-sources"></div></details><details class="settings-section"><summary>偏好与对话保存</summary><div id="settings-memory"></div></details></div>`;
  const c=S.data.agent;
  $('#settings-model').innerHTML=`<div class="panel"><h2>模型连接</h2><p class="description">使用支持工具调用的模型；API协议必须与服务端匹配。</p><form id="settings-form"><div class="form-grid"><label class="field">Provider ID<input name="provider_id" required value="${esc(c.provider_id)}"></label><label class="field">显示名称<input name="display_name" required value="${esc(c.display_name)}"></label><label class="field full">API Base URL<input name="base_url" type="url" required value="${esc(c.base_url)}" placeholder="http://127.0.0.1:8091/v1"></label><label class="field">API协议<select name="protocol">${options([['chat_completions','OpenAI Chat Completions'],['responses','OpenAI Responses']],c.protocol)}</select></label><label class="field">模型 ID<input name="model" required value="${esc(c.model)}"></label><label class="field full">API 密钥<input name="api_key" type="password" autocomplete="off" placeholder="${c.key_configured?'已有服务端密钥，留空保留':'输入密钥；本机接口可填 local'}"><small>输入后仅在服务端内存使用，不写浏览器存储或配置文件。重启时可通过 KB_MODEL_API_KEY 环境变量加载。</small></label><label class="field">每轮模型请求上限<input name="request_limit" type="number" min="1" max="30" value="${c.request_limit}"></label><label class="field">单轮超时（秒）<input name="timeout_seconds" type="number" min="30" max="900" value="${c.timeout_seconds}"></label></div><div class="provider-note">本机接口使用当前模型；厂商API会接收本次问答所用的证据片段。资料文件仍保存在本地。工具包括本地检索、网页搜索、批量采集、公开原文读取、验证入库和简报。</div><div class="actions"><button class="primary" type="submit">保存连接</button><button class="secondary" type="button" id="test-model">测试模型列表</button><a class="link-button" href="https://github.com/pydantic/pydantic-ai" target="_blank" rel="noopener noreferrer">SDK 源码 ↗</a></div><div id="connection-result" class="connection-result"></div></form></div><div class="panel"><h2>每天更新</h2><p class="description">已配置的Codex定时任务：${esc(S.data.automation.schedule)}，北京时间。运行时读取行业开关；电脑和Codex应用需运行。本页面的Agent按按钮启动，避免重复建立采集调度。</p><div class="actions">${badge(S.data.automation.status==='PAUSED'?'superseded':'current',S.data.automation.status)}</div></div>`;
  const n=S.data.network;
  $('#settings-model').innerHTML+=`<div class="panel"><h2>网页搜索后端</h2><p class="description">默认免密钥DDGS。也可接入支持JSON搜索的自有SearXNG实例；引擎不可用时可以切换，不会伪造结果。</p><form id="network-settings-form"><div class="form-grid"><label class="field">搜索服务<select name="provider">${options([['ddgs','DDGS · 无需API密钥'],['searxng','SearXNG · 自有实例']],n.provider)}</select></label><label class="field">DDGS搜索引擎<select name="backend">${options([['bing','Bing'],['duckduckgo','DuckDuckGo'],['brave','Brave']],n.backend)}</select></label><label class="field full">SearXNG Base URL<input name="searxng_url" type="url" value="${esc(n.searxng_url)}" placeholder="http://127.0.0.1:8080"></label><label class="field">搜索地区<select name="region">${options([['cn-zh','中国 · 中文'],['us-en','美国 · 英文'],['wt-wt','全球']],n.region)}</select></label></div><button class="primary" type="submit">保存搜索后端</button></form></div>`;
  renderIndustries();
  await Promise.all([renderSources(),renderMemory()]);
}
function addIndustry(){dialog('新建研究行业',`<form id="industry-form"><div class="form-grid"><label class="field">行业名称<input name="name" placeholder="例如：半导体" required></label><label class="field">行业编号<input name="id" placeholder="例如：semiconductor" required pattern="[a-z0-9][a-z0-9_-]{2,79}"><small>小写英文、数字和连字符，至少3位。</small></label><label class="field full">主要地区<input name="region" value="中国" required></label><label class="field full">分类（逗号或换行分隔）<textarea name="categories" placeholder="政策与监管，企业与竞争，产品与技术，供应链，市场与财务，风险"></textarea></label></div><div class="provider-note">新行业独立归档，自动采集默认暂停。准备好范围和来源后再开启。</div><div class="dialog-actions"><button class="primary" type="submit">创建行业</button></div></form>`);}

function addForestryRegion(){
  const id='forestry-'+crypto.randomUUID().replaceAll('-','').slice(0,12);
  dialog('新增林草地区档案',`<form id="forestry-region-form"><input type="hidden" name="id" value="${id}"><label class="field">地区<input name="region" required maxlength="100" placeholder="省份、城市、区域或全国"></label><label class="field">档案名称（可选）<input name="name" maxlength="100" placeholder="默认：地区名称＋林草"></label><label><input type="checkbox" name="active" checked>设为默认研究档案</label><p class="description">自动沿用林草分类、复核周期和地区关键词，并引用全国基础资料。地方政策、项目、信源与采集记录独立保存；每日采集默认暂停。</p><div class="dialog-actions"><button class="primary" type="submit">创建地区档案</button></div></form>`);
}

function editScope(id){const entry=S.data.industries.find(i=>i.id===id), c=entry.scope;dialog('编辑 '+entry.name+' 的研究范围',`<form id="scope-form" data-id="${esc(id)}"><div class="form-grid"><label class="field">主要地区<input name="primary_region" value="${esc(c.primary_region)}" required></label><label class="field full">同时研究的地区（逗号分隔）<input name="research_regions" value="${esc((c.research_regions?.length>1?c.research_regions:[]).join('，'))}" placeholder="留空只研究主要地区"><small>保留原资料真实地区；模板关键词随研究范围调整，自定义关键词保留。当地信源需要另外管理。</small></label><label class="field">对照地区<input name="comparison_regions" value="${esc((c.comparison_regions||[]).join('，'))}"></label><label class="field full">分类<textarea name="topics" required>${esc(c.topics.join('，'))}</textarea><small>修改分类不自动重写已有事实；旧证据可单独编辑归档。</small></label><label class="field">默认复核周期（天）<input name="default_days" type="number" min="1" max="36500" value="${c.freshness?.default_days||30}"></label><label class="field">最近检索范围（天）<input name="lookback_days" type="number" min="1" value="${c.lookback_days}"></label><label class="field full">按分类设置复核周期（JSON对象）<textarea name="category_days">${esc(JSON.stringify(c.freshness?.category_days||{},null,2))}</textarea></label></div><div class="dialog-actions"><button class="primary" type="submit">保存范围</button></div></form>`);}
function sourceDialog(id){const c=S.sources.sources.find(s=>s.id===id)||{};dialog(id?'编辑信息来源':'添加公开来源',`<form id="source-form" data-industry="${esc(S.sourceIndustry||chooseIndustry())}" data-original="${esc(id||'')}"><div class="form-grid"><label class="field">名称<input name="name" required value="${esc(c.name)}"></label><label class="field">来源编号<input name="id" required pattern="[a-z0-9][a-z0-9_-]{2,79}" value="${esc(c.id)}" ${id?'readonly':''}></label><label class="field full">公开入口URL<input type="url" name="url" required value="${esc(c.url)}"></label><label class="field">来源类型<select name="type">${options([['primary','官方原始来源'],['government','政府与监管'],['company','企业公告'],['agency_report','机构报告'],['media','媒体线索']],c.type||'primary')}</select></label><label class="field">优先级<input type="number" name="priority" min="1" max="5" value="${c.priority||1}"></label><label class="field full">适用分类<input name="categories" value="${esc((c.categories||[]).join('，'))}" placeholder="逗号分隔"></label><label class="field full">备注<textarea name="notes">${esc(c.notes)}</textarea></label></div><div class="provider-note">新增或更改地址会标记为待核验。暂停来源不会删除旧证据。</div><div class="dialog-actions"><button class="primary" type="submit">保存来源</button></div></form>`);$('#source-form').sourceCatalog=structuredClone(S.sources);}
async function showRecord(industry,id){
  const r=await api(`/evidence/${encodeURIComponent(industry)}/${encodeURIComponent(id)}`),source=r.material_source||r.related_material,history=r.history||[];
  dialog(r.title,`<div class="actions">${badge(r.status,labels[r.status])}${badge(r.freshness,r.freshness_label)}</div><div class="detail-meta"><div>发布机构<strong>${esc(r.publisher)}</strong></div><div>主分类<strong>${esc(r.category)}</strong></div><div>发布日期<strong>${esc(r.published_date||'未知')}</strong></div><div>事件日期<strong>${esc(r.event_date||'未知 / 不适用')}</strong></div><div>核验日期<strong>${esc(r.checked_date)}</strong></div><div>复核期限<strong>${esc(r.review_due_date)}</strong></div></div><div class="detail-summary">${esc(r.summary)}</div>${r.url?`<p><a href="${esc(safeURL(r.url))}" target="_blank" rel="noopener noreferrer">阅读原始来源 ↗</a></p>`:''}${source?`<div class="verification-origin"><strong>本地原始资料</strong><p>引用位置：${esc(source.citation)}</p><pre class="material-body">${esc(source.quote)}</pre><button class="secondary" id="open-evidence-material">打开关联资料</button><button class="secondary" id="locate-evidence-material">定位原文片段</button></div>`:''}<div class="detail-limit"><strong>适用范围与限制</strong><br>${esc(r.limitations||'未填写，需补充。')}</div>${r.metrics?.length?`<details><summary>数字与统计口径 (${r.metrics.length})</summary><pre class="json">${esc(JSON.stringify(r.metrics,null,2))}</pre></details>`:''}<details><summary>历史版本 (${history.length})</summary>${history.length?history.map(h=>`<p>${esc(h.updated_at)} · ${esc(labels[h.status])}</p><div class="detail-limit">${esc(h.summary)}</div>`).join(''):'<p>尚无修订，原记录仍保留。</p>'}</details><div class="dialog-actions"><button class="secondary" id="edit-evidence">编辑归档信息</button><button class="primary" id="ask-record">询问这条证据</button></div>`);
  if(source){
    $('#open-evidence-material').onclick=()=>viewMaterial(source.industry_id,source.material_id).catch(handleError);
    $('#locate-evidence-material').onclick=async()=>{try{const material=await api('/materials/'+encodeURIComponent(source.industry_id)+'/'+encodeURIComponent(source.material_id));if(material.extraction?.text_index){const block=/#block-(\d+)/.exec(source.citation||'');await showMaterialSections(source.industry_id,source.material_id,material.title,'',block?Math.max(0,Number(block[1])-1):0);}else{await viewMaterial(source.industry_id,source.material_id);const notice=document.createElement('div');notice.className='verification-origin';notice.textContent=`引用位置：${source.citation}\n${source.quote}`;$('#dialog-body').prepend(notice);}}catch(error){handleError(error);}};
  }
  const supports=r.supporting_sources||r.supports||source?.supports||[];
  if(supports.length){
    const section=document.createElement('section');section.className='verification-origin';
    section.innerHTML=`<strong>关联原文引用 (${supports.length})</strong>${supports.map((support,index)=>`<div class="evidence-support"><p>引用位置：${esc(support.citation||support.location||support.unit_id||'原文位置')}</p><pre class="material-body">${esc(support.quote||support.text||'')}</pre><button class="secondary" data-support-index="${index}">查看原文</button></div>`).join('')}`;
    $('#dialog-body').append(section);
    section.querySelectorAll('[data-support-index]').forEach(button=>{button.onclick=async()=>{try{const support=supports[Number(button.dataset.supportIndex)],originIndustry=support.industry_id||source?.industry_id||industry,materialId=support.material_id||source?.material_id||r.material_id;if(!materialId){toast('此引用未关联资料目录');return;}const original=await api('/materials/'+encodeURIComponent(originIndustry)+'/'+encodeURIComponent(materialId)),block=/#block-(\d+)/.exec(support.citation||'');if(original.extraction?.text_index)await showMaterialSections(originIndustry,materialId,original.title,block?'':String(support.quote||support.text||'').slice(0,60),block?Math.max(0,Number(block[1])-1):0);else await viewMaterial(originIndustry,materialId);}catch(error){handleError(error);}};});
  }
  $('#edit-evidence').onclick=()=>manualEvidence(r,industry);
  $('#ask-record').onclick=()=>{$('#dialog').close();$('#chat-input').value=`解释证据 ${r.id}：${r.title}，说明原始来源、核验日期、限制和可继续调查的问题。`;openAgent();$('#chat-input').focus();};
}
function manualEvidence(record=null,industry=chooseIndustry()){
  if(!industry){toast('请先建立一个行业');return;}
  const clean=record?Object.fromEntries(Object.entries(record).filter(([k])=>!['industry_id','freshness','freshness_label','review_due_date','as_of','path','history'].includes(k))):{title:'',summary:'',url:'',publisher:'',region:'中国',category:S.data.industries.find(i=>i.id===industry).scope.topics[0],status:'needs_verification',checked_date:S.data.as_of,published_date:null,event_date:null,tags:[],limitations:'',metrics:[]};
  dialog('录入证据 · '+industryName(industry),`<form id="evidence-form" data-industry="${esc(industry)}"><label class="field">证据JSON<textarea name="record" required rows="18">${esc(JSON.stringify(clean,null,2))}</textarea><small>核验日期必须来自真实核对。修订已有事实应保留id；保存后自动留历史。</small></label><div class="dialog-actions"><button class="primary" type="submit">校验并保存</button></div></form>`);
}
async function openDoc(path){const doc=await api('/document?'+new URLSearchParams({path}));dialog('阅读报告',`<div class="doc-content">${esc(doc.text)}</div><div class="dialog-actions"><a class="primary" href="/api/download?${new URLSearchParams({path})}">下载 Markdown</a></div>`);}
function appendMessage(role,text){const el=document.createElement('div');el.className='message '+role;el.innerHTML=`<div class="message-label">${role==='user'?'你':'✳ 研究助手'}</div><div class="message-content">${esc(text)}</div>`;$('#chat-log').append(el);$('#chat-log').scrollTop=$('#chat-log').scrollHeight;return el;}
async function sendAgent(crawlId=null){
  const prompt=$('#chat-input').value.trim();if(!prompt||S.job||S.sending)return;
  const research=$('#research-mode').checked;
  if(research&&S.industry==='all'){toast('研究归档需要先选择单个行业');return;}
  const industry=S.industry,conversation=S.conversation,allowSourceChanges=$('#source-changes').checked;
  S.sending=true;$('#send-agent').disabled=true;
  $('.agent-welcome')?.remove();appendMessage('user',prompt);$('#chat-input').value='';
  const answer=appendMessage('assistant','正在连接模型…');const body=answer.querySelector('.message-content');
  try{
    const job=await api('/agent/jobs','POST',{prompt,industry,research,conversation,allow_source_changes:allowSourceChanges,crawl_id:crawlId});
    if(S.industry===industry)S.conversation=job.conversation;S.job=job.id;$('#send-agent').disabled=true;$('#stop-agent').hidden=false;body.innerHTML='';
    const eventLog=document.createElement('div'),figures=document.createElement('div'),text=document.createElement('div'),seenCharts=new Set();figures.className='research-figures';text.className='answer-markdown';body.append(eventLog,figures,text);
    S.stream=new EventSource('/api/agent/jobs/'+job.id+'/events');
    S.stream.addEventListener('update',async e=>{
      const event=JSON.parse(e.data);
      if(['tool','status','archived','tool_error','error'].includes(event.type)){const line=document.createElement('div');line.className='tool-event'+(['error','tool_error'].includes(event.type)?' error':'');line.textContent=event.title+(event.detail?' · '+event.detail:'');eventLog.append(line);}
      if(event.type==='citations'){event.items.slice(0,4).forEach(c=>{const link=document.createElement('a');link.className='citation';link.href=safeURL(c.url);link.target='_blank';link.rel='noopener noreferrer';link.innerHTML=esc(c.title)+`<small>核验 ${esc(c.checked_date)} · ${esc(c.freshness_label)}</small>`;eventLog.append(link);});}
      if(event.type==='chart'){const key=String(event.seq??JSON.stringify(event.chart));if(!seenCharts.has(key)&&seenCharts.size<3){seenCharts.add(key);appendResearchChart(figures,event.chart);}}
      if(event.type==='answer')text.innerHTML=markdown(event.text);
      if(event.type==='report'){const button=document.createElement('button');button.className='citation';button.textContent='阅读生成的证据简报 ↗';button.onclick=()=>openDoc(event.path).catch(handleError);eventLog.append(button);}
      if(event.type==='verification_required')appendAgentVerification(eventLog,event,industry);
      if(event.type==='done'){S.stream.close();S.stream=null;S.job=null;$('#send-agent').disabled=false;$('#stop-agent').hidden=true;if(event.archived_ids?.length||event.pending_review_ids?.length)await refresh();}
      $('#chat-log').scrollTop=$('#chat-log').scrollHeight;
    });
    S.stream.onerror=()=>{if(S.job){text.textContent='事件连接中断，正在尝试恢复；可点击停止取消任务。';}};
  }catch(error){body.textContent=error.message;S.job=null;$('#send-agent').disabled=false;$('#stop-agent').hidden=true;}
  finally{S.sending=false;}
}
document.addEventListener('click',async e=>{try{
  const nav=e.target.closest('[data-view]');if(nav){const view=nav.dataset.view;S.view=view;await render();if(S.view===view)$('#content').focus();return;}
  if(e.target.closest('#research-tools-toggle')){await setResearchTools(!S.toolsOpen);return;}
  if(e.target.closest('#close-research-tools')){await setResearchTools(false);return;}
  const tool=e.target.closest('[data-research-tool]');if(tool){S.researchTool=tool.dataset.researchTool;await renderResearchTool();return;}
  const tab=e.target.closest('[data-status]');if(tab){S.status=tab.dataset.status;await render();return;}
  const prompt=e.target.closest('[data-prompt]');if(prompt){$('#chat-input').value=prompt.dataset.prompt;openAgent();$('#chat-input').focus();return;}
  if(e.target.closest('a'))return;
  const card=e.target.closest('[data-record]');if(card){await showRecord(card.dataset.industry,card.dataset.record);return;}
  const action=e.target.closest('[data-action]');if(!action)return;
  const id=action.dataset.id;
  switch(action.dataset.action){
    case'reconnect':await refresh();break;
    case'add-industry':addIndustry();break;
    case'scope':editScope(id);break;
    case'set-active':await api('/industries/'+id,'PUT',{active:true});await refresh();toast('默认行业已更新');break;
    case'browse-industry':S.industry=id;S.view='materials';S.q='';S.category='';S.status='';S.freshness='';S.reviewOnly=false;await refresh();break;
    case'add-source':sourceDialog();break;
    case'edit-source':sourceDialog(id);break;
    case'add-material':materialDialog();break;
    case'add-forestry-region':addForestryRegion();break;
    case'view-material':await viewMaterial(action.dataset.industry,id);break;
    case'view-related-evidence':await showRecord(action.dataset.industry,id);break;
    case'restart-material-extraction':await startMaterialExtraction(action.dataset.industry,id,action);break;
    case'view-extraction':case'refresh-extraction':await openExtraction(action.dataset.industry,id);break;
    case'control-extraction':await controlExtraction(action.dataset.industry,id,action.dataset.control,action);break;
    case'verify-material-manual':case'verify-material-automatic':{const record=materialRecords.find(r=>r.id===id&&r.industry_id===action.dataset.industry);if(record)await prepareVerification([record],action.dataset.action.endsWith('automatic')?'automatic':'manual');break;}
    case'verify-selected-manual':case'verify-selected-automatic':await prepareVerification(selectedMaterials(),action.dataset.action.endsWith('automatic')?'automatic':'manual');break;
    case'verification-queue':await showVerificationQueue();break;
    case'open-verification':await openVerification(action.dataset.industry,id);break;
    case'refresh-verifications':await refreshVerificationStatus($('#verification-decision-form'));break;
    case'preview-verifications':await previewSelectedVerifications();break;
    case'retry-material-extraction':{const review=await api('/verifications/'+encodeURIComponent(action.dataset.industry)+'/'+encodeURIComponent(id));showVerificationReviews([review]);await submitVerificationDecisions($('#verification-decision-form'),'approve');break;}
    case'reject-verifications':await submitVerificationDecisions($('#verification-decision-form'),'reject');break;
    case'professional-prompt':$('#chat-input').value=action.dataset.prompt;openAgent();$('#chat-input').focus();break;
    case'add-memory':memoryDialog();break;
    case'archive-memory':await api('/memories/'+id+'/archive','POST',{});await renderMemory();toast('记忆已停用，记录保留');break;
    case'view-conversation':{const c=conversationRecords[Number(id)];dialog('历史对话',c.turns.map((t,index)=>`<div class="detail-limit"><strong>你 · ${esc(t.at)}</strong><p>${esc(t.prompt)}</p><strong>研究助手</strong><div data-history-chart="${index}"></div><div class="answer-markdown">${markdown(t.answer)}</div></div>`).join(''));document.querySelectorAll('[data-history-chart]').forEach(host=>(c.turns[Number(host.dataset.historyChart)].charts||[]).slice(0,3).forEach(chart=>appendResearchChart(host,chart)));break;}
    case'crawl-search':{const picks=[...document.querySelectorAll('[name="web-pick"]:checked')].map(el=>webResults.items[Number(el.value)].url);if(!picks.length){toast('先勾选要采集的网页');break;}crawlSeeds=picks.join('\n');S.researchTool='crawl';await renderResearchTool();break;}
    case'source-from-search':{const item=webResults.items[Number(id)];const sourceIndustry=S.industry,ticket=S.renderSeq,tool=S.toolSeq;const catalog=await api('/sources/'+sourceIndustry);if(S.industry!==sourceIndustry||ticket!==S.renderSeq||tool!==S.toolSeq)break;S.sources=catalog;S.sourceIndustry=sourceIndustry;sourceDialog();$('#source-form').elements.namedItem('name').value=item.title.slice(0,100);$('#source-form').elements.namedItem('url').value=item.url;$('#source-form').elements.namedItem('id').value='web-'+crypto.randomUUID().slice(0,8);break;}
    case'stop-crawl':await api('/crawls/'+id+'/stop','POST',{});toast('正在停止采集');break;
    case'organize-crawl':{if(S.job){toast('先等待当前Agent任务完成');break;}const industry=S.industry;const job=await api('/crawls/'+id);if(industry!==S.industry||job.industry&&job.industry!==industry){toast('采集任务属于其他档案，请先切回原档案');break;}const items=job.items.filter(i=>i.state!=='failed'&&i.state!=='duplicate').slice(0,12);$('#research-mode').checked=true;S.conversation=null;updateChatScope();$('#chat-input').value='请整理这批已抓取的候选页面。先查看行业范围和现有证据，再逐一使用read_public_source读取原文（有缓存时使用缓存），筛出有价值且属于该行业的事实，忠实摘要、分类并归档，最多12条。同一事实沿用已有id，不要把搜索摘要、入口导航、重复内容或计划当成已实现事实。日期未知填null，数字保留完整口径。最后列原始链接和未解决问题。候选：\n'+items.map(i=>i.title+' '+i.url).join('\n');openAgent();await sendAgent(id);break;}
    case'manual-evidence':manualEvidence();break;
    case'report':{const result=await api('/reports','POST',{industry:S.industry,query:''});toast('简报已生成');await openDoc(result.path);break;}
    case'open-doc':await openDoc(action.dataset.path);break;
  }
}catch(error){handleError(error);}});
document.addEventListener('change',async e=>{try{
  if(e.target.id==='material-select-all'){if(e.target.checked&&materialRecords.length>10){e.target.checked=false;toast('每批最多核验10份，请分别选择');return;}materialSelection=e.target.checked?new Set(materialRecords.map(materialKey)):new Set();document.querySelectorAll('[name="material-pick"]').forEach(b=>b.checked=e.target.checked);syncMaterialSelection();return;}
  if(e.target.getAttribute('name')==='material-pick'){if(e.target.checked&&materialSelection.size>=10){e.target.checked=false;toast('每批最多核验10份');return;}if(e.target.checked)materialSelection.add(e.target.value);else materialSelection.delete(e.target.value);syncMaterialSelection();return;}
  if(e.target.name==='file'&&e.target.closest('#material-form')){const file=e.target.files[0],title=e.target.form.elements.namedItem('title');if(file&&!title.value.trim())title.value=file.name.replace(/\.[^.]+$/,'');return;}
  if(e.target.id==='industry-select'){S.industry=e.target.value;S.category='';S.q='';S.reviewOnly=false;materialKind='';webResults=null;crawlSeeds='';S.conversation=null;updateChatScope();await render();return;}
  if(e.target.id==='category-filter'){S.category=e.target.value;await render();return;}
  if(e.target.id==='freshness-filter'){S.freshness=e.target.value;await render();return;}
  if(e.target.id==='material-kind'){materialKind=e.target.value;await render();return;}
  if(e.target.id==='review-only'){S.reviewOnly=e.target.checked;await render();return;}
  if(e.target.id==='save-conversations'){S.data.knowledge=await api('/knowledge/settings','PUT',{save_conversations:e.target.checked});toast('历史对话设置已保存');return;}
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
    if(formId==='library-search'){S.q=String(values.q||'').trim();await render();}
    if(formId==='memory-form'){await api('/memories','POST',{...values,model_allowed:form.elements.namedItem('model_allowed').checked});$('#dialog').close();await renderMemory();toast('记忆已保存');}
    if(formId==='material-form')await saveMaterialForm(form,values);
    if(formId==='verification-decision-form')await submitVerificationDecisions(form,'approve');
    if(formId==='web-search-form'){const button=form.querySelector('[type="submit"]');button.disabled=true;button.textContent='搜索中…';const industry=S.industry,tool=++S.toolSeq;try{const result=await api('/network/search','POST',{industry,query:values.query,period:values.period||null,limit:Number(values.limit)});if(S.industry===industry&&tool===S.toolSeq&&S.view==='research'&&S.researchTool==='search'){webResults=result;webOptions={period:values.period,limit:Number(values.limit)};renderWebSearch();}}finally{if(button.isConnected){button.disabled=false;button.textContent='搜索网页';}}}
    if(formId==='crawl-form'){crawlSeeds=values.urls;crawlOptions={max_pages:Number(values.max_pages),max_depth:values.max_depth};const industry=S.industry,tool=S.toolSeq;const job=await api('/crawls','POST',{industry,urls:values.urls.split('\n').map(s=>s.trim()).filter(Boolean),max_pages:Number(values.max_pages),max_depth:Number(values.max_depth)});lastCrawlId=job.id;if(industry===S.industry&&tool===S.toolSeq&&S.view==='research'&&S.researchTool==='crawl')await renderCrawls();toast('采集任务已启动');}
    if(formId==='network-settings-form'){S.data.network=await api('/network/config','PUT',values);toast('搜索后端已保存');}
    if(formId==='forestry-region-form'){if(!form.dataset.created){await api('/industries','POST',{id:values.id,name:values.name.trim()||values.region.trim()+'林草',region:values.region.trim(),specialty:'forestry'});form.dataset.created=values.id;}if(form.elements.namedItem('active').checked)await api('/industries/'+form.dataset.created,'PUT',{active:true});S.industry=form.dataset.created;S.view='materials';$('#dialog').close();await refresh();toast('林草地区档案已创建，地方信源待配置，采集暂停');}
    if(formId==='industry-form'){await api('/industries','POST',{...values,categories:split(values.categories).length?split(values.categories):null});S.industry=values.id;$('#dialog').close();await refresh();toast('行业已创建，采集默认暂停');}
    if(formId==='scope-form'){await api('/scope/'+form.dataset.id,'PUT',{primary_region:values.primary_region,research_regions:split(values.research_regions).length?Array.from(new Set([values.primary_region,...split(values.research_regions)])):[values.primary_region],comparison_regions:split(values.comparison_regions),topics:split(values.topics),lookback_days:Number(values.lookback_days),freshness:{default_days:Number(values.default_days),category_days:JSON.parse(values.category_days)}});$('#dialog').close();await refresh();toast('研究范围已保存');}
    if(formId==='source-form'){const original=form.dataset.original;const candidate=structuredClone(form.sourceCatalog||S.sources);const index=candidate.sources.findIndex(s=>s.id===original);const item={...values,priority:Number(values.priority),categories:split(values.categories),enabled:index>=0?candidate.sources[index].enabled!==false:true};if(index>=0)candidate.sources[index]={...candidate.sources[index],...item};else candidate.sources.push(item);const saved=await api('/sources/'+form.dataset.industry,'PUT',candidate);if(form.dataset.industry===S.sourceIndustry)S.sources=saved;$('#dialog').close();await refresh();toast('信息来源已保存');}
    if(formId==='evidence-form'){await api('/evidence/'+form.dataset.industry,'POST',JSON.parse(values.record));$('#dialog').close();await refresh();toast('证据已校验保存');}
    if(formId==='settings-form'){values.request_limit=Number(values.request_limit);values.timeout_seconds=Number(values.timeout_seconds);if(!values.api_key)delete values.api_key;const result=await api('/agent/config','PUT',values);S.data.agent=result;updateModelLabel(result);form.elements.namedItem('api_key').value='';toast('模型配置已保存');}
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
$('#agent-settings').onclick=()=>{S.view='settings';render().then(()=>{if(S.view==='settings')$('#settings-model').parentElement.open=true;}).catch(handleError);};
$('#new-chat').onclick=()=>{if(S.job||S.sending){toast('请先停止当前任务');return;}S.conversation=null;$('#chat-log').innerHTML='';appendMessage('assistant','已开始新的研究对话。');};
document.addEventListener('keydown',e=>{
  const drawerOpen=S.view==='research'&&S.toolsOpen&&!$('#dialog').open;
  const fullScreen=drawerOpen&&typeof matchMedia==='function'&&matchMedia('(max-width:760px)').matches;
  if(e.key==='Escape'&&drawerOpen){e.preventDefault();setResearchTools(false).catch(handleError);return;}
  if(e.key==='Tab'&&fullScreen){
    const drawer=$('#research-tools-drawer');
    const controls=[...drawer.querySelectorAll('button,a[href],input,select,textarea,[tabindex]')].filter(el=>!el.disabled&&el.tabIndex>=0&&el.getClientRects().length);
    const first=controls[0],last=controls[controls.length-1],active=document.activeElement;
    if(first&&(!drawer.contains(active)||e.shiftKey&&active===first||!e.shiftKey&&active===last)){
      e.preventDefault();(e.shiftKey?last:first).focus();
    }
  }
  if((e.ctrlKey||e.metaKey)&&e.key==='k'&&!$('#dialog').open){
    const input=fullScreen?($('#research-tool-content textarea')||$('#research-tool-content input')||$('#close-research-tools')):($('#library-query')||(S.view==='research'?$('#chat-input'):null));
    if(input){e.preventDefault();input.focus();}
  }
});
$('#stop-agent').onclick=()=>S.job&&api('/agent/jobs/'+S.job+'/stop','POST',{}).catch(handleError);
document.addEventListener('click',async e=>{if(e.target.id==='test-model'){e.target.disabled=true;try{const result=await api('/agent/test','POST',{});$('#connection-result').textContent=result.message;$('#connection-result').classList.toggle('bad',!result.ok);}catch(error){handleError(error);}finally{e.target.disabled=false;}}});
refresh().catch(localConnectionError);

async function showMaterialSections(industry,id,title,q='',offset=0){
  const result=await api('/materials/'+industry+'/'+id+'/sections?'+new URLSearchParams({q,offset,limit:3}));
  dialog(title+' · 分段原文',`<form id="material-sections-form" class="search-form"><input name="q" aria-label="检索本文关键词" value="${esc(q)}" placeholder="输入正文关键词"><button class="primary" type="submit">检索原文</button></form><p class="description">匹配 ${result.total} 个分段 · 显示 ${result.total?offset+1:0}–${Math.min(offset+3,result.total)}。定位是原始XML的段落、表格行及单元格序号；不是印刷页码，图片未做OCR。</p>${result.items.map(p=>`<h3>分段 ${p.index}</h3><small>${esc(p.first_location)} 至 ${esc(p.last_location)}</small><pre class="material-body">${esc(p.text)}</pre>`).join('')||empty('没有匹配分段','可缩短关键词，或分别检索。')}<div class="dialog-actions">${offset>0?'<button class="secondary" id="section-prev">上一组</button>':''}${offset+3<result.total?'<button class="secondary" id="section-next">下一组</button>':''}<button class="primary" id="section-back">返回资料详情</button></div>`);
  $('#material-sections-form').onsubmit=e=>{e.preventDefault();showMaterialSections(industry,id,title,e.target.elements.namedItem('q').value,0).catch(handleError);};
  if($('#section-prev'))$('#section-prev').onclick=()=>showMaterialSections(industry,id,title,q,Math.max(0,offset-3)).catch(handleError);
  if($('#section-next'))$('#section-next').onclick=()=>showMaterialSections(industry,id,title,q,offset+3).catch(handleError);
  $('#section-back').onclick=()=>viewMaterial(industry,id).catch(handleError);
}
