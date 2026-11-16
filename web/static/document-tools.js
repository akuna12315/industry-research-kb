/* Explicit local document processing. All source/model text is escaped. */
let documentToolsSequence=0,documentToolsPoll=null,documentToolsContext=null;
function documentProcessingTools(record){
  return `<div class="document-tools notice"><div><strong>图表与表格</strong><p>保留表头、单位、行列关系和注释；可识别图片文字与图形说明。处理结果可用于检索，识别不代表核验。</p></div><button class="secondary" data-document-action="open" data-industry="${esc(record.industry_id)}" data-material="${esc(record.id)}">查看 / 处理图表</button></div>`;
}
const documentPath=(industry,key)=>'/materials/'+encodeURIComponent(industry)+'/'+encodeURIComponent(key)+'/document';
function documentTableMarkup(table){
  const lookup=Object.fromEntries(table.cells.map(c=>[c.id,c]));let rows='';
  for(let row=1;row<=Math.min(table.rows,100);row++){
    let cells='';
    for(let column=1;column<=Math.min(table.columns,80);column++){
      const cell=lookup[table.grid[row-1][column-1]];
      if(!cell){cells+='<td></td>';continue;}
      if(cell.row!==row||cell.column!==column)continue;
      const tag=row<=table.header_rows?'th':'td';
      cells+=`<${tag} rowspan="${Math.min(cell.row_span,100-row+1)}" colspan="${Math.min(cell.column_span,80-column+1)}"><button class="document-cell" data-document-action="cell" data-table="${esc(table.id)}" data-cell="${esc(cell.id)}" title="查看单元格来源">${cell.text?esc(cell.text):'<span class="document-unknown">'+(cell.source?.formula&&!cell.source?.cached_value_available?'公式结果未缓存':'空白 / 未披露')+'</span>'}</button></${tag}>`;
    }
    rows+='<tr>'+cells+'</tr>';
  }
  return `<section class="document-table panel"><h3>${esc(table.caption)}</h3><p class="description">${table.rows} 行 × ${table.columns} 列 · 表头 ${table.header_rows} 行 · ${table.quality!=='parsed_not_verified'?'图片表头为识别结果，请核对':table.header_detection==='heuristic'?'表头为自动推断，请核对':'保留原生或指定表头'} · ${table.quality==='parsed_not_verified'?'结构化解析':'图片识别草稿'}</p><div class="document-grid" role="region" tabindex="0" aria-label="${esc(table.caption)}"><table>${rows}</table></div>${table.rows>100||table.columns>80?'<p class="description">预览最多100行、80列；导出和计算使用完整数据。</p>':''}${table.notes.length?'<p class="document-notes">'+table.notes.map(esc).join('<br>')+'</p>':''}<button class="secondary" data-document-action="calculate" data-table="${esc(table.id)}">按选定行计算</button></section>`;
}
function documentFiguresMarkup(figures,industry,key){return figures.map(f=>`<section class="document-figure panel"><h3>${esc(f.caption||'图像')}</h3><img loading="lazy" src="/api${documentPath(industry,key)}/figures/${encodeURIComponent(f.id)}" alt="${esc(f.caption||'原图预览')}"><p class="description">${esc(f.source.format||'')} ${f.source.page?'· 第'+esc(f.source.page)+'页':''} · 机器识别，需核对原图</p>${f.vision?`<p>${esc(f.vision.summary||'')}</p><ul>${(f.vision.observations||[]).map(v=>'<li>'+esc(String(v))+'</li>').join('')}</ul><p class="description">${(f.vision.limitations||[]).map(v=>esc(String(v))).join('；')}</p>`:`<p class="description">${f.vision_status==='waiting_permission'?'未授权模型读取，本轮未进行图形理解。':f.vision_error?'图形理解尚未完成，可重试。':'本轮没有图形语义识别结果。'}</p>`}${f.ocr?`<details><summary>查看OCR文字与识别置信度</summary><pre>${esc(f.ocr.text)}</pre><p class="description">置信度来自OCR识别，不代表文字或事实一定正确。</p></details>`:''}</section>`).join('');}
async function openDocumentTools(industry,key){
  const sequence=++documentToolsSequence;clearTimeout(documentToolsPoll);
  const navigation=S.renderSeq;
  const [record,data]=await Promise.all([api('/materials/'+encodeURIComponent(industry)+'/'+encodeURIComponent(key)),api(documentPath(industry,key))]);
  if(sequence!==documentToolsSequence||navigation!==S.renderSeq)return;
  documentToolsContext={industry,key,record,data,sequence};
  const a=data.analysis&&data.analysis.status!=='invalidated'?data.analysis:null,job=data.jobs[0],active=job&&['queued','running'].includes(job.status),coverage=a?.coverage||{};
  const canModel=record.model_allowed&&record.sensitivity!=='restricted';
  const options=job?.options||{};
  dialog('图表处理 · '+record.title,`<div id="document-analysis" data-material="${esc(key)}"><p>表格保留原始值、表头、单位、合并关系与注释；图片识别结果保留原图和覆盖范围。结果加入本资料的索引，原件与旧版本保留。</p><p class="description">解析及识别都是资料表示，不等于已核实事实。空白单元格不会自动当成0。</p>${data.analysis?.status==='invalidated'?'<p class="notice">资料版本已变化，请重新处理图表。</p>':''}<form id="document-process-form" class="panel"><div class="form-grid"><label class="field">表头行数<select name="header_rows">${[['','自动识别'],['0','无表头'],['1','1行'],['2','2行'],['3','3行']].map(([v,t])=>`<option value="${v}" ${String(options.header_rows??'')===v?'selected':''}>${t}</option>`).join('')}</select></label><label class="field">起始PDF页<input name="start_page" type="number" min="1" max="300" value="${coverage.next_start_page||1}"></label><label class="field">本轮最多页数<input name="max_pages" type="number" min="1" max="50" value="${options.max_pages||12}"></label><label class="field">本轮最多图像<input name="max_images" type="number" min="0" max="8" value="${options.max_images??4}"></label></div><label><input type="checkbox" name="ocr" ${options.ocr!==false?'checked':''}> 识别图片文字（本机OCR）</label><label><input type="checkbox" name="vision" ${canModel?(options.vision!==false?'checked':''):'disabled'}> 解释图表内容（本机模型）</label>${!canModel?'<p class="description">图形模型分析需要资料已加入检索且不为受限资料；当前可处理原生表格和本机OCR。</p>':''}<div class="dialog-actions"><button class="primary" ${active?'disabled':''}>${a?.status==='partial'?'继续下一批 / 重新处理':'开始图表处理'}</button></div></form>${job?`<div class="notice" id="document-job"><strong>${esc(({queued:'等待处理',running:'正在本机处理',interrupted:'已中断，可继续',completed:'本轮处理完成',partial:'部分完成，仍有未处理内容',failed:'处理失败，原件保留'})[job.status]||job.status)}</strong><p>${esc(job.note||'')} 已保存表格 ${job.progress?.tables||0} 个，已处理图像 ${job.progress?.processed_images||0} 个。</p>${active?'<button class="secondary" data-document-action="pause">当前步骤后暂停</button>':['interrupted','failed','partial'].includes(job.status)?'<button class="secondary" data-document-action="resume">继续 / 重试未完成步骤</button>':''}</div>`:''}${a?`<p class="description">${coverage.pages_total?'累计图表页数 '+coverage.pages_processed+'/'+coverage.pages_total+'；':''}已发现图像/页预览 ${coverage.images_total||0} 个，OCR ${coverage.images_ocr||0} 个，图形理解 ${coverage.images_vision||0} 个。${a.notices.map(esc).join('；')}</p><div class="dialog-actions"><a class="secondary" href="/api${documentPath(industry,key)}/export?format=json">导出结构JSON</a><a class="secondary" href="/api${documentPath(industry,key)}/export?format=markdown">导出表格Markdown</a></div>${a.tables.map(documentTableMarkup).join('')||'<p class="description">当前没有结构化表格结果。</p>'}${documentFiguresMarkup(a.figures,industry,key)}`:'<p class="description">尚无图表处理结果。</p>'}</div>`);
  if(active)pollDocumentJob(job.id,sequence);
}
function pollDocumentJob(id,sequence){documentToolsPoll=setTimeout(async()=>{
  if(sequence!==documentToolsSequence||!$('#document-analysis')||!$('#dialog').open)return;
  try{const job=await api('/document-jobs/'+encodeURIComponent(id));
    if(sequence!==documentToolsSequence||!$('#document-analysis')||!$('#dialog').open)return;
    if(['queued','running'].includes(job.status)){
      const host=$('#document-job');if(host)host.querySelector('p').textContent=(job.note||'正在处理。')+' 已保存表格 '+(job.progress?.tables||0)+' 个，图像 '+(job.progress?.processed_images||0)+' 个。';
      pollDocumentJob(id,sequence);
    }else await openDocumentTools(documentToolsContext.industry,documentToolsContext.key);
  }catch(error){handleError(error);}
},2500);}
document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-document-action]');if(!button)return;
  try{
    const action=button.dataset.documentAction;
    if(action==='open'){await openDocumentTools(button.dataset.industry,button.dataset.material);return;}
    const context=documentToolsContext;if(!context)return;
    if(action==='pause'||action==='resume'){
      const job=context.data.jobs[0];await api('/document-jobs/'+encodeURIComponent(job.id)+'/control','POST',{action:action==='pause'?'pause':'resume'});
      if(button.isConnected&&$('#dialog').open&&documentToolsContext===context)await openDocumentTools(context.industry,context.key);return;
    }
    const table=context.data.analysis?.tables.find(t=>t.id===button.dataset.table);if(!table)return;
    if(action==='cell'){
      const cell=table.cells.find(c=>c.id===button.dataset.cell);if(!cell)return;
      const source=cell.source||{},citation=table.row_citations?.[String(cell.row)];
      dialog('单元格来源',`<h3>${esc(table.caption)} · 行${cell.row} 列${cell.column}</h3><pre>${esc(cell.text||(cell.source?.formula&&!cell.source?.cached_value_available?'公式结果未缓存（未执行计算）':'空白 / 未披露'))}</pre><p>列含义：${esc(table.headers[cell.column-1]?.label||'未标注')}；单位提示：${esc(table.headers[cell.column-1]?.unit||'未提供')}</p><pre>${esc(JSON.stringify(source,null,2))}</pre><p class="description">${esc(citation||'暂无结构化行定位')} · ${esc(table.quality)}</p><button class="secondary" data-document-action="open" data-industry="${esc(context.industry)}" data-material="${esc(context.key)}">返回图表</button>`);return;
    }
    if(action==='calculate')dialog('按选定单元格计算',`<p>${esc(table.caption)}。请选择明确的数据行，避免把明细与合计重复计入。不会把空白当成0或自动换算单位。</p><form id="document-calculate-form" data-table="${esc(table.id)}"><label class="field">列<select name="column">${table.headers.map(h=>`<option value="${h.column}">${esc(h.label)}${h.unit?'（'+esc(h.unit)+'）':''}</option>`).join('')}</select></label><label class="field">数据行号<input name="rows" required placeholder="例如 3,4,5"></label><label class="field">方式<select name="operation"><option value="sum">求和</option><option value="mean">平均值</option><option value="min">最小值</option><option value="max">最大值</option></select></label><button class="primary">计算并显示来源</button><div id="document-calculation"></div></form>`);
  }catch(error){handleError(error);}
});
document.addEventListener('submit',async event=>{
  if(!['document-process-form','document-calculate-form'].includes(event.target.id))return;
  event.preventDefault();const form=event.target,context=documentToolsContext;if(!context)return;
  if(form.dataset.submitting==='1')return;
  form.dataset.submitting='1';const submit=form.querySelector('button');if(submit)submit.disabled=true;
  try{
    const values=new FormData(form);
    if(form.id==='document-process-form'){
      await api(documentPath(context.industry,context.key)+'/process','POST',{
        ocr:values.has('ocr'),vision:values.has('vision'),header_rows:values.get('header_rows')===''?null:Number(values.get('header_rows')),
        start_page:Number(values.get('start_page')),max_pages:Number(values.get('max_pages')),max_images:Number(values.get('max_images'))});
      if(form.isConnected&&$('#dialog').open&&documentToolsContext===context)await openDocumentTools(context.industry,context.key);
    }else{
      const result=await api(documentPath(context.industry,context.key)+'/calculate','POST',{
        table_id:form.dataset.table,column:Number(values.get('column')),rows:String(values.get('rows')).split(/[,，\s]+/).filter(Boolean).map(Number),operation:values.get('operation')});
      if(form.isConnected&&documentToolsContext===context)$('#document-calculation').innerHTML=`<p><strong>${esc(result.value)} ${esc(result.unit||'')}</strong></p><p>${esc(result.note)}</p><p class="description">输入单元格：${result.input_cell_ids.map(esc).join('、')}</p>`;
    }
  }catch(error){handleError(error);}finally{form.dataset.submitting='0';if(submit?.isConnected)submit.disabled=false;}
});
