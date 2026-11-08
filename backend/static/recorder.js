import {all,put,saveChunk,audioBlob,downloadBlob} from './local-db.js';
const el=id=>document.getElementById(id);
let media=null,stream=null,meta=null,queue=Promise.resolve(),pending=0,failed=false,unsaved=[],ctx=null,node=null;
let wake=null,ws=null,retry=null,liveReady=false,ended=false,activeStart=0,accumulated=0,liveLines=new Map();
const seconds=()=>accumulated+(media?.state==='recording'?(performance.now()-activeStart)/1000:0);
function note(message){el('recordNotice').textContent=message;}
function localStatus(text){el('recordSave').textContent=text;}
async function lock(){try{if(navigator.wakeLock&&document.visibilityState==='visible')wake=await navigator.wakeLock.request('screen');el('recordWake').textContent=wake?'屏幕常亮已请求':'请手动保持屏幕常亮';}catch{el('recordWake').textContent='常亮请求未获允许，请手动保持屏幕常亮';}}
function renderLive(){const values=[...liveLines.values()];el('liveDocument').textContent=values.map(x=>x.text).join('\n');el('liveSubtitle').textContent=values.at(-1)?.text||'等待识别文字…';}
async function prepare(m){const backend=window.ltBackend();if(m.jid&&m.backend===backend)return m.jid;const r=await window.api('/api/recordings/prepare',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(m)});m.jid=r.id;m.backend=backend;await put('recordings',m);return r.id;}
async function connectLive(){
  if(!meta||ended||!el('enableLive').checked||!navigator.onLine||ws?.readyState<=1)return;
  try{
    el('recordLive').textContent='正在连接实时服务…';const jid=await prepare(meta);
    const connection=new WebSocket(window.ltLiveUrl(`/api/live/${jid}`),window.ltLiveProtocols());ws=connection;
    const offset=Math.floor(seconds()*1000);
    connection.onopen=()=>connection.send(JSON.stringify({offset}));
    connection.onmessage=e=>{const value=JSON.parse(e.data);
      if(value.type==='ready'){liveReady=true;el('recordLive').textContent='实时识别已连接';}
      if(value.type==='sentence'){liveLines.set(value.session+':'+value.seq,value.segment);renderLive();meta.live=[...liveLines.entries()];put('recordings',{...meta}).catch(()=>note('实时文字本地保存失败，音频保存状态请见上方'));}
      if(value.type==='error'){liveReady=false;el('recordLive').textContent=value.message;}
    };
    connection.onclose=()=>{liveReady=false;if(ws===connection)ws=null;el('recordLive').textContent='实时连接已断开；录音继续，缺失文字由完整音频重识别补齐';if(!ended&&el('enableLive').checked)retry=setTimeout(connectLive,5000);};
    connection.onerror=()=>{liveReady=false;};
  }catch{el('recordLive').textContent='实时连接失败；本地录音继续';if(!ended)retry=setTimeout(connectLive,10000);}
}
function pause(){if(media?.state!=='recording')return;accumulated=seconds();media.requestData();media.pause();el('pauseRecord').textContent='继续';el('recordState').textContent='已暂停';}
async function cleanup(){
  ended=true;clearTimeout(retry);liveReady=false;
  if(ws?.readyState===1){ws.send(JSON.stringify({type:'finish'}));setTimeout(()=>ws?.close(),20000);}else ws?.close();
  node?.disconnect();stream?.getTracks().forEach(t=>t.stop());if(ctx)await ctx.close().catch(()=>{});ctx=null;
  await wake?.release().catch(()=>{});wake=null;
}
async function start(){
  if(media&&media.state!=='inactive')return;
  if(!window.isSecureContext||!navigator.mediaDevices?.getUserMedia||!window.MediaRecorder){note('当前浏览器不具备安全录音能力，请使用 HTTPS 和支持 MediaRecorder 的浏览器');return;}
  failed=false;unsaved=[];pending=0;ended=false;accumulated=0;queue=Promise.resolve();liveLines.clear();renderLive();
  try{
    await navigator.storage?.persist?.();const available=await navigator.storage?.estimate?.();
    if(available?.quota&&available.quota-available.usage<30_000_000)throw Error('可用本地空间不足 30 MB，请先导出重要录音并释放空间');
    stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1},video:false});
    const mime=['audio/mp4','audio/webm;codecs=opus','audio/webm','audio/ogg;codecs=opus'].find(t=>MediaRecorder.isTypeSupported(t));
    media=new MediaRecorder(stream,mime?{mimeType:mime,audioBitsPerSecond:96000}:{audioBitsPerSecond:96000});
    const ext=media.mimeType.includes('mp4')?'m4a':media.mimeType.includes('ogg')?'ogg':'webm';
    meta={id:crypto.randomUUID(),title:el('recordTitle').value||'现场录音',topic:el('recordTopic').value,
      name:'现场录音.'+ext,mime:media.mimeType,created:new Date().toISOString(),state:'recording',duration:0,seq:0,bytes:0,live:[]};
    await put('recordings',meta);
    media.ondataavailable=e=>{
      if(!e.data.size)return;const blob=e.data;const seq=meta.seq++;
      const snapshot={...meta,bytes:meta.bytes+blob.size,duration:seconds()};meta.bytes=snapshot.bytes;meta.duration=snapshot.duration;
      pending++;localStatus(`正在保存片段（${pending} 段待确认）`);
      queue=queue.then(async()=>{
        if(failed){unsaved.push(blob);return;}
        try{await saveChunk(snapshot,blob,seq);localStatus(`已保存 ${seq+1} 段 · ${(snapshot.bytes/1e6).toFixed(1)} MB`);}
        catch(error){failed=true;unsaved.push(blob);pause();note('本地写入失败，已暂停录音。请立即导出已保存音频与未保存片段，释放空间后开始新录音。');localStatus('保存失败：'+error.name);}
      }).finally(()=>pending--);
      if(pending>=5){pause();note('本地保存速度不足，已暂停，请等待保存完成后继续');}
    };
    media.onerror=()=>{note('录音器发生错误，已保存片段保留，可从下方恢复导出');stop();};
    stream.getAudioTracks()[0].onended=()=>{if(!ended){note('麦克风已中断，已保存音频保留');stop();}};
    media.onstop=async()=>{
      await queue;meta={...meta,state:failed?'storage-error':'stopped',duration:accumulated,live:[...liveLines.entries()]};
      // A quota failure must not claim that unsaved data is safe.
      try{await put('recordings',meta);}catch{note('结束状态未能保存；请立即导出音频');}
      await cleanup();el('enableLive').disabled=window.ltStaticHost&&!window.ltConnected();el('startRecord').disabled=false;el('pauseRecord').disabled=true;el('stopRecord').disabled=true;el('recordState').textContent=failed?'存储异常，已停止':'录音已结束';await recordings();
    };
    activeStart=performance.now();media.start(2000);await lock();
    el('enableLive').disabled=true;el('startRecord').disabled=true;el('pauseRecord').disabled=false;el('pauseRecord').textContent='暂停';el('stopRecord').disabled=false;
    el('recordState').textContent='正在录音';note('仅支持前台录音，请保持此页面打开和屏幕亮起。片段保存确认后才视为安全保存。');
    if(el('enableLive').checked){
      if(!window.AudioWorkletNode){note('当前设备不支持实时 PCM 采集，本地录音继续');return;}
      try{
        ctx=new AudioContext();await ctx.resume();await ctx.audioWorklet.addModule(window.ltAsset('static/pcm-worklet.js'));
        const source=ctx.createMediaStreamSource(stream);node=new AudioWorkletNode(ctx,'pcm16');const silent=ctx.createGain();silent.gain.value=0;source.connect(node);node.connect(silent);silent.connect(ctx.destination);
        node.port.onmessage=e=>{if(media?.state==='recording'&&liveReady&&ws?.readyState===1){if(ws.bufferedAmount<64000)ws.send(e.data);else{ws.close();note('实时网络拥塞，正在重连；原始录音继续保存');}}};
        connectLive();
      }catch{note('实时采集初始化失败，本地录音继续，可结束后识别完整音频');}
    }else el('recordLive').textContent='实时识别未开启，不发送实时音频';
  }catch(error){await cleanup();note(error.name==='NotAllowedError'?'麦克风权限被拒绝或失效，请在浏览器设置中允许麦克风':error.message);el('enableLive').disabled=window.ltStaticHost&&!window.ltConnected();el('startRecord').disabled=false;}
}
function stop(){if(!media||media.state==='inactive')return;accumulated=seconds();media.stop();ended=true;el('recordState').textContent='正在保存最后片段…';}
async function sendRecording(m){
  if(media?.state!=='inactive'&&m.id===meta?.id)throw Error('请先结束录音');
  if(!window.ltConnected())throw Error('请先在 API 设置中连接 HTTPS 后端；本机音频仍保留');
  if(m.backend!==window.ltBackend())m.jid=null;
  m.pendingUpload=true;m.backend=window.ltBackend();await put('recordings',m);
  if(!navigator.onLine){note('已加入待上传队列，网络恢复后处理；本地片段保留');return;}
  const jid=await prepare(m);const blob=await audioBlob(m);
  if(blob.size>1_000_000_000)throw Error('音频超过临时上传 1 GB 限制，请先导出');
  el('recordTransfer').textContent='正在提交完整音频到后端…';
  await window.api('/api/jobs/'+jid+'/recorded-audio',{method:'PUT',body:blob});
  await window.api('/api/jobs/'+jid+'/start',{method:'POST'});
  m.pendingUpload=false;m.uploaded=true;await put('recordings',m);
  el('recordTransfer').textContent='完整音频已提交；可回工作台查看识别进度';await recordings();window.refresh();
}
async function recordings(){
  const list=el('localRecordings');list.replaceChildren();
  for(const m of (await all('recordings')).sort((a,b)=>b.created.localeCompare(a.created))){
    const box=document.createElement('div');box.className='local-recording';const text=document.createElement('p');
    const active=m.id===meta?.id&&media?.state!=='inactive';
    text.textContent=`${m.title} · ${Math.floor(m.duration||0)} 秒 · ${(m.bytes/1e6).toFixed(1)} MB · ${active?'当前录音':m.state==='recording'?'异常退出后恢复的片段，末尾可能不完整':m.state}${m.pendingUpload?' · 待上传':''}`;
    const save=document.createElement('button');save.textContent='导出已保存音频';save.onclick=async()=>downloadBlob(await audioBlob(m),m.name);
    const details=document.createElement('button');details.textContent='导出实时稿与录音信息';details.onclick=()=>downloadBlob(new Blob([JSON.stringify(m,null,2)],{type:'application/json'}),'recording.json');
    const send=document.createElement('button');send.textContent=m.uploaded?'已提交（可再次查询）':'上传完整音频并转录';send.disabled=active;send.onclick=async()=>{send.disabled=true;try{await sendRecording(m);}catch(e){note('上传未完成：'+e.message+'；本地片段保留');}finally{send.disabled=active;}};
    box.append(text,save,details,send);list.append(box);
  }
}
el('startRecord').onclick=start;el('stopRecord').onclick=stop;
el('pauseRecord').onclick=()=>{if(media?.state==='recording')pause();else if(media?.state==='paused'&&!pending&&!failed){activeStart=performance.now();media.resume();el('pauseRecord').textContent='暂停';el('recordState').textContent='正在录音';}};
el('liveMode').onchange=()=>{el('liveSubtitle').hidden=el('liveMode').value!=='subtitle';el('liveDocument').hidden=el('liveMode').value!=='document';};
el('emergencyAudio').onclick=()=>{unsaved.forEach((blob,i)=>downloadBlob(blob,`unsaved-fragment-${i}.bin`));note(unsaved.length?'未保存片段已导出，需与已保存音频一起保留':'暂无未保存的内存片段');};
el('restoreRecording').onchange=async e=>{
  try{
    const files=[...e.target.files];const info=files.find(f=>f.name.endsWith('.json'));const audio=files.find(f=>!f.name.endsWith('.json'));
    if(!info||!audio)throw Error('请同时选择音频文件和导出的 recording.json');
    if(info.size>5_000_000||audio.size>1_000_000_000)throw Error('备份文件超过恢复大小限制');
    const source=JSON.parse(await info.text());
    const restored={...source,id:crypto.randomUUID(),jid:null,pendingUpload:false,uploaded:false,state:'restored',seq:1,bytes:audio.size,
      created:typeof source.created==='string'?source.created:new Date().toISOString(),title:String(source.title||'恢复的录音'),name:audio.name,mime:audio.type||source.mime||'audio/webm'};
    await saveChunk(restored,audio,0);await recordings();note('录音及元数据已恢复到本机，新建记录不会覆盖现有录音');
  }catch(error){note('恢复未完成：'+error.message);}finally{e.target.value='';}
};
window.addEventListener('online',async()=>{connectLive();for(const m of await all('recordings'))if(m.pendingUpload&&m.backend===window.ltBackend())try{await sendRecording(m);}catch{note('待上传任务暂未完成，本地音频保留，可重试');}});
document.addEventListener('visibilitychange',()=>{if(media?.state==='recording'){if(document.visibilityState==='visible')lock();else note('页面进入后台：iOS 可能中断录音，请立即回到前台；已保存片段保留');}});
window.addEventListener('beforeunload',e=>{if(media&&media.state!=='inactive'){e.preventDefault();e.returnValue='';}});
setInterval(()=>{el('recordNetwork').textContent=navigator.onLine?'网络在线':'网络离线 · 录音仍保存在本机';if(media&&media.state!=='inactive'){el('recordDuration').textContent=window.duration(seconds());if(seconds()>=10800){note('已达到三小时录音上限，正在保存并结束');stop();}}},1000);
if(window.ltStaticHost&&!window.ltConnected()){el('enableLive').disabled=true;note('当前为本地采集模式。可录音、保存和导出；实时与完整识别需先在 API 设置中连接 HTTPS 后端。');}
window.refreshLocalRecordings=recordings;
recordings().catch(()=>note('本地数据库不可用，录音前请检查浏览器存储权限'));
