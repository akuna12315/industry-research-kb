'use strict';

const P={id:null,revision:null,q:'',region:'',tag:'',archived:false,favorite:false,page:null,mutating:false,load:0};
const pageNature={reported:'来源记载',planned:'计划 / 目标',inference:'分析推断',unknown:'待查明'};
const pageOperation={create:'生成页面',layout:'调整展示',refresh:'更新研究',metadata:'页面信息',restore:'恢复历史',copy:'复制页面'};
const pageDate=value=>String(value||'').slice(0,16).replace('T',' ');
async function resumePageDraft(draft){
  if(S.job||S.sending){toast('请等待当前任务完成或先停止任务');return;}
  if(draft.needs_material_selection)return pageComposer({prompt:draft.request||'',industry:draft.industry});
  $('#chat-input').value=`继续生成“${draft.title}”，只处理未完成的场景。`;
  openAgent();
  return sendAgent(null,{generate_page:true,resume_draft_id:draft.id,industry:draft.industry,research:!!draft.research});
}
function pageDraftList(drafts){
  if(!drafts.length)return '';
  return `<section class="page-drafts"><h2>未完成的生成任务</h2><p>已通过部分保留为草稿，全部校验通过后才加入页面库。</p>${drafts.map(d=>`<article class="page-draft-row"><div><strong>${esc(d.title)}</strong><small>已通过 ${d.completed} / ${d.total} 个场景${Object.values(d.failures||{}).some(f=>f.quarantined)?' · 错误场景已隔离':''}${d.needs_material_selection?' · 需先确认资料，原草稿保留':''}</small></div><button type="button" class="secondary" data-resume-draft="${esc(d.id)}">${d.needs_material_selection?'选择资料重新生成':'继续未完成部分'}</button></article>`).join('')}</section>`;
}
function pageAction(command,label,icon,variant='secondary',attributes=''){
  return `<button class="${variant} ui-control" data-page-command="${command}" aria-label="${esc(label)}" ${attributes}>${uiIcon(icon)}<span>${esc(label)}</span></button>`;
}
function pageLibraryHeader(items){
  return `<div class="page-heading page-library-heading"><div><div class="page-kicker">${uiIcon('document')} 研究成果</div><h1>页面库</h1><p>为每一次研究，留下一份清晰的表达。</p></div><button class="primary" id="new-research-page">${uiIcon('sparkles')}<span>生成展示页</span></button></div>
    <form id="page-filter" class="page-filters"><div class="page-search-field">${uiIcon('search')}<input name="q" type="search" aria-label="搜索页面" placeholder="搜索标题、摘要或标签…" value="${esc(P.q)}"></div>
    <div class="page-filter-options"><div class="page-filter-fields"><label>地区<input name="region" aria-label="地区" placeholder="全部地区" value="${esc(P.region)}"></label><label>标签<input name="tag" aria-label="标签" placeholder="全部标签" value="${esc(P.tag)}"></label></div>
    <div class="page-filter-chips"><label class="page-filter-chip"><input name="favorite" type="checkbox" ${P.favorite?'checked':''}>${uiIcon('star')}<span>收藏</span></label><label class="page-filter-chip"><input name="archived" type="checkbox" ${P.archived?'checked':''}>${uiIcon('archive')}<span>已归档</span></label></div><button class="secondary page-filter-submit">${uiIcon('filter')}<span>筛选</span></button></div></form>
    <div class="page-list-caption"><span>${P.archived?'归档页面':'已保存的页面'} <strong>${items.length}</strong></span><small>让资料、结论与依据一起呈现</small></div>`;
}
function pageReaderControls(page,historical){
  const r=page.revision;
  return `<div class="page-reader-toolbar">${pageAction('back','页面库','back','ui-ghost')}<div class="actions page-main-actions">${pageAction('present','演示模式','present')}${pageAction('layout','调整展示','edit','secondary',historical?'disabled':'')}${pageAction('refresh','更新研究','refresh','primary',historical?'disabled':'')}</div></div>
    <div class="page-management"><div class="page-version">${uiIcon('history')}<select id="page-history" aria-label="查看历史版本">${page.history.map(h=>`<option value="${h.number}" ${h.number===r.number?'selected':''}>v${h.number} · ${esc(pageOperation[h.operation]||h.operation)} · ${esc(pageDate(h.created_at))}</option>`).join('')}</select></div>${historical?pageAction('restore','恢复此版本','history','secondary'):''}
    <div class="page-utility-actions">${pageAction('favorite',page.favorite?'已收藏':'收藏','star','ui-ghost',`aria-pressed="${page.favorite}"`)}<details class="page-more"><summary>${uiIcon('more')}<span>更多</span></summary><div class="page-more-menu" aria-label="更多页面操作">${pageAction('rename','重命名','edit','ui-menu-item')}${pageAction('copy','复制','copy','ui-menu-item')}${pageAction('archive',page.archived?'取消归档':'归档','archive','ui-menu-item')}<div class="page-menu-divider"></div>${pageAction('delete','删除','trash','ui-menu-item ui-danger')}</div></details></div></div>`;
}

