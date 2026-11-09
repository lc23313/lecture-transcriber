'use strict';
let versions=[],selectedRevision=null,editingVersion=false,loadedProject=null,vocabularyDirty=false,lastVocab='',workspaceLoading=false,aiRunning=false,lastPaint='';
const versionLabels={realtime:'实时初稿',filetrans:'完整音频原稿',user_raw:'用户编辑原稿',tidy_light:'轻度整理',tidy_deep:'深度整理',tidy_edit:'编辑后的整理稿'};
function page(name){document.querySelector('main').hidden=name!=='workspace';$('recordPage').hidden=name!=='record';$('settingsPage').hidden=name!=='settings';document.querySelectorAll('[data-page]').forEach(b=>b.setAttribute('aria-current',b.dataset.page===name?'page':'false'));}
document.querySelectorAll('[data-page]').forEach(b=>b.onclick=()=>page(b.dataset.page));page('workspace');
function jsonValue(s,fallback){try{return JSON.parse(s||'');}catch{return fallback;}}
function selectedSegments(){return selectedRevision?jsonValue(selectedRevision.segments,[]):[];}
window.renderVersion=function(reset){
  if(!job)return;
  if(loadedProject!==job.id){loadedProject=job.id;window.checkLocalDraft?.();selectedRevision=null;editingVersion=false;versions=[];vocabularyDirty=false;lastVocab='';$('revision').replaceChildren();loadWorkspace(true);}
  if(dirty&&!reset)return;
  const v=selectedRevision,parts=selectedSegments();
  const paintKey=JSON.stringify([job.id,v?.id,v?.text===undefined,v?.segments?.length,editingVersion,$('paragraphMode').checked,job.speakers]);if(!reset&&lastPaint===paintKey)return;lastPaint=paintKey;$('text').value=v?.text||job.original||'';$('text').readOnly=!editingVersion;
  $('paragraphs').replaceChildren();const paragraphView=$('paragraphMode').checked&&parts.length>0;
  $('text').hidden=paragraphView;
  if(paragraphView)for(const p of parts){
    const box=document.createElement('div');box.className='paragraph';const heading=document.createElement('div');heading.className='meta';
    const speakers=jsonValue(job.speakers,{});heading.textContent=(p.start==null||p.end==null?'时间未知':duration(p.start/1000)+' – '+duration(p.end/1000))+(p.speaker?' · '+(speakers[p.speaker]||p.speaker):'');
    const field=document.createElement(editingVersion?'textarea':'p');if(editingVersion){field.value=p.text;field.oninput=()=>dirty=true;}else field.textContent=p.text;
    box.append(heading,field);$('paragraphs').append(box);
  }
};
function paintVocabulary(){
  const value=jsonValue(job.vocab,{});$('vocabEntries').replaceChildren();
  for(const entry of value.entries||[])vocabRow(entry);
  $('vocabBackground').value=value.background||'';$('vocabContext').value=value.context||'';
  lastVocab=job.vocab||'';vocabularyDirty=false;
}
function vocabRow(entry={en:'',zh:'',weight:2}){
  const row=document.createElement('div');row.className='vocab-row';row._entry=entry;
  for(const [key,placeholder] of [['en','英文 / 专名 / 缩写'],['zh','中文'],['weight','权重']]){
    const input=document.createElement('input');input.placeholder=placeholder;input.value=entry[key]??'';input.dataset.key=key;input.setAttribute('aria-label',placeholder);
    if(key==='weight'){input.type='number';input.min=1;input.max=5;}input.oninput=()=>vocabularyDirty=true;row.append(input);
  }
  const remove=document.createElement('button');remove.textContent='删除';remove.onclick=()=>{row.remove();vocabularyDirty=true;};row.append(remove);$('vocabEntries').append(row);
}
window.renderWorkspace=function(reset){
  if(!job)return;
  if(reset){$('projectTitle').value=job.title||job.name;$('projectTopic').value=job.topic||'';$('projectDate').value=job.recorded_at||'';$('diarization').checked=!!job.diarization;}
  $('vocabStatus').textContent=job.vocab_status||'填写主题后，转录开始前自动生成；生成失败不阻止录音或识别';
  if(!vocabularyDirty&&lastVocab!==(job.vocab||''))paintVocabulary();
  $('save').disabled=!editingVersion||!selectedRevision;
  $('editVersion').disabled=!selectedRevision;$('lightTidy').disabled=!selectedRevision;$('deepTidy').disabled=!selectedRevision;
  for(const ext of ['txt','md','json'])$(ext).disabled=!selectedRevision;
  $('again').disabled=!!job.active||job.status==='RECORDING';
  if(reset){$('speakerEntries').replaceChildren();for(const [id,name] of Object.entries(jsonValue(job.speakers,{}))){const label=document.createElement('label');label.textContent=id;const input=document.createElement('input');input.value=name;input.dataset.speaker=id;label.append(input);$('speakerEntries').append(label);}}
};
async function loadWorkspace(newest=false){
  if(!current||workspaceLoading)return;workspaceLoading=true;const id=current;
  try{
    const [revs,actions]=await Promise.all([api('/api/jobs/'+id+'/versions?summary=true'),api('/api/jobs/'+id+'/actions')]);if(current!==id)return;
    const cached=new Map(versions.filter(v=>v.text!==undefined).map(v=>[v.id,v]));versions=revs.map(v=>cached.get(v.id)||v);const changed=revs[0]?.id!==$('revision').options[0]?.value;
    if(!dirty&&!editingVersion){
      const previous=selectedRevision?.id;$('revision').replaceChildren();
      for(const v of revs){const option=document.createElement('option');option.value=v.id;option.textContent=(versionLabels[v.kind]||v.kind)+' · '+new Date(v.created*1000).toLocaleString();$('revision').append(option);}
      selectedRevision=(newest||!previous)?versions[0]:versions.find(v=>v.id===previous)||versions[0];
      if(selectedRevision&&selectedRevision.text===undefined){const full=await api(`/api/jobs/${id}/versions/${selectedRevision.id}`);if(current!==id)return;selectedRevision=full;versions=versions.map(v=>v.id===full.id?full:v);}
      if(selectedRevision)$('revision').value=selectedRevision.id;
      if(changed&&!previous)editingVersion=false;
      window.renderVersion(false);
    }
    aiRunning=actions.some(a=>a.status==='RUNNING');
    $('aiActions').textContent=actions.slice(0,4).map(a=>`${a.kind}: ${a.status}${a.error?' · '+a.error:''}`).join(' / ');
    window.renderWorkspace(false);
    if(window.localProjectStore&&job)window.localProjectStore({...job,versions:versions.filter(v=>v.text!==undefined),cachedAt:new Date().toISOString()}).catch(()=>{});
  }catch(e){if(connected)toast(e.message);}finally{workspaceLoading=false;}
}
$('revision').onchange=async()=>{if(dirty&&!confirm('放弃未保存编辑并切换版本？')){$('revision').value=selectedRevision.id;return;}selectedRevision=versions.find(v=>v.id===$('revision').value);if(selectedRevision&&selectedRevision.text===undefined){try{const full=await api(`/api/jobs/${current}/versions/${selectedRevision.id}`);selectedRevision=full;versions=versions.map(v=>v.id===full.id?full:v);}catch(e){return toast(e.message);}}dirty=false;editingVersion=false;renderVersion(true);renderWorkspace(false);await window.localProjectStore?.({...job,versions:versions.filter(v=>v.text!==undefined),cachedAt:new Date().toISOString()});};
$('paragraphMode').onchange=()=>{if(dirty){$('paragraphMode').checked=!$('paragraphMode').checked;return toast('请先保存当前编辑再切换视图');}renderVersion(true);};
$('editVersion').onclick=()=>{editingVersion=true;renderVersion(true);renderWorkspace(false);};
$('save').onclick=async()=>{if(!selectedRevision)return;const id=current;let segments=[];let text=$('text').value;
  if($('paragraphMode').checked&&selectedSegments().length){segments=selectedSegments().map((p,i)=>({...p,text:$('paragraphs').querySelectorAll('textarea')[i].value}));text=segments.map(p=>p.text).join('\n\n');}
  try{const r=await api('/api/jobs/'+id+'/versions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({parent:selectedRevision.id,text,segments})});
    if(current===id){await window.clearLocalDraft?.();dirty=false;editingVersion=false;await loadWorkspace(true);await refresh();toast('已保存为独立版本，原版本仍保留');}
  }catch(e){toast(e.message);}
};
$('saveProject').onclick=async()=>{try{await api('/api/jobs/'+current,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:$('projectTitle').value,topic:$('projectTopic').value,recorded_at:$('projectDate').value,diarization:$('diarization').checked})});await refresh();toast('讲座信息已保存');}catch(e){toast(e.message);}};
const startWithMetadata=$('start').onclick;
$('start').onclick=async()=>{try{await api('/api/jobs/'+current,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:$('projectTitle').value,topic:$('projectTopic').value,recorded_at:$('projectDate').value,diarization:$('diarization').checked})});await startWithMetadata();}catch(e){toast(e.message);}};
$('saveSpeakers').onclick=async()=>{const speakers={};$('speakerEntries').querySelectorAll('input').forEach(i=>speakers[i.dataset.speaker]=i.value);try{await api('/api/jobs/'+current,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({speakers})});await refresh();renderVersion(false);toast('名称已保存');}catch(e){toast(e.message);}};
$('again').onclick=async()=>{if(!confirm('将创建新的识别任务，保留所有旧版本，可能产生费用。继续？'))return;try{const r=await api('/api/jobs/'+current+'/recognize-again',{method:'POST'});await openJob(r.id);}catch(e){toast(e.message);}};
$('addVocab').onclick=()=>{vocabRow();vocabularyDirty=true;};
for(const id of ['vocabBackground','vocabContext'])$(id).oninput=()=>vocabularyDirty=true;
$('saveVocab').onclick=async()=>{const entries=[...$('vocabEntries').children].map(row=>{const e={...row._entry};row.querySelectorAll('input').forEach(i=>e[i.dataset.key]=i.value);return e;});try{await api('/api/jobs/'+current+'/vocabulary',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({entries,background:$('vocabBackground').value,context:$('vocabContext').value})});vocabularyDirty=false;await refresh();toast('词汇表已保存');}catch(e){toast(e.message);}};
$('generateVocab').onclick=async()=>{try{await api('/api/jobs/'+current+'/vocabulary/generate',{method:'POST'});toast('词汇生成已开始');loadWorkspace();}catch(e){toast(e.message);}};
for(const [id,mode] of [['lightTidy','light'],['deepTidy','deep']])$(id).onclick=async()=>{if(dirty)return toast('请先保存编辑稿');try{await api('/api/jobs/'+current+'/tidy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode,version:selectedRevision.id})});toast('已手动发起整理，原稿不会改变');loadWorkspace();}catch(e){toast(e.message);}};
$('showDiff').onclick=async()=>{if(!selectedRevision?.parent)return toast('此版本没有来源版本可比较');try{const d=await api(`/api/jobs/${current}/diff?before=${selectedRevision.parent}&after=${selectedRevision.id}`);$('diff').textContent=d.lines.join('\n')||'两个版本文本相同';$('diff').hidden=false;}catch(e){toast(e.message);}};
for(const ext of ['txt','md'])$(ext).onclick=()=>{if(!selectedRevision)return;
  if(!connected){const names=jsonValue(job.speakers,{});const parts=selectedSegments();let text=selectedRevision.text;
    if(parts.length&&($('includeTime').checked||$('includeSpeaker').checked))text=parts.map(p=>($('includeTime').checked?(p.start==null||p.end==null?'[时间未知] ':`[${duration(p.start/1000)}–${duration(p.end/1000)}] `):'')+($('includeSpeaker').checked&&p.speaker?(names[p.speaker]||p.speaker)+': ':'')+p.text).join('\n\n');
    if(ext==='md')text='# '+(job.title||job.name)+'\n\n'+text;
    import(window.ltAsset('static/local-db.js')).then(db=>db.downloadBlob(new Blob(['\uFEFF'+text],{type:'text/plain'}),'transcript.'+ext));return;
  }
  const q=new URLSearchParams({version:selectedRevision.id,format:ext,timestamps:$('includeTime').checked,speakers:$('includeSpeaker').checked});window.ltDownload(`/api/jobs/${current}/export?${q}`).catch(e=>toast(e.message));};
$('json').onclick=()=>{if(!selectedRevision)return;const blob=new Blob([JSON.stringify({project:job,version:selectedRevision},null,2)],{type:'application/json'});import(window.ltAsset('static/local-db.js')).then(db=>db.downloadBlob(blob,'transcript.json'));};
$('backupProject').onclick=()=>{window.ltDownload('/api/jobs/'+current+'/backup').catch(e=>toast(e.message));};$('downloadAudio').onclick=()=>{window.ltDownload('/api/jobs/'+current+'/audio').catch(e=>toast(e.message));};
$('restoreProject').onchange=async e=>{const file=e.target.files[0];if(!file)return;$('restoreStatus').textContent='正在恢复，请稍候…';try{const r=await api('/api/restore',{method:'POST',body:file});$('restoreStatus').textContent='恢复完成，原项目未被覆盖';page('workspace');await openJob(r.id);}catch(e){$('restoreStatus').textContent=e.message;}finally{e.target.value='';}};
$('saveSettings').onclick=async()=>{try{await api('/api/config',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({api_key:$('settingKey').value,workspace_id:$('settingWorkspace').value,region:$('settingRegion').value,llm_model:$('settingModel').value,llm_base_url:$('settingBase').value,llm_api_key:$('settingLlmKey').value})});$('settingKey').value='';$('settingLlmKey').value='';const c=await api('/api/config');$('config').textContent=c.message;toast('API 配置已保存');}catch(e){toast(e.message);}};
$('loginButton').onclick=async()=>{try{const token=$('accessToken').value;await api('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})});window.ltRememberToken(token);$('accessToken').value='';$('loginDialog').close();location.reload();}catch(e){$('loginError').textContent=e.message;}};
async function reloadConfiguration(){try{const c=await api('/api/config');$('config').textContent=c.message;$('settingWorkspace').value=c.workspace_id||'';$('settingRegion').value=c.region||'cn-beijing';$('settingModel').value=c.llm_model||'';$('settingBase').value=c.llm_base_url||'';}catch{$('config').textContent=window.ltStaticHost&&!window.ltConnected()?'本地采集模式 · 可录音和导出；识别与整理需连接 HTTPS 后端':'当前无法连接后端；可使用本机录音与已保存的稿件副本。';}}
reloadConfiguration();
window.addEventListener('online',()=>{reloadConfiguration();refresh().then(rows=>{if(rows?.length&&!current)openJob(rows.find(r=>r.status==='SUCCEEDED')?.id||rows[0].id);});});
import(window.ltAsset('static/local-db.js')).then(db=>{
  window.localProjectStore=async value=>{await db.merge('projects',value.id,value,(old,next)=>{const merged=new Map((old.versions||[]).map(v=>[v.id,v]));for(const v of next.versions||[])merged.set(v.id,v);return {...old,...next,versions:[...merged.values()].sort((a,b)=>b.created-a.created)};});};
  window.cacheProjectRows=async rows=>{const old=new Map((await db.all('projects')).map(p=>[p.id,p]));for(const r of rows){const value={...r};delete value.server_time;const prior=old.get(r.id);if(!prior||Object.entries(value).some(([k,v])=>JSON.stringify(v)!==JSON.stringify(prior[k])))await db.merge('projects',value.id,value);}};
  window.openOfflineProject=async id=>{const p=await db.get('projects',id);if(!p)return false;current=p.id;job=p;loadedProject=p.id;versions=p.versions||[];selectedRevision=versions[0];$('revision').replaceChildren();for(const v of versions){const o=document.createElement('option');o.value=v.id;o.textContent=versionLabels[v.kind]||v.kind;$('revision').append(o);}$('empty').hidden=true;$('detail').hidden=false;$('name').textContent=p.title||p.name;dirty=false;editingVersion=false;connected=false;renderVersion(true);renderWorkspace(true);$('connection').hidden=false;$('connection').textContent='正在查看本地副本 · 保存于 '+(p.cachedAt?new Date(p.cachedAt).toLocaleString():'未知时间');window.checkLocalDraft?.();return true;};
  window.showOfflineProjects=async()=>{const cached=await db.all('projects');if(!cached.length)return;$('list').replaceChildren();for(const p of cached){const b=document.createElement('button');b.className='item';b.textContent=(p.title||p.name)+' · 本地副本';b.onclick=async()=>{if(dirty&&!confirm('当前编辑已自动保存为本地草稿，切换项目？'))return;await window.openOfflineProject(p.id);};$('list').append(b);}};
  let draftTimer;let draft=null;
  async function checkDraft(){if(!current)return;const id=current;const saved=await db.get('drafts',id);if(current!==id)return;draft=saved;$('restoreDraft').hidden=!saved;$('draftStatus').textContent=saved?'有未提交草稿 · '+new Date(saved.saved).toLocaleString():'';}
  window.checkLocalDraft=checkDraft;
  window.clearLocalDraft=async()=>{clearTimeout(draftTimer);const saved=await db.get('drafts',current);if(saved){saved.source=null;saved.editing=false;saved.text='';saved.paragraphs=[];await db.put('drafts',saved);}draft=saved;$('restoreDraft').hidden=!saved;$('draftStatus').textContent='稿件已提交；其他未提交字段仍保留本地草稿';};
  document.getElementById('detail').addEventListener('input',()=>{
    if(!current)return;clearTimeout(draftTimer);
    const fields={};for(const id of ['projectTitle','projectTopic','projectDate','vocabBackground','vocabContext'])fields[id]=$(id).value;
    const snapshot={id:current,saved:new Date().toISOString(),source:selectedRevision,editing:editingVersion,text:$('text').value,paragraphs:[...$('paragraphs').querySelectorAll('textarea')].map(t=>t.value),fields,
      vocab:[...$('vocabEntries').children].map(row=>{const e={...row._entry};row.querySelectorAll('input').forEach(i=>e[i.dataset.key]=i.value);return e;}),speakers:[...$('speakerEntries').querySelectorAll('input')].map(i=>[i.dataset.speaker,i.value]),diarization:$('diarization').checked};
    draftTimer=setTimeout(()=>db.put('drafts',snapshot).then(()=>{$('draftStatus').textContent='未提交编辑已保存到当前设备';}).catch(()=>toast('本地草稿保存失败，请立即保存或导出编辑内容')),400);
  });
  $('restoreDraft').onclick=()=>{if(!draft)return;const saved=draft;if(saved.source){selectedRevision=saved.source;editingVersion=saved.editing;renderVersion(true);$('text').value=saved.text;$('paragraphs').querySelectorAll('textarea').forEach((t,i)=>t.value=saved.paragraphs[i]??t.value);}
    for(const [id,value] of Object.entries(saved.fields))$(id).value=value;$('diarization').checked=saved.diarization;$('vocabEntries').replaceChildren();for(const e of saved.vocab)vocabRow(e);vocabularyDirty=true;
    for(const [id,name] of saved.speakers){const input=[...$('speakerEntries').querySelectorAll('input')].find(i=>i.dataset.speaker===id);if(input)input.value=name;}
    dirty=true;renderWorkspace(false);$('draftStatus').textContent='已恢复草稿，请分别保存稿件、讲座信息或词汇修改';};
  checkDraft();
}).catch(()=>toast('浏览器本地缓存不可用，请导出重要数据'));
if('serviceWorker' in navigator)navigator.serviceWorker.register(window.ltAsset('sw.js')).catch(()=>toast('离线资源缓存未能启用'));
$('deviceCheck').onclick=async()=>{
  const types=['audio/mp4','audio/webm;codecs=opus','audio/webm','audio/ogg;codecs=opus'];
  const report={time:new Date().toISOString(),userAgent:navigator.userAgent,secureContext:isSecureContext,
    standalone:matchMedia('(display-mode:standalone)').matches||!!navigator.standalone,
    mediaRecorder:!!window.MediaRecorder,microphone:!!navigator.mediaDevices?.getUserMedia,indexedDB:!!window.indexedDB,
    audioWorklet:!!window.AudioWorkletNode,wakeLock:'wakeLock' in navigator,opfs:!!navigator.storage?.getDirectory,
    mimeTypes:Object.fromEntries(types.map(t=>[t,!!window.MediaRecorder&&MediaRecorder.isTypeSupported(t)]))};
  try{report.storage=await navigator.storage?.estimate?.();report.persistent=await navigator.storage?.persisted?.();}catch{report.storage='无法获取';}
  $('deviceReport').textContent=JSON.stringify(report,null,2);
  import(window.ltAsset('static/local-db.js')).then(db=>db.downloadBlob(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}),'device-report.json'));
};
if(window.ltStaticHost){
  $('backendConnection').hidden=false;$('backendAddress').value=window.ltBackend();
  $('backendStatus').textContent=window.ltConnected()?'已设置后端地址，需登录并实际连接验证':'未连接后端：可用本地录音、保存和导出；云端功能尚未连接';
  if(!window.ltConnected()){$('connection').textContent='GitHub Pages 网页已就绪。当前未连接转录后端，可先使用本地录音和导出。';$('config').textContent='本地采集模式 · 请在 API 设置中连接 HTTPS 后端后使用识别与整理';}
  $('saveBackend').onclick=()=>{try{window.ltSaveBackend($('backendAddress').value);location.reload();}catch(e){toast(e.message);}};
  $('disconnectBackend').onclick=()=>{window.ltSaveBackend('');location.reload();};
}
async function scheduleWorkspace(){if(!document.hidden&&connected)await loadWorkspace();setTimeout(scheduleWorkspace,job?.active||aiRunning?5000:20000);}
setTimeout(scheduleWorkspace,5000);
$('stopWaiting').onclick=async()=>{try{const r=await api(`/api/jobs/${current}/stop-waiting`,{method:'POST'});toast(r.message);}catch(e){toast(e.message);}};
$('showRuns').onclick=async()=>{try{$('modelRuns').textContent=JSON.stringify(await api(`/api/jobs/${current}/runs`),null,2);$('modelRuns').hidden=false;}catch(e){toast(e.message);}};
$('deleteProject').onclick=async()=>{if(!confirm('删除当前项目的音频、所有稿件和调用记录？请先确认已导出备份，此操作无法撤销。'))return;try{await api(`/api/jobs/${current}`,{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm:current})});const db=await import(window.ltAsset('static/local-db.js'));await db.remove('projects',current);await db.remove('drafts',current);current=null;job=null;selectedRevision=null;$('detail').hidden=true;$('empty').hidden=false;dirty=false;await refresh();}catch(e){toast(e.message);}};
