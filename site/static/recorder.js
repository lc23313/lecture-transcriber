import {all,put,get,merge,saveAudio,saveChunk,audioBlob,downloadBlob,deleteRecording} from './local-db.js';
import {RecordingSession} from './recording-session.js';
import {uploadRecording,resumeQueue} from './upload-queue.js';
const el=id=>document.getElementById(id);let active=null,starting=false;const emergencies=[];
const current=s=>active===s;
const seconds=s=>s.accumulated+(s.media?.state==='recording'?(performance.now()-s.activeStart)/1000:0);
function note(message){el('recordNotice').textContent=message;}
function status(s,id,text){if(current(s))el(id).textContent=text;}
async function persist(s){await s.queue;if(s.failed)return;await merge('recordings',s.meta.id,{state:s.meta.state,duration:s.meta.duration,live:[...s.lines.entries()],realtimeGap:!!s.meta.realtimeGap});}
async function lock(s){try{if(navigator.wakeLock&&document.visibilityState==='visible')s.wake=await navigator.wakeLock.request('screen');status(s,'recordWake',s.wake?'屏幕常亮已请求':'请手动保持屏幕常亮');}catch{status(s,'recordWake','常亮请求未获允许，请手动保持屏幕常亮');}}
function renderLive(s){if(!current(s))return;const values=[...s.lines.values()];el('liveDocument').textContent=values.map(x=>x.text).join('\n');el('liveSubtitle').textContent=values.at(-1)?.text||'等待识别文字…';}
async function prepare(m){const backend=window.ltBackend();if(m.jid&&m.backend===backend)return m.jid;const r=await window.api('/api/recordings/prepare',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(m)});m.jid=r.id;m.backend=backend;await put('recordings',m);return r.id;}
async function connectLive(s){
  if(s.connecting||!current(s)||s.ended||s.media.state!=='recording'||!s.enableLive||!navigator.onLine||s.socket?.readyState<=1)return;
  s.connecting=true;
  try{
    status(s,'recordLive','正在连接实时服务…');const jid=await prepare(s.meta);if(s.ended||!current(s)||s.media.state!=='recording')return;
    const connection=new WebSocket(window.ltLiveUrl(`/api/live/${jid}`),window.ltLiveProtocols());s.socket=connection;s.sentStart=false;
    connection.onopen=()=>connection.send(JSON.stringify({offset:Math.floor(seconds(s)*1000)}));
    connection.onmessage=e=>{if(s.socket!==connection)return;const value=JSON.parse(e.data);
      if(value.type==='ready'&&!s.ended){s.ready=true;status(s,'recordLive','实时识别已连接');s.drain();}
      if(value.type==='sentence'){s.lines.set(value.session+':'+value.seq,value.segment);renderLive(s);persist(s).catch(()=>{if(current(s))note('实时文字本地保存失败，请检查音频保存状态');});}
      if(value.type==='error'){s.ready=false;status(s,'recordLive',value.message);}
    };
    connection.onclose=()=>{if(s.socket!==connection)return;s.ready=false;s.socket=null;clearTimeout(s.closeTimer);persist(s).catch(()=>{});
      if(!s.ended){status(s,'recordLive','实时连接断开；录音继续，完整重识别补齐文字');s.retry=setTimeout(()=>connectLive(s),5000);}};
    connection.onerror=()=>{};
  }catch{status(s,'recordLive','实时连接失败；本地录音继续');if(!s.ended)s.retry=setTimeout(()=>connectLive(s),10000);}finally{s.connecting=false;}
}
function pause(s){if(s.media?.state!=='recording')return;s.accumulated=seconds(s);s.media.requestData();s.media.pause();
  s.ready=false;clearTimeout(s.retry);s.socket?.close();s.frames=[];
  status(s,'pauseRecord','继续');status(s,'recordState','已暂停');}