async function openResearchPage(id,revision=null){
  P.id=id;P.revision=revision;S.view='pages';await render();
}
function pageRequestNeedsSelection(prompt){
  if(/(不要|不需要|不用|别)[^。\n]{0,30}(生成|制作|做成|页面)|(?:如何|怎么|能否|是否)[^。\n]{0,20}(生成|制作|创建)|功能|大纲|开发计划/.test(prompt))return false;
  return /我的|这份|这篇|该论文|该资料|刚才上传|毕设|毕业设计|毕业论文/.test(prompt)&&/生成|整理成|做成|创建/.test(prompt)&&/页面|网页|展示页/.test(prompt);
}
async function pageComposer({prompt='',industry=S.industry,page=null,operation='create',sceneId=null}={}){
  if(S.job||S.sending){toast('请等待当前任务完成或先停止任务');return;}
  const mode=sceneId?'调整选中图形':operation==='create'?'生成可视化页面':pageOperation[operation];
  dialog(mode,`<form id="page-compose"><label>展示要求<textarea id="page-request" rows="5" required maxlength="10000" placeholder="主题、展示对象、重点问题和风格，例如：面向项目负责人的政策对比页">${esc(prompt)}</textarea></label><div id="page-material-choices" class="page-material-choices">${operation==='create'?'正在读取可用资料…':'沿用原页面的资料范围。'}</div><p class="description">选择资料后仅使用这些资料；“我的毕设”等要求需要选定对象。全部校验通过后保存；出错时保留草稿。</p><div class="dialog-actions"><button class="primary" type="submit" id="page-generate-submit" ${operation==='create'?'disabled':''}>${esc(mode)}</button></div></form>`);
  const form=$('#page-compose');let catalog=[];
  form.onsubmit=async e=>{
    e.preventDefault();const request=$('#page-request').value.trim();if(!request||$('#page-generate-submit').disabled)return;
    const materials=[...form.querySelectorAll('[data-page-material]')].filter(input=>input.checked).map(input=>{const r=catalog[Number(input.dataset.pageMaterial)];return {industry_id:r.industry_id,id:r.id};});
    if(materials.length>6){toast('每页最多选择6份资料');return;}
    if(operation==='create'&&pageRequestNeedsSelection(request)&&!materials.length){toast('请先选择要展示的资料');return;}
    $('#dialog').close();$('#chat-input').value=request;openAgent();
    await sendAgent(null,{generate_page:true,industry,page_id:page?.id||null,page_version:page?.version??null,page_operation:operation,page_scene_id:sceneId,page_materials:materials});
  };
  if(operation!=='create')return;
  try{
    const records=await api('/materials?'+new URLSearchParams({industry}));
    if($('#page-compose')!==form||!form.isConnected)return;
    catalog=records.filter(r=>r.model_allowed&&r.sensitivity!=='restricted'&&(industry==='all'||r.industry_id===industry||r.sensitivity==='public'));
    $('#page-material-choices').innerHTML=catalog.length?`<fieldset><legend>本次展示的资料</legend>${catalog.map((r,i)=>`<label><input type="checkbox" data-page-material="${i}" ${prompt.includes(r.title)||prompt.includes(r.id)?'checked':''}><span>${esc(r.title)}<small>${esc(r.industry_id)}</small></span></label>`).join('')}</fieldset>`:'<p class="description">暂无可供模型使用的资料；请在知识库授权所需资料。普通专题仍可按已有证据研究。</p>';
    $('#page-generate-submit').disabled=false;
  }catch(error){if($('#page-compose')===form)$('#page-material-choices').textContent='资料目录读取失败，请关闭后重试。';}
}
async function renderPages(){
  if(typeof cleanupVisualPage==='function')cleanupVisualPage();
  const seq=S.renderSeq,industry=S.industry,load=++P.load;
  $('#content').innerHTML='<div class="content-loading">正在读取页面库…</div>';
  if(P.id){
    let page;
    try{page=await api('/pages/'+encodeURIComponent(P.id)+(P.revision?'?revision='+P.revision:''));}
    catch(error){if(seq!==S.renderSeq||load!==P.load)return;P.id=null;toast(error.message);return renderPages();}
    if(seq!==S.renderSeq||load!==P.load)return;
    P.page=page;renderPageReader(page);return;
  }
  const [items,drafts]=await Promise.all([
    api('/pages?'+new URLSearchParams({industry,q:P.q,region:P.region,tag:P.tag,archived:P.archived,favorite:P.favorite})),
    api('/page-drafts?'+new URLSearchParams({industry}))]);
  if(seq!==S.renderSeq||industry!==S.industry||load!==P.load)return;
  $('#content').innerHTML=`${pageLibraryHeader(items)}<div class="page-grid">${items.map(p=>`<article class="saved-page-card">${typeof visualPreview==='function'?visualPreview(p.visual_preview):''}<div class="card-top"><span class="tag">${esc(industryName(p.industry))}</span><span>${p.favorite?uiIcon('star'):''}<span>v${p.version}</span></span></div><h2><button class="link-button" data-open-page="${esc(p.id)}">${esc(p.title)}</button></h2><p>${esc(p.summary)}</p><div class="page-tags">${[...p.regions,...p.tags].map(t=>`<span class="tag">${esc(t)}</span>`).join('')}</div><div class="card-foot"><small>${esc(pageDate(p.updated_at))}</small><button class="secondary" data-open-page="${esc(p.id)}">查看页面 ${uiIcon('arrow')}</button></div></article>`).join('')||`<div class="page-empty"><div class="page-empty-symbol">${uiIcon('document')}</div><h2>还没有匹配的展示页</h2><p>把值得留下的研究，变成一张清晰的页面。<br>从一个主题开始，或试试其他筛选条件。</p><button class="secondary" data-new-page>${uiIcon('sparkles')}<span>开始生成</span></button></div>`}</div>`;
  $('#new-research-page').onclick=()=>pageComposer();
  const draftHost=document.createElement('div');draftHost.innerHTML=pageDraftList(drafts);$('#page-filter').insertAdjacentElement?$('#page-filter').insertAdjacentElement('afterend',draftHost):$('#content').append(draftHost);
  draftHost.querySelectorAll('[data-resume-draft]').forEach(button=>button.onclick=()=>resumePageDraft(drafts.find(d=>d.id===button.dataset.resumeDraft)).catch(handleError));
  $('#page-filter').onsubmit=e=>{e.preventDefault();const f=new FormData(e.target);P.q=String(f.get('q')||'');P.region=String(f.get('region')||'');P.tag=String(f.get('tag')||'');P.favorite=f.has('favorite');P.archived=f.has('archived');renderPages().catch(handleError);};
}
function pageRefs(refs){return (refs||[]).map(ref=>`<button class="page-ref" data-page-ref="${esc(ref)}" aria-label="查看引用依据">[${esc((P.page?.revision.sources.findIndex(s=>s.ref===ref)??-1)+1)}]</button>`).join('');}
function pageBlock(block,index){
  if(block.kind==='chart')return `<section class="page-section"><h2>${esc(block.title)}</h2><div data-page-chart="${index}"></div></section>`;
  const items=block.items||[];
  if(block.kind==='table')return `<section class="page-section"><h2>${esc(block.title)}</h2><div class="page-table-wrap"><table class="page-table"><thead><tr>${block.columns.map(c=>`<th>${esc(c)}</th>`).join('')}<th>性质与依据</th></tr></thead><tbody>${items.map(item=>`<tr>${item.cells.map(c=>`<td>${esc(c)}</td>`).join('')}<td><span class="page-nature ${esc(item.nature)}">${esc(pageNature[item.nature])}</span>${pageRefs(item.references)}</td></tr>`).join('')}</tbody></table></div></section>`;
  return `<section class="page-section"><h2>${esc(block.title)}</h2><div class="${block.kind==='cards'?'page-insight-grid':'page-prose'}">${items.map(item=>`<article class="page-insight"><span class="page-nature ${esc(item.nature)}">${esc(pageNature[item.nature])}</span>${item.label?`<h3>${esc(item.label)}</h3>`:''}<p>${esc(item.text)}</p><div>${pageRefs(item.references)}</div></article>`).join('')}</div></section>`;
}
function renderPageReader(page){
  if(page.revision.content.schema_version===2){renderVisualPage(page);return;}
  const r=page.revision,c=r.content,historical=r.number!==page.version;
  $('#content').innerHTML=`${pageReaderControls(page,historical)}${page.warnings.length?`<div class="notice">此页面保存的部分依据已变化，更新前需要重新检索：${page.warnings.map(w=>esc(w.title+'：'+w.message)).join('；')}</div>`:''}<article class="research-page"><header class="page-hero"><div class="page-eyebrow">研究展示 · ${esc(industryName(page.industry))} · v${r.number}${historical?' · 历史版本':''}</div><h1>${esc(c.title)}</h1><p class="page-subtitle">${esc(c.subtitle)}</p><div class="page-tags">${[...c.regions,...c.tags].map(t=>`<span class="tag">${esc(t)}</span>`).join('')}</div><div class="page-abstract"><span>核心发现</span><p>${esc(c.summary)}</p>${pageRefs(c.summary_references)}</div><div class="page-byline">展示对象：${esc(c.audience)} · 本版保存：${esc(pageDate(r.created_at))}${r.model?' · 模型：'+esc(r.model):''}</div></header><div class="page-body">${c.blocks.map(pageBlock).join('')}<section class="page-section page-coverage"><h2>研究范围与待查事项</h2><p>${esc(c.coverage)}</p>${c.gaps.length?`<ul>${c.gaps.map(g=>`<li>${esc(g)}</li>`).join('')}</ul>`:''}<p class="description">页面保存日期不等于调查覆盖截止。摘要及分析来自所列片段，资料核验通过不代表全部外部事实已确认。</p></section><section class="page-section"><h2>引用来源 <small>${r.sources.length}</small></h2><div class="page-source-list">${r.sources.map((s,i)=>`<button class="page-source-row" data-page-ref="${esc(s.ref)}"><span>[${i+1}] ${esc(s.title)}</span><small>${esc(s.location||s.citation||s.url||'本地资料')} · 核验日期 ${esc(s.checked_date||'未登记')}</small></button>`).join('')}</div></section></div></article>`;
  document.querySelectorAll('[data-page-chart]').forEach(host=>appendResearchChart(host,c.blocks[Number(host.dataset.pageChart)].figure));
  $('#page-history').onchange=e=>openResearchPage(page.id,Number(e.target.value)).catch(handleError);
}
function showPageSource(ref){
  const s=P.page?.revision.sources.find(s=>s.ref===ref);if(!s)return;
  const block=/#block-(\d+)/.exec(s.citation||'');
  dialog('引用依据',`<h3>${esc(s.title)}</h3><p class="description">${esc(s.region||'地域未登记')} · 核验日期 ${esc(s.checked_date||'未登记')}<br>${esc(s.citation||s.location||'')}</p><blockquote class="page-quote">${esc(s.text)}</blockquote>${s.support_quotes?.length?'<h4>图形使用的原文摘录</h4>'+s.support_quotes.map(q=>`<blockquote class="page-quote">${esc(q)}</blockquote>`).join(''):''}<p class="description">这是页面生成时保存的短引用；原文版本变化不会改写旧页面。</p><div class="dialog-actions">${s.kind==='material'?'<button class="primary" id="page-original">定位原文</button>':''}${s.kind==='evidence'?`<button class="secondary" data-record="${esc(s.id)}" data-industry="${esc(s.industry_id)}">查看历史证据</button>`:''}${s.url&&researchChartURL(s.url)!=='#'?`<a class="secondary" href="${esc(researchChartURL(s.url))}" target="_blank" rel="noopener noreferrer">打开原始来源 ↗</a>`:''}</div>`);
  if(s.kind==='material')$('#page-original').onclick=async()=>{try{const original=await api('/materials/'+encodeURIComponent(s.industry_id)+'/'+encodeURIComponent(s.id));if(original.extraction?.text_index)await showMaterialSections(s.industry_id,s.id,original.title,'',block?Math.max(0,Number(block[1])-1):0);else await viewMaterial(s.industry_id,s.id);}catch(error){handleError(error);}};
}
async function pageCommand(command){
  const page=P.page;
  if(command==='back'){P.id=null;P.revision=null;return render();}
  if(command==='present'){document.body.classList.toggle('page-present');return;}
  if(command==='layout'||command==='refresh'){pageComposer({page,industry:page.industry,operation:command,prompt:command==='refresh'?'根据当前可用资料更新本页，保留原有展示重点。':''});return;}
  if(!page)return;
  if(P.mutating){toast('页面操作正在保存，请稍候');return;}
  if(command==='rename'){
    dialog('重命名页面',`<form id="page-rename"><label>页面名称<input id="page-title" required maxlength="160" value="${esc(page.title)}"></label><div class="dialog-actions"><button class="primary">保存</button></div></form>`);
    $('#page-rename').onsubmit=async e=>{e.preventDefault();if(P.mutating)return;const seq=S.renderSeq,form=e.target;P.mutating=true;form.querySelector('button').disabled=true;try{await api('/pages/'+page.id+'/metadata','POST',{version:page.version,title:$('#page-title').value});if(form.isConnected)$('#dialog').close();if(S.view==='pages'&&S.renderSeq===seq){P.revision=null;await render();}}catch(error){handleError(error);}finally{P.mutating=false;if(form.isConnected)form.querySelector('button').disabled=false;}};return;
  }
  if(command==='delete'&&!confirm('永久删除这个展示页及其历史版本？原始资料将保留。'))return;
  const action={favorite:'metadata',archive:'metadata'}[command]||command;
  const body={version:page.version,...(command==='favorite'?{favorite:!page.favorite}:{}),...(command==='archive'?{archived:!page.archived}:{}),...(command==='restore'?{revision:page.revision.number}:{})};
  const seq=S.renderSeq;P.mutating=true;
  const controls=Array.from(document.querySelectorAll('[data-page-command]'),b=>[b,b.disabled]);controls.forEach(([b])=>{if(!['back','present'].includes(b.dataset.pageCommand))b.disabled=true;});
  try{const result=await api('/pages/'+page.id+'/'+action,'POST',body);if(S.view!=='pages'||S.renderSeq!==seq)return;P.id=command==='delete'?null:result.id;P.revision=null;await render();}
  finally{P.mutating=false;controls.forEach(([b,disabled])=>{if(b.isConnected)b.disabled=disabled;});}
}
document.addEventListener('click',e=>{
  document.querySelectorAll('.page-more').forEach(menu=>{if(menu.open&&(!menu.contains(e.target)||e.target.closest('[data-page-command]')))menu.open=false;});
  if(e.target.closest('[data-new-page]')){pageComposer();return;}
  const open=e.target.closest('[data-open-page]');if(open){openResearchPage(open.dataset.openPage).catch(handleError);return;}
  const ref=e.target.closest('[data-page-ref]');if(ref){showPageSource(ref.dataset.pageRef);return;}
  const command=e.target.closest('[data-page-command]');if(command)pageCommand(command.dataset.pageCommand).catch(handleError);
});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){document.body.classList.remove('page-present');document.querySelectorAll('.page-more').forEach(menu=>{if(menu.open){menu.open=false;menu.querySelector('summary')?.focus();}});}});
