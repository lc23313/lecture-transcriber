'use strict';
const $ = id => document.getElementById(id);
const BUSY = new Set(['WAITING_UPLOAD','UPLOADING','SUBMITTING','PENDING','RUNNING','FETCHING_RESULT']);
const labels = {RECORDING:'现场录音项目',LOCAL:'已保存到本机',WAITING_UPLOAD:'等待上传',UPLOADING:'正在上传音频',SUBMITTING:'正在提交识别',PENDING:'云端排队',RUNNING:'正在识别',FETCHING_RESULT:'正在保存完整稿',SUCCEEDED:'完整稿已生成',FAILED:'处理失败',INTERRUPTED:'处理曾中断'};
let current = null, job = null, dirty = false, connected = true, refreshing = false, starting = false;
let serverOffset = 0, toastTimer, importing = false;

async function api(url, options) {
  let r;
  const target=window.ltApiUrl(url);
  try{r = await fetch(target, {cache:'no-store', ...options,credentials:window.ltStaticHost?'omit':'same-origin',headers:{...window.ltHeaders(),...(options?.headers||{})}});}catch{throw Error('暂时无法连接后端；本机录音继续保存，网络恢复后可重试');}
  const v = await r.json();
  if(r.status===401) document.getElementById('loginDialog')?.showModal();
  if (!r.ok) throw Error(v.detail || '操作失败');
  return v;
}
function toast(text) {
  $('message').textContent = text;
  $('message').hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $('message').hidden = true, 6000);
}
function bytes(n) { return n >= 1e6 ? (n/1e6).toFixed(1)+' MB' : Math.round(n/1000)+' KB'; }
function duration(seconds) {
  const n = Math.max(0,Math.floor(seconds));
  const m = Math.floor(n/60), s = String(n%60).padStart(2,'0');
  return m >= 60 ? `${Math.floor(m/60)}:${String(m%60).padStart(2,'0')}:${s}` : `${m}:${s}`;
}
function bar(id, percent, {indeterminate=false,failed=false,paused=false,text=''} = {}) {
  const el = $(id);
  el.className = 'bar'+(indeterminate?' indeterminate':'')+(failed?' failed':'')+(paused?' paused':'');
  el.firstElementChild.style.width = indeterminate ? '' : Math.max(0,Math.min(100,percent))+'%';
  if (indeterminate) el.removeAttribute('aria-valuenow');
  else el.setAttribute('aria-valuenow', String(percent));
  el.setAttribute('aria-valuetext',text);
}
function renderProgress() {
  if (!job) return;
  const state = job.status;
  const stopped = state==='FAILED'||state==='INTERRUPTED';
  // Legacy completed tasks have no byte counters, but a task ID proves their upload finished.
  const uploaded = !!job.upload_completed || !!job.task || state==='SUCCEEDED';
  const sent = job.upload_sent || 0, total = job.upload_total || 0;
  const percent = uploaded ? 100 : total ? Math.min(99,Math.floor(sent/total*100)) : 0;
  $('cloudPercent').textContent = uploaded ? '100%' : total ? percent+'%' : state==='WAITING_UPLOAD' ? '等待中' : '待上传';
  let uploadLabel = '音频已在本机，尚未发送';
  if (uploaded) uploadLabel = '上传完成，服务已接收';
  else if (state==='WAITING_UPLOAD') uploadLabel = '等待上传通道，请保持程序运行';
  else if (state==='UPLOADING') uploadLabel = total && sent>=total ? '音频已发送，等待服务确认…' : total ? '正在发送到百炼临时空间' : '正在准备上传…';
  else if (stopped) uploadLabel = '上传未确认完成，本机音频保留';
  if (!connected && BUSY.has(state)) uploadLabel = '状态连接中断，显示最后获取的进度';
  $('cloudLabel').textContent = uploadLabel;
  $('cloudMeta').textContent = total ? `${bytes(sent)} / ${bytes(total)}${!uploaded && sent>=total?' · 尚待服务确认':''}` : uploaded ? '无需再次上传' : '';
  bar('cloudBar',percent,{failed:stopped&&!uploaded,paused:!connected,text:uploadLabel});

  let value='待开始', label='上传完成后开始识别', indeterminate=false;
  if (state==='SUBMITTING') { value='提交中'; label='正在创建完整音频识别任务'; indeterminate=true; }
  if (state==='PENDING') { value='排队中'; label='百炼已接收任务，正在等待识别'; indeterminate=true; }
  if (state==='RUNNING') { value='识别中'; label='正在识别完整音频，请保持程序运行'; indeterminate=true; }
  if (state==='FETCHING_RESULT') { value='保存中'; label='识别完成，正在下载并保存完整稿'; indeterminate=true; }
  if (state==='SUCCEEDED') { value='100%'; label='完整原始稿已保存到本机'; }
  if (stopped) { value=state==='FAILED'?'失败':'已中断'; label=job.task?'已有任务可继续查询，原音频保留':'请查看上方提示，原音频保留'; }
  if (!connected && BUSY.has(state)) { value='连接中断'; label='暂时无法更新识别状态'; indeterminate=false; }
  $('recognitionValue').textContent=value;
  $('recognitionLabel').textContent=label;
  bar('recognitionBar',state==='SUCCEEDED'?100:0,{indeterminate,failed:stopped,paused:!connected,text:label});
  let meta='';
  if (job.recognition_started) {
    const end=job.finished || (connected ? Date.now()/1000+serverOffset : job.server_time);
    meta=(job.finished?'用时 ':'已等待 ')+duration(end-job.recognition_started);
  }
  if (indeterminate) meta+=(meta?' · ':'')+'服务不返回识别百分比';
  $('recognitionMeta').textContent=meta;
}
function render(reset) {
  if (!job) return;
  $('empty').hidden=true; $('detail').hidden=false;
  $('name').textContent=job.title||job.name;
  $('state').textContent=(labels[job.status]||job.status)+(job.cleanup?' · '+job.cleanup:'');
  $('error').textContent=job.error||'';
  $('start').hidden=job.status==='SUCCEEDED';
  const running=job.active||BUSY.has(job.status);
  $('start').disabled=running||starting;
  $('start').textContent=running?'正在处理…':job.task?'继续查询已有任务':job.status==='FAILED'?'重新上传并转录':'临时上传并转录';
  if (reset||!dirty) {
    if(window.renderVersion) window.renderVersion(reset);
    else {
    const editable=$('version').value==='edited';
    $('text').readOnly=!editable||job.status!=='SUCCEEDED';
    $('text').value=editable?(job.edited??job.original??''):(job.original??'');
    }
  }
  $('save').disabled=$('version').value!=='edited'||job.status!=='SUCCEEDED';
  for (const ext of ['txt','md','json']) $(ext).disabled=job.status!=='SUCCEEDED';
  if(window.renderWorkspace)window.renderWorkspace(reset);
  renderProgress();
}
function acceptJob(next) {
  job=next;
  serverOffset=job.server_time-Date.now()/1000;
}
async function refresh() {
  if (refreshing) return;
  refreshing=true;
  try {
    const rows=await api('/api/jobs');
    window.cacheProjectRows?.(rows);
    $('list').replaceChildren();
    for (const r of rows) {
      const b=document.createElement('button'); b.className='item'+(current===r.id?' selected':'');
      const n=document.createElement('div'); n.textContent=r.title||r.name;
      const s=document.createElement('div'); s.className='status';
      let title=labels[r.status]||r.status;
      if (r.status==='UPLOADING'&&r.upload_total) title+=' · '+Math.min(99,Math.floor((r.upload_sent||0)/r.upload_total*100))+'%';
      s.textContent=title; b.append(n,s); b.onclick=()=>openJob(r.id); $('list').append(b);
    }
    const id=current;
    if (id) {
      const next=await api('/api/jobs/'+id);
      if (current===id) { acceptJob(next); connected=true; render(false); }
    }
    connected=true; $('connection').hidden=true;
    return rows;
  } catch (e) {
    connected=false; $('connection').hidden=false; renderProgress();window.showOfflineProjects?.();
  } finally { refreshing=false; }
}
async function openJob(id) {
  if (dirty&&!confirm('编辑内容尚未保存，是否放弃？')) return;
  try {
    const next=await api('/api/jobs/'+id);
    current=id; dirty=false; connected=true; acceptJob(next); render(true);
    await refresh();
  } catch (e) { toast(e.message); }
}
function importFile(file) {
  if(window.ltStaticHost&&!window.ltConnected())return import(window.ltAsset('static/local-db.js')).then(async db=>{
    const id=crypto.randomUUID();const meta={id,title:file.name,name:file.name,mime:file.type,created:new Date().toISOString(),state:'imported',duration:0,seq:1,bytes:file.size,live:[]};
    await db.saveChunk(meta,file,0);return {local:true,id};
  });
  return new Promise((resolve,reject)=>{
    const xhr=new XMLHttpRequest();
    xhr.open('POST',window.ltApiUrl('/api/audio?name='+encodeURIComponent(file.name)));
    for(const [k,v] of Object.entries(window.ltHeaders()))xhr.setRequestHeader(k,v);
    xhr.upload.onprogress=e=>{
      if (!e.lengthComputable) return;
      const p=Math.min(99,Math.floor(e.loaded/e.total*100));
      $('importPercent').textContent=p+'%';
      $('importMeta').textContent=`${bytes(e.loaded)} / ${bytes(e.total)}`;
      $('importLabel').textContent=e.loaded>=e.total?'等待本机保存确认…':'正在保存到本机';
      bar('importBar',p,{text:$('importLabel').textContent});
    };
    xhr.onload=()=>{
      let result;
      try { result=JSON.parse(xhr.responseText); } catch { return reject(Error('保存失败：本机程序返回异常，请重试')); }
      if (xhr.status<200||xhr.status>=300) return reject(Error(result.detail||'保存失败'));
      resolve(result);
    };
    xhr.onerror=()=>reject(Error('与本机程序连接中断，尚未确认保存，请检查程序后重试'));
    xhr.onabort=()=>reject(Error('导入已中断，尚未确认保存'));
    xhr.send(file);
  });
}
$('file').onchange=async e=>{
  const f=e.target.files[0]; if (!f||importing) return;
  if (f.size>1_000_000_000) { toast('临时上传支持的音频上限为 1 GB，请选择更小的文件'); $('file').value=''; return; }
  if (!f.size) { toast('文件为空'); $('file').value=''; return; }
  importing=true; $('file').disabled=true; $('importProgress').hidden=false;
  $('importPercent').textContent='0%'; $('importLabel').textContent='正在保存到本机';
  $('importMeta').textContent=`0 KB / ${bytes(f.size)}`; bar('importBar',0);
  try {
    const r=await importFile(f);
    $('importPercent').textContent='100%'; $('importLabel').textContent='已安全保存到本机';
    $('importMeta').textContent=bytes(f.size); bar('importBar',100,{text:'本机已确认保存'});
    if(r.local){await window.refreshLocalRecordings?.();window.page?.('record');toast('音频已保存到本机；连接后端后可在本机列表中提交转录');}
    else {await openJob(r.id); toast('音频已保存在本机，可以开始转录');}
  } catch (e) {
    $('importLabel').textContent='保存未完成';
    $('importPercent').textContent='失败'; $('importMeta').textContent=e.message;
    $('importBar').classList.add('failed'); toast(e.message);
  } finally { importing=false; $('file').disabled=false; $('file').value=''; }
};
$('start').onclick=async()=>{
  if (!job||starting||job.active||BUSY.has(job.status)) return;
  if (!job.task&&!confirm('将向百炼临时空间发送这份音频并调用识别，可能产生费用。临时文件约 48 小时自动清理，无法提前删除；此通道仅供开发测试。是否继续？')) return;
  const id=current;
  starting=true; render(false);
  try {
    await api('/api/jobs/'+id+'/start',{method:'POST'});
    toast(job.task?'已继续查询任务':'已开始上传与转录，请保持程序运行');
    await refresh();
  } catch (e) { toast(e.message); }
  finally { starting=false; render(false); }
};
$('text').oninput=()=>dirty=true;
$('version').onchange=()=>{
  if (dirty&&!confirm('编辑内容尚未保存，是否放弃？')) { $('version').value='edited'; return; }
  dirty=false; render(true);
};
$('save').onclick=async()=>{
  const id=current, text=$('text').value;
  try {
    await api('/api/jobs/'+id+'/edit',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});
    if (id===current&&text===$('text').value) dirty=false;
    toast('编辑稿及历史快照已保存'); await refresh();
  } catch(e) { toast(e.message); }
};
function download(ext) {
  if (!job||job.status!=='SUCCEEDED') return toast('请等待识别完成');
  let text=ext==='json'?JSON.stringify({...job,exported_at:new Date().toISOString()},null,2):$('text').value;
  if(ext==='md') text='# '+job.name+'\n\n'+text;
  const url=URL.createObjectURL(new Blob([ext==='txt'?'\uFEFF'+text:text],{type:'text/plain;charset=utf-8'}));
  const a=document.createElement('a'); a.href=url; a.download=job.name+'.'+ext; a.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
}
for (const ext of ['txt','md','json']) $(ext).onclick=()=>download(ext);
window.onbeforeunload=e=>{ if(dirty||importing){e.preventDefault();e.returnValue='';} };
refresh().then(rows=>{if(rows?.length&&!current)openJob(rows.find(r=>r.active)?.id||rows[0].id);});
setInterval(refresh,1000);
setInterval(renderProgress,1000);
