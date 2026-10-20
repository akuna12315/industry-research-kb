'use strict';

const chartDateKinds={event:'事件日期',published:'发布日期',effective:'实施日期',unknown:'日期未知'};
const chartStatuses={reported:'来源记载',planned:'计划 / 目标',uncertain:'待核对',conflict:'来源冲突'};
const chartColors={reported:'#527d52',planned:'#b47c31',uncertain:'#8c9698',conflict:'#b65353'};

function researchChartURL(value){
  try{const url=new URL(value);return ['http:','https:'].includes(url.protocol)&&!url.username&&!url.password?url.href:'#';}catch{return '#';}
}

function researchChartDate(value){
  if(typeof value!=='string'||!/^\d{4}(?:-\d{2}(?:-\d{2})?)?$/.test(value))return false;
  const parts=value.split('-').map(Number),year=parts[0],month=parts[1]||1,day=parts[2]||1;
  if(year<1||month<1||month>12||day<1||day>new Date(Date.UTC(year,month,0)).getUTCDate())return false;
  return true;
}
function validResearchChart(raw){
  const bounded=(v,n)=>typeof v==='string'&&v.length<=n;
  if(!raw||!['timeline','line','bar'].includes(raw.kind)||!bounded(raw.title,160)||!raw.title.trim()||!Array.isArray(raw.items)||raw.items.length<1||raw.items.length>30)return null;
  if(!['description','note','coverage_note'].every(k=>raw[k]===undefined||bounded(raw[k],1000)))return null;
  for(const item of raw.items){
    if(!item||!bounded(item.label,120)||!item.label.trim()||item.date!==null&&!researchChartDate(item.date)||!Object.hasOwn(chartDateKinds,item.date_kind)||!Object.hasOwn(chartStatuses,item.status))return null;
    if(!bounded(item.summary||'',600)||!bounded(item.quote||'',1200)||!Array.isArray(item.sources)||item.sources.length<1||item.sources.length>4)return null;
    if(raw.kind==='timeline'?item.value!=null:item.value!==null&&(typeof item.value!=='number'||!Number.isFinite(item.value)||Math.abs(item.value)>1e15))return null;
    for(const s of item.sources){
      if(!s||!['material','evidence','page'].includes(s.kind)||!bounded(s.title||'',300)||!bounded(s.excerpt||'',1600)||!bounded(s.url||'',3000))return null;
      if(s.kind!=='page'&&(!bounded(s.id,100)||!bounded(s.industry_id,100)))return null;
    }
  }
  if(raw.kind!=='timeline'){
    if(!['metric','unit','geography','scope','basis'].every(k=>bounded(raw[k],300)&&raw[k].trim())||!['actual','planned'].includes(raw.nature))return null;
    if(raw.items.some(p=>p.date===null)||new Set(raw.items.map(p=>p.date)).size!==raw.items.length||new Set(raw.items.map(p=>p.date.length)).size!==1||raw.items.filter(p=>p.value!==null).length<2)return null;
  }
  return raw;
}
function chartPointGroup(point,index,shape){
  return `<g class="chart-node" role="button" tabindex="0" data-chart-point="${index}" aria-label="${esc((point.date||'日期未知')+' '+point.label+'，查看依据')}"><title>${esc(point.label+' · '+chartStatuses[point.status])}</title>${shape}</g>`;
}
function timelineFigure(chart){
  const known=chart.items.map((point,index)=>({point,index})).filter(v=>v.point.date);
  if(!known.length)return '<p class="chart-limit">事件日期均未知，暂不放到时间轴上；请查看下方节点与依据。</p>';
  const width=Math.max(680,known.length*180),x=i=>known.length===1?width/2:70+i*(width-140)/(known.length-1);
  const nodes=known.map(({point,index},i)=>chartPointGroup(point,index,`<circle cx="${x(i)}" cy="105" r="7" fill="${chartColors[point.status]}"></circle><text x="${x(i)}" y="64" text-anchor="middle">${esc(point.date)}</text><text class="chart-small" x="${x(i)}" y="83" text-anchor="middle">${esc(chartDateKinds[point.date_kind])}</text><text x="${x(i)}" y="140" text-anchor="middle">${esc(point.label.length>15?point.label.slice(0,14)+'…':point.label)}</text><text class="chart-small" x="${x(i)}" y="162" text-anchor="middle">${esc(chartStatuses[point.status])}</text>`)).join('');
  return `<div class="chart-plot"><svg viewBox="0 0 ${width} 195" width="${width}" height="195" role="group" aria-label="${esc(chart.title+'，事件时间轴')}" xmlns="http://www.w3.org/2000/svg"><line class="chart-axis" x1="35" y1="105" x2="${width-35}" y2="105"></line>${nodes}</svg></div><p class="chart-limit">按日期排序，节点间距不代表时长。${chart.items.length-known.length?`${chart.items.length-known.length}个日期未知节点列于下方，未强行定位。`:''}</p>`;
}
function trendFigure(chart){
  const width=Math.max(720,chart.items.length*65),height=290,left=75,right=width-35,top=25,bottom=230;
  const values=chart.items.filter(p=>p.value!==null).map(p=>p.value),minimum=Math.min(0,...values),maximum=Math.max(0,...values);
  const range=maximum===minimum?1:maximum-minimum;
  const times=chart.items.map(p=>{const a=p.date.split('-').map(Number);return Date.UTC(a[0],(a[1]||1)-1,a[2]||1);});
  const x=i=>left+(times[i]-times[0])/(times[times.length-1]-times[0])*(right-left),y=v=>bottom-(v-minimum)/range*(bottom-top);
  const number=v=>Number(v.toPrecision(5)).toLocaleString('zh-CN');
  const grid=Array.from({length:5},(_,i)=>{const value=minimum+range*i/4,py=y(value);return `<line class="chart-grid" x1="${left}" y1="${py}" x2="${right}" y2="${py}"></line><text class="chart-small" x="${left-10}" y="${py+4}" text-anchor="end">${esc(number(value))}</text>`;}).join('');
  let path='',connected=false;
  const segments=[];
  if(chart.kind==='line')for(let i=0;i<chart.items.length;i++){
    const value=chart.items[i].value;
    if(value===null){connected=false;continue;}
    path+=(connected?' L':' M')+x(i)+' '+y(value);connected=true;
  }
  if(path)segments.push(`<path class="chart-series ${chart.nature==='planned'?'planned':''}" d="${path}"></path>`);
  const barWidth=Math.min(44,...times.slice(1).map((_,i)=>(x(i+1)-x(i))*.65));
  const marks=chart.items.map((point,i)=>{
    const date=i===0||i===chart.items.length-1||i%Math.ceil(chart.items.length/8)===0?`<text class="chart-small" x="${x(i)}" y="258" text-anchor="middle">${esc(point.date)}</text>`:'';
    if(point.value===null)return date+`<text class="chart-small" x="${x(i)}" y="${bottom-8}" text-anchor="middle">缺失</text>`;
    const color=chart.nature==='planned'?chartColors.planned:chartColors.reported;
    const shape=chart.kind==='bar'?`<rect x="${x(i)-barWidth/2}" y="${Math.min(y(0),y(point.value))}" width="${barWidth}" height="${Math.max(1,Math.abs(y(point.value)-y(0)))}" rx="2" fill="${color}"></rect>`:`<circle cx="${x(i)}" cy="${y(point.value)}" r="5" fill="${color}"></circle>`;
    return date+chartPointGroup(point,i,shape);
  }).join('');
  return `<div class="chart-plot"><svg viewBox="0 0 ${width} ${height}" width="${width}" height="${height}" role="group" aria-label="${esc(chart.title+'，'+(chart.kind==='line'?'折线图':'柱状图'))}" xmlns="http://www.w3.org/2000/svg">${grid}<line class="chart-axis" x1="${left}" y1="${y(0)}" x2="${right}" y2="${y(0)}"></line>${segments.join('')}${marks}</svg></div><p class="chart-limit">${esc(chart.metric)} · ${esc(chart.unit)} · ${esc(chart.geography)} · ${esc(chart.scope)} · ${esc(chart.basis)} · ${chart.nature==='planned'?'计划 / 目标':'实际口径'}。缺失值留空，不补0、不跨缺失连线。</p>`;
}
function showChartPoint(chart,index){
  const point=chart.items[index];if(!point)return;
  const sourceMarkup=point.sources.map(source=>`<article class="chart-source"><h3>${esc(source.title||'引用依据')}</h3><p class="description">${esc(source.industry_id||'本轮公开原文')} · ${esc(source.citation||source.id||'')} · 核验 ${esc(source.checked_date||'未登记')} ${esc(source.location||'')}</p><p class="description">${[['事件日期',source.event_date],['发布日期',source.published_date],['实施日期',source.effective_date]].filter(([,value])=>value).map(([label,value])=>esc(label)+' '+esc(value)).join(' · ')}</p><blockquote>${esc(source.excerpt||'无正文摘录，仅有已登记信息。')}</blockquote><div class="actions">${source.kind==='material'?`<button class="secondary" data-action="view-material" data-id="${esc(source.id)}" data-industry="${esc(source.industry_id)}">查看本地资料</button>`:source.kind==='evidence'?`<button class="secondary" data-record="${esc(source.id)}" data-industry="${esc(source.industry_id)}">查看本地证据</button>`:''}${source.url&&researchChartURL(source.url)!=='#'?`<a class="secondary" href="${esc(researchChartURL(source.url))}" target="_blank" rel="noopener noreferrer">打开原始来源 ↗</a>`:''}</div></article>`).join('');
  dialog(point.label,`<p class="description">${esc(point.date||'日期未知')} · ${esc(chart.kind==='timeline'?chartDateKinds[point.date_kind]:'统计期间')} · ${esc(chartStatuses[point.status])}${chart.kind!=='timeline'?' · '+esc(point.value===null?'数值缺失':String(point.value)+' '+chart.unit):''}</p><p>${esc(point.summary)}</p>${point.quote?`<blockquote>${esc(point.quote)}</blockquote>`:''}${sourceMarkup}`);
}
function appendResearchChart(host,raw){
  const chart=validResearchChart(raw);
  if(!chart){const message=document.createElement('p');message.className='chart-limit';message.textContent='图表数据不完整，未绘制；请让助手核对依据和数据口径。';host.append(message);return;}
  const section=document.createElement('section');section.className='research-chart';
  const rows=chart.items.map((point,index)=>`<tr><td>${esc(point.date||'未知')}${chart.kind==='timeline'?`<small>${esc(chartDateKinds[point.date_kind])}</small>`:''}</td><td><button class="chart-source-button" type="button" data-chart-point="${index}">${esc(point.label)}</button><small>${esc(point.summary)}</small></td><td>${chart.kind==='timeline'?esc(chartStatuses[point.status]):esc(point.value===null?'缺失':String(point.value))}</td><td><button class="link-button" type="button" data-chart-point="${index}">查看依据 · ${point.sources.length}</button>${point.support==='quoted-extraction'?'<small>原文摘取，需核对</small>':''}</td></tr>`).join('');
  section.innerHTML=`<header><span class="chart-type">${chart.kind==='timeline'?'事件时间轴':chart.kind==='line'?'变化趋势 · 折线':'变化趋势 · 柱状'}</span><h3>${esc(chart.title)}</h3></header>${chart.description?`<p class="chart-description">${esc(chart.description)}</p>`:''}${chart.kind==='timeline'?timelineFigure(chart):trendFigure(chart)}<div class="chart-table-wrap"><table class="chart-table"><thead><tr><th>${chart.kind==='timeline'?'日期':'期间'}</th><th>${chart.kind==='timeline'?'事件 / 阶段':'指标节点'}</th><th>${chart.kind==='timeline'?'状态':esc(chart.unit)}</th><th>引用依据</th></tr></thead><tbody>${rows}</tbody></table></div><p class="chart-limit">${esc(chart.coverage_note||'助手依据本轮检索整理，请点击节点核对原文。')}${chart.note?' '+esc(chart.note):''}</p>`;
  const activate=e=>{const target=e.target.closest('[data-chart-point]');if(!target||!section.contains(target))return;if(e.type==='keydown'&&!['Enter',' '].includes(e.key))return;e.preventDefault();showChartPoint(chart,Number(target.dataset.chartPoint));};
  section.addEventListener('click',activate);section.addEventListener('keydown',activate);host.append(section);
}
