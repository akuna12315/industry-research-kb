'use strict';

// Trusted local geometry only. Models choose names, never supply SVG or scripts.
function visualIcon(name){
  const paths={
    battery:'<rect x="3" y="6" width="17" height="12" rx="2"/><path d="M22 10v4M7 10v4m4-4v4"/>',
    energy:'<path d="m13 2-9 12h7l-1 8 10-12h-7l1-8Z"/>',
    health:'<path d="M20.5 4.5a5 5 0 0 0-7 0L12 6l-1.5-1.5a5 5 0 0 0-7 7L12 20l8.5-8.5a5 5 0 0 0 0-7Z"/><path d="M3 12h5l2-4 3 8 2-4h6"/>',
    layers:'<path d="m12 3 10 5-10 5L2 8l10-5Zm-10 9 10 5 10-5M2 16l10 5 10-5"/>',
    workflow:'<rect x="2" y="3" width="7" height="6" rx="1"/><rect x="15" y="15" width="7" height="6" rx="1"/><path d="M9 6h9v9M5 9v9h10"/>',
    database:'<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/>',
    server:'<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6h.01M7 17h.01M12 6h5m-5 11h5"/>',
    screen:'<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M12 17v4m-5 0h10"/>',
    sensor:'<path d="M2 12h4l3-8 5 16 3-8h5"/>',
    leaf:'<path d="M20 3C7 1 1 8 5 16c8 8 17 0 15-13ZM5 20 17 8"/>',
    tree:'<path d="m12 2-6 7h3l-5 7h6v6h4v-6h6l-5-7h3l-6-7Z"/>',
    policy:'<path d="M12 2 3 6v6c0 6 9 10 9 10s9-4 9-10V6l-9-4Z"/><path d="m8 12 3 3 5-6"/>',
    map:'<path d="m3 5 6-3 6 3 6-3v17l-6 3-6-3-6 3V5Zm6-3v17m6-14v17"/>',
    target:'<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
    experiment:'<path d="M9 2h6M10 2v7L4 19a2 2 0 0 0 2 3h12a2 2 0 0 0 2-3L14 9V2M7 15h10"/>',
    chart:'<path d="M3 3v18h18M7 16v-5m5 5V6m5 10v-8"/>',
    clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    people:'<circle cx="9" cy="7" r="4"/><path d="M2 21v-3a7 7 0 0 1 14 0v3M17 3a4 4 0 0 1 0 8m2 4c2 1 3 3 3 6"/>',
    idea:'<path d="M9 18h6m-5 4h4M8 15a7 7 0 1 1 8 0l-1 3H9l-1-3Z"/>'
    ,check:'<path d="m4 12 5 5L20 6"/>'
  };
  return paths[name]?`<svg class="visual-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${paths[name]}</svg>`:uiIcon(name);
}
const visualKind={flow:'研究路线',architecture:'系统结构',concept:'概念关系',comparison:'方案比较',chart:'数据观察',table:'数据表格'};
let visualPageCleanup=null;
function cleanupVisualPage(){if(visualPageCleanup){visualPageCleanup();visualPageCleanup=null;}}
function visualPreview(preview){
  if(!preview)return '';
  return `<div class="visual-preview" aria-label="${esc(visualKind[preview.kind]||'可视化页面')}预览"><span class="visual-preview-kind">${esc(visualKind[preview.kind]||'可视化')}</span><div class="visual-preview-nodes">${(preview.nodes||[]).map(n=>`<span>${visualIcon(n.icon)}<small>${esc(n.label)}</small></span>`).join('')||visualIcon('chart')}</div></div>`;
}
function visualGroups(scene){
  if(scene.kind==='flow'){
    const pending=new Set(scene.nodes.map(n=>n.id)),groups=[];
    while(pending.size){
      const next=scene.nodes.filter(n=>pending.has(n.id)&&!scene.edges.some(e=>e.target===n.id&&pending.has(e.source)));
      if(!next.length)return [scene.nodes];
      groups.push(next);next.forEach(n=>pending.delete(n.id));
    }
    return groups;
  }
  const keys=[...new Set(scene.nodes.map(n=>n.group||'组成模块'))];
  return keys.map(key=>scene.nodes.filter(n=>(n.group||'组成模块')===key));
}
function visualNode(node){
  return `<button class="visual-node" type="button" data-visual-node="${esc(node.id)}" aria-pressed="false"><span class="visual-node-symbol">${visualIcon(node.icon)}</span><strong>${esc(node.label)}</strong>${node.nature==='planned'?'<small>计划 / 目标</small>':''}${node.nature==='inference'?'<small>分析归纳</small>':''}${node.nature==='unknown'?'<small>待查明</small>':''}</button>`;
}
function visualSceneMarkup(scene,index){
  const badge=visualKind[scene.kind]||'可视化',groups=!['chart','comparison','table'].includes(scene.kind)?visualGroups(scene):[];
  return `<section class="visual-scene visual-${esc(scene.kind)} visual-${esc(scene.direction)} ${scene.emphasis==='primary'?'visual-primary':''}" data-visual-scene="${esc(scene.id)}">
    <header class="visual-scene-heading"><div><span class="visual-kicker">${esc(badge)}</span><h2>${esc(scene.title)}</h2>${scene.lead?`<p>${esc(scene.lead)}</p>`:''}</div><button type="button" class="secondary" data-edit-scene="${esc(scene.id)}">${uiIcon('edit')}调整这幅图</button></header>
    ${scene.kind==='chart'?`<div data-visual-chart="${index}"></div>`:scene.kind==='table'?visualTableMarkup(scene):scene.kind==='comparison'?`<div class="visual-choices" role="group" aria-label="选择比较方案">${scene.nodes.map(visualNode).join('')}</div><div class="visual-comparison"></div>`:`<div class="visual-graph"><svg class="visual-links" aria-hidden="true" focusable="false"></svg><div class="visual-stages visual-columns-${Math.min(groups.length,6)}">${groups.map((nodes,i)=>`<div class="visual-stage">${scene.kind==='flow'?`<span class="visual-stage-label">步骤 ${i+1}</span>`:nodes[0]?.group?`<span class="visual-stage-label">${esc(nodes[0].group)}</span>`:''}<div class="visual-stage-nodes">${nodes.map(visualNode).join('')}</div></div>`).join('')}</div></div>`}
    ${scene.kind==='flow'?'<div class="visual-step-controls"><button type="button" class="secondary" data-visual-step="-1" aria-label="上一步">← 上一步</button><span class="visual-step-position"></span><button type="button" class="secondary" data-visual-step="1" aria-label="下一步">下一步 →</button></div>':''}
    ${!['chart','table'].includes(scene.kind)?'<div class="visual-detail" aria-live="polite" aria-atomic="true"></div>':''}
  </section>`;
}
function visualTableMarkup(scene){
  return `<div class="visual-data-wrap" role="region" aria-label="${esc(scene.title)}数据表" tabindex="0"><table class="visual-data-table"><thead><tr><th scope="col">对象 / 方法</th>${scene.dimensions.map(d=>`<th scope="col">${esc(d.label)}</th>`).join('')}</tr></thead><tbody>${scene.nodes.map(n=>`<tr><th scope="row"><span>${visualIcon(n.icon)}${esc(n.label)}</span>${n.detail?`<small>${esc(n.detail)}</small>`:''}${pageRefs(n.references)}</th>${scene.dimensions.map(d=>{const cell=scene.cells.find(c=>c.node===n.id&&c.dimension===d.id);return `<td><p>${esc(cell.text)}</p><small>${esc(pageNature[cell.nature])}</small>${pageRefs(cell.references)}</td>`;}).join('')}</tr>`).join('')}</tbody></table></div>`;
}
function renderVisualPage(page){
  const r=page.revision,c=r.content,historical=r.number!==page.version;
  $('#content').innerHTML=`${pageReaderControls(page,historical)}${page.warnings.length?`<div class="notice">页面部分依据已变化：${page.warnings.map(w=>esc(w.title+'：'+w.message)).join('；')}</div>`:''}
    <article class="visual-page visual-theme-${esc(c.theme)} visual-layout-${esc(c.layout)}">
      <header class="visual-hero"><div class="visual-kicker">可视化研究 · ${esc(industryName(page.industry))} · v${r.number}</div><h1>${esc(c.title)}</h1><p>${esc([c.question!==c.title?c.question:'',c.subtitle].filter(Boolean).join(' · '))}</p><div class="visual-hero-meta"><span>${esc(c.audience)}</span><span>${c.scenes.length} 个视图</span><span>${esc(pageDate(r.created_at))}</span></div></header>
      <div class="visual-scene-list">${c.scenes.map(visualSceneMarkup).join('')}</div>
      <details class="visual-research-notes"><summary>研究说明与引用依据 · ${r.sources.length}处引用</summary><h2>核心结论</h2><p>${esc(c.summary)}</p>${pageRefs(c.summary_references)}<h2>范围与待查</h2><p>${esc(c.coverage)}</p>${c.gaps.length?`<ul>${c.gaps.map(g=>`<li>${esc(g)}</li>`).join('')}</ul>`:''}<p>页面依据为检索命中片段，生成日期不代表调查覆盖截止。</p><div class="page-source-list">${r.sources.map((s,i)=>`<button class="page-source-row" data-page-ref="${esc(s.ref)}">[${i+1}] ${esc(s.title)}<small>${esc(s.location||s.citation||'原文片段')}</small></button>`).join('')}</div></details>
    </article>`;
  $('#page-history').onchange=e=>openResearchPage(page.id,Number(e.target.value)).catch(handleError);
  const cleanup=[];
  document.querySelectorAll('[data-visual-scene]').forEach(host=>{
    const scene=c.scenes.find(s=>s.id===host.dataset.visualScene);
    host.querySelector('[data-edit-scene]').disabled=historical;
    host.querySelector('[data-edit-scene]').onclick=()=>pageComposer({page,industry:page.industry,operation:'layout',sceneId:scene.id,prompt:`调整“${scene.title}”：`});
    if(scene.kind==='chart'){appendResearchChart(host.querySelector('[data-visual-chart]'),scene.figure);return;}
    if(scene.kind==='table')return;
    cleanup.push(mountVisualScene(host,scene));
  });
  visualPageCleanup=()=>cleanup.forEach(fn=>fn());
}
function mountVisualScene(host,scene){
  const nodes=scene.nodes, buttons=[...host.querySelectorAll('[data-visual-node]')];
  const sequence=scene.kind==='flow'?visualGroups(scene).flat():nodes;
  let selected=sequence[0].id;
  const svg=host.querySelector('.visual-links');
  let positions=new Map(),observer=null;
  function draw(){
    if(!svg||!host.isConnected)return;
    const graph=host.querySelector('.visual-graph'),bounds=graph.getBoundingClientRect();
    if(!bounds.width)return;
    svg.setAttribute('viewBox',`0 0 ${bounds.width} ${bounds.height}`);
    positions=new Map(buttons.map(button=>{const b=button.getBoundingClientRect();return [button.dataset.visualNode,{x:b.x-bounds.x,y:b.y-bounds.y,w:b.width,h:b.height}];}));
    const marker='visual-arrow-'+scene.id;
    svg.innerHTML=`<defs><marker id="${marker}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0 10 5 0 10Z" fill="currentColor"/></marker></defs>`+scene.edges.map((edge,i)=>{
      const a=positions.get(edge.source),b=positions.get(edge.target);if(!a||!b)return '';
      const ac={x:a.x+a.w/2,y:a.y+a.h/2},bc={x:b.x+b.w/2,y:b.y+b.h/2};
      const horizontal=Math.abs(bc.x-ac.x)>Math.abs(bc.y-ac.y);
      let sx=ac.x,sy=ac.y,tx=bc.x,ty=bc.y;
      if(horizontal){sx+=Math.sign(tx-sx)*a.w/2;tx-=Math.sign(tx-ac.x)*b.w/2;}
      else{sy+=Math.sign(ty-sy)*a.h/2;ty-=Math.sign(ty-ac.y)*b.h/2;}
      const d=horizontal?`M${sx} ${sy} C${(sx+tx)/2} ${sy},${(sx+tx)/2} ${ty},${tx} ${ty}`:`M${sx} ${sy} C${sx} ${(sy+ty)/2},${tx} ${(sy+ty)/2},${tx} ${ty}`;
      return `<g data-visual-edge="${i}" class="${edge.source===selected||edge.target===selected?'is-connected':''}"><path d="${d}" fill="none" ${edge.relation!=='related'?`marker-end="url(#${marker})"`:''}/></g>`;
    }).join('');
  }
  function choose(id){
    const node=nodes.find(n=>n.id===id);if(!node)return;selected=id;
    const related=scene.edges.filter(e=>e.source===id||e.target===id);
    const neighbors=new Set([id,...related.flatMap(e=>[e.source,e.target])]);
    buttons.forEach(button=>{const key=button.dataset.visualNode;button.setAttribute('aria-pressed',String(key===id));button.classList.toggle('visual-dim',scene.kind!=='comparison'&&!neighbors.has(key));});
    const detail=host.querySelector('.visual-detail');
    detail.innerHTML=`<div class="visual-detail-title">${visualIcon(node.icon)}<strong>${esc(node.label)}</strong><span class="page-nature ${esc(node.nature)}">${esc(pageNature[node.nature]||'来源记载')}</span></div>${node.detail?`<p>${esc(node.detail)}</p>`:''}${pageRefs(node.references)}${related.length?`<div class="visual-relations">${related.map(e=>`<div><span>${esc(nodes.find(n=>n.id===e.source).label)} ${e.relation==='related'?'—':'→'} ${esc(nodes.find(n=>n.id===e.target).label)}</span><strong>${esc(e.label)}</strong>${e.nature==='inference'?'<small>分析归纳</small>':''}${pageRefs(e.references)}</div>`).join('')}</div>`:''}`;
    if(scene.kind==='comparison'){
      host.querySelector('.visual-comparison').innerHTML=scene.dimensions.map(d=>`<div class="visual-comparison-row"><h3>${esc(d.label)}</h3><div class="visual-comparison-values">${nodes.map(n=>{const cell=scene.cells.find(v=>v.node===n.id&&v.dimension===d.id);return `<div class="visual-comparison-value ${n.id===id?'is-selected':''}"><span>${esc(n.label)}</span><p>${esc(cell.text)}</p><small>${esc(pageNature[cell.nature])}</small>${pageRefs(cell.references)}</div>`;}).join('')}</div></div>`).join('');
    }
    if(scene.kind==='flow'){
      const index=sequence.findIndex(n=>n.id===id);
      host.querySelector('.visual-step-position').textContent=`${index+1} / ${sequence.length} · ${node.label}`;
      host.querySelector('[data-visual-step="-1"]').disabled=index===0;
      host.querySelector('[data-visual-step="1"]').disabled=index===sequence.length-1;
    }
    if(svg)svg.querySelectorAll('[data-visual-edge]').forEach(g=>{const edge=scene.edges[Number(g.dataset.visualEdge)];g.classList.toggle('is-connected',edge.source===id||edge.target===id);});
  }
  buttons.forEach(button=>button.onclick=()=>choose(button.dataset.visualNode));
  host.querySelectorAll('[data-visual-step]').forEach(button=>button.onclick=()=>{
    const i=sequence.findIndex(n=>n.id===selected)+Number(button.dataset.visualStep);if(sequence[i])choose(sequence[i].id);
  });
  choose(selected);
  if(svg){
    if(typeof ResizeObserver!=='undefined'){observer=new ResizeObserver(draw);observer.observe(host.querySelector('.visual-graph'));}
    if(typeof requestAnimationFrame!=='undefined')requestAnimationFrame(draw);
  }
  return ()=>observer?.disconnect();
}