async function release(s){s.finish();s.node?.disconnect();s.stream?.getTracks().forEach(t=>t.stop());await s.ctx?.close().catch(()=>{});await s.wake?.release().catch(()=>{});}
async function start(){
  if(starting||active?.media&&active.media.state!=='inactive')return;starting=true;el('startRecord').disabled=true;
  const s=new RecordingSession({});active=s;
  try{
    if(!isSecureContext||!navigator.mediaDevices?.getUserMedia||!window.MediaRecorder)throw Error('请使用 HTTPS 和支持录音的浏览器');
    await navigator.storage?.persist?.();const space=await navigator.storage?.estimate?.();if(space?.quota&&space.quota-space.usage<30_000_000)throw Error('可用本地空间不足30MB，请先导出重要录音并清理');
    s.stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1},video:false});
    const mime=['audio/mp4','audio/webm;codecs=opus','audio/webm','audio/ogg;codecs=opus'].find(t=>MediaRecorder.isTypeSupported(t));
    s.media=new MediaRecorder(s.stream,mime?{mimeType:mime,audioBitsPerSecond:96000}:{audioBitsPerSecond:96000});s.enableLive=el('enableLive').checked;
    const ext=s.media.mimeType.includes('mp4')?'m4a':s.media.mimeType.includes('ogg')?'ogg':'webm';
    s.meta={id:crypto.randomUUID(),title:el('recordTitle').value||'现场录音',topic:el('recordTopic').value,name:'现场录音.'+ext,mime:s.media.mimeType,created:new Date().toISOString(),state:'recording',duration:0,seq:0,bytes:0,live:[]};
    await put('recordings',s.meta);renderLive(s);status(s,'recordSave','等待首个片段保存');
    s.media.ondataavailable=e=>{if(!e.data.size)return;const blob=e.data;const seq=s.meta.seq++;s.meta.bytes+=blob.size;s.meta.duration=seconds(s);const snapshot={...s.meta};s.pending++;status(s,'recordSave',`正在保存片段（${s.pending} 段待确认）`);
      s.queue=s.queue.then(async()=>{if(s.failed){s.unsaved.push(blob);return;}try{await saveChunk({...snapshot,live:[...s.lines.entries()]},blob,seq);status(s,'recordSave',`已保存 ${seq+1} 段 · ${(s.meta.bytes/1e6).toFixed(1)} MB`);}catch(error){s.failed=true;s.unsaved.push(blob);emergencies.push(s);pause(s);if(current(s))note('本地写入失败，已暂停。请立即导出音频与未保存片段，清理后开始新录音。');status(s,'recordSave','保存失败：'+error.name);}}).finally(()=>s.pending--);
      if(s.pending>=5){pause(s);note('本地保存速度不足，已暂停，请等待保存完成后继续');}
    };
    s.media.onerror=()=>{note('录音器发生错误，已保存片段保留');stop(s);};
    s.stream.getAudioTracks()[0].onended=()=>{if(!s.ended){note('麦克风已中断，已保存音频保留');stop(s);}};
    s.media.onstop=async()=>{
      await s.queue;s.meta.state=s.failed?'storage-error':'stopped';s.meta.duration=s.accumulated;
      if(!s.failed)try{await persist(s);}catch{note('结束状态未能保存，请立即导出音频');}
      await release(s);if(current(s)){el('enableLive').disabled=window.ltStaticHost&&!window.ltConnected();el('startRecord').disabled=false;el('pauseRecord').disabled=true;el('stopRecord').disabled=true;status(s,'recordState',s.failed?'存储异常，已停止':'录音已结束');}await recordings();
    };
    s.activeStart=performance.now();s.media.start(2000);await lock(s);
    el('enableLive').disabled=true;el('pauseRecord').disabled=false;el('stopRecord').disabled=false;status(s,'pauseRecord','暂停');status(s,'recordState','正在录音');note('仅支持前台录音，请保持页面打开和屏幕亮起。片段保存确认后才视为已保存，请及时导出备份。');
    if(s.enableLive){try{
      s.ctx=new AudioContext();await s.ctx.resume();await s.ctx.audioWorklet.addModule(window.ltAsset('static/pcm-worklet.js'));
      const source=s.ctx.createMediaStreamSource(s.stream);s.node=new AudioWorkletNode(s.ctx,'pcm16');const silent=s.ctx.createGain();silent.gain.value=0;source.connect(s.node);s.node.connect(silent);silent.connect(s.ctx.destination);
      s.node.port.onmessage=e=>{if(s.media.state==='recording'&&!s.ended){const hadGap=s.meta.realtimeGap;s.buffer(e.data,seconds(s)*1000-100);if(s.meta.realtimeGap&&!hadGap)note('实时缓冲已满，部分实时文字可能缺失；原始音频继续保存，请结束后识别完整音频');s.drain();if(s.socket?.bufferedAmount>=64000)s.socket.close();}};connectLive(s);
    }catch{note('实时采集初始化失败，本地录音继续，可结束后识别完整音频');}}else status(s,'recordLive','实时识别未开启，不发送实时音频');
  }catch(error){if(s.media?.state==='recording')stop(s);else await release(s);note(error.name==='NotAllowedError'?'麦克风权限被拒绝，请在浏览器设置中允许麦克风':error.message);el('startRecord').disabled=false;}
  finally{starting=false;}
}
function stop(s=active){if(!s?.media||s.media.state==='inactive')return;s.accumulated=seconds(s);if(s.frames.length)s.meta.realtimeGap=true;s.ended=true;s.media.stop();status(s,'recordState','正在保存最后片段…');}
const uploadOptions={notice:note,prepare,progress:(sent,total,done)=>{el('recordTransfer').textContent=`设备上传到私人后端：${done?100:Math.min(99,Math.floor(sent/total*100))}% · ${(sent/1e6).toFixed(1)} / ${(total/1e6).toFixed(1)} MB${done?' 已确认保存':''}`;el('recordUploadProgress').hidden=false;el('recordUploadProgress').value=done?100:Math.min(99,sent/total*100);}};
async function recordings(){
  const list=el('localRecordings');list.replaceChildren();const items=(await all('recordings')).sort((a,b)=>b.created.localeCompare(a.created));el('localStorageUsed').textContent=`本机录音占用 ${(items.reduce((n,m)=>n+m.bytes,0)/1e6).toFixed(1)} MB`;
  for(const m of items){const box=document.createElement('div');box.className='local-recording';const text=document.createElement('p');const busy=m.id===active?.meta.id&&active.media?.state!=='inactive';
    text.textContent=`${m.title} · ${Math.floor(m.duration||0)} 秒 · ${(m.bytes/1e6).toFixed(1)} MB · ${busy?'当前录音':m.state==='recording'?'异常退出后恢复的片段，末尾可能不完整':m.state}${m.pendingUpload?' · 待上传':''}${m.exported?' · 已请求导出备份':''}`;
    const button=(label,fn)=>{const b=document.createElement('button');b.textContent=label;b.onclick=async()=>{b.disabled=true;try{await fn();}catch(e){note(e.message+'；原片段保留');}finally{b.disabled=busy;}};b.disabled=busy;box.append(b);};
    box.append(text);
    button('导出已保存音频',async()=>{downloadBlob(await audioBlob(m),m.name);m.exported=new Date().toISOString();await put('recordings',m);await recordings();note('已发起音频导出，请在文件中确认保存并试听，数据库校验不等于全程可播放');});
    button('导出实时稿与录音信息',()=>downloadBlob(new Blob([JSON.stringify(m,null,2)],{type:'application/json'}),'recording.json'));
    button(m.uploaded?'已提交（可再次查询）':'上传完整音频并转录',async()=>{await uploadRecording(m,uploadOptions);await recordings();});
    button('删除本机录音',async()=>{if(!confirm(`删除“${m.title}”在当前设备的音频和实时稿？请先确认备份已保存。此操作无法撤销。`))return;await deleteRecording(m.id);await recordings();});
    list.append(box);
  }
}
el('startRecord').onclick=start;el('stopRecord').onclick=()=>stop();el('pauseRecord').onclick=()=>{const s=active;if(s?.media.state==='recording')pause(s);else if(s?.media.state==='paused'&&!s.pending&&!s.failed){s.activeStart=performance.now();s.media.resume();status(s,'pauseRecord','暂停');status(s,'recordState','正在录音');if(s.enableLive)connectLive(s);}};
el('liveMode').onchange=()=>{el('liveSubtitle').hidden=el('liveMode').value!=='subtitle';el('liveDocument').hidden=el('liveMode').value!=='document';};
el('emergencyAudio').onclick=()=>{emergencies.forEach(s=>s.unsaved.forEach((blob,i)=>downloadBlob(blob,`${s.meta.id}-unsaved-${i}.bin`)));note(emergencies.some(s=>s.unsaved.length)?'请将未保存片段与已保存音频一起保留':'暂无未保存的内存片段');};
el('restoreRecording').onchange=async e=>{try{const files=[...e.target.files];const info=files.find(f=>f.name.endsWith('.json'));const audio=files.find(f=>!f.name.endsWith('.json'));if(!info||!audio)throw Error('请同时选择音频和 recording.json');if(info.size>5_000_000||audio.size>1_000_000_000)throw Error('备份超过大小限制');const source=JSON.parse(await info.text());const m={...source,id:crypto.randomUUID(),jid:null,backend:null,pendingUpload:false,uploaded:false,state:'restored',seq:1,bytes:audio.size,created:typeof source.created==='string'?source.created:new Date().toISOString(),title:String(source.title||'恢复的录音'),name:audio.name,mime:audio.type||source.mime||'audio/webm'};await saveAudio(m,audio);await recordings();note('已恢复为新记录，原记录保留');}catch(e){note('恢复未完成：'+e.message);}finally{e.target.value='';}};
window.addEventListener('online',()=>{if(active)connectLive(active);resumeQueue(uploadOptions).then(recordings);});
document.addEventListener('visibilitychange',()=>{if(active?.media?.state==='recording'){if(document.visibilityState==='visible')lock(active);else note('页面进入后台，系统可能中断录音，请立即回到前台；已保存片段保留');}});
window.addEventListener('beforeunload',e=>{if((active?.media&&active.media.state!=='inactive')||emergencies.some(s=>s.unsaved.length)){e.preventDefault();e.returnValue='';}});
setInterval(()=>{el('recordNetwork').textContent=navigator.onLine?'网络在线':'网络离线 · 录音仍保存在本机';if(active?.media&&active.media.state!=='inactive'){el('recordDuration').textContent=window.duration(seconds(active));if(seconds(active)>=10800){note('已达到三小时上限，正在保存');stop();}}},1000);
if(window.ltStaticHost&&!window.ltConnected()){el('enableLive').disabled=true;note('本地采集模式：可录音、保存和导出，识别需要连接私人后端。');}
window.refreshLocalRecordings=recordings;recordings().then(()=>resumeQueue(uploadOptions)).then(recordings).catch(()=>note('本地数据库不可用，请检查存储权限'));
