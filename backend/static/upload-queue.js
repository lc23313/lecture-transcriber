import {all,put,audioBlob,digest} from './local-db.js';
const running=new Map();
export function uploadRecording(m,{notice,progress,prepare}){
  const key=window.ltBackend()+':'+m.id;if(running.has(key))return running.get(key);
  const work=(async()=>{
    const backend=window.ltBackend();if(!backend)throw Error('请先连接私人后端');
    m.pendingUpload=true;if(m.backend!==backend)m.jid=null;m.backend=backend;await put('recordings',m);
    if(!navigator.onLine){notice('已加入待上传队列，网络恢复后继续');return;}
    const jid=await prepare(m);const state=await window.api(`/api/jobs/${jid}/recorded-upload`);
    if(!state.completed){
      const blob=await audioBlob(m);if(blob.size>1_000_000_000)throw Error('音频超过临时上传1GB限制');
      const size=state.part_size;let sent=0;const count=Math.ceil(blob.size/size);
      for(let i=0;i<count;i++){
        if(window.ltBackend()!==backend)throw Error('后端已切换，上传暂停');
        const part=blob.slice(i*size,(i+1)*size);const hash=await digest(part);const old=state.parts.find(p=>p.seq===i);
        if(!old||old.hash!==hash||old.size!==part.size)await window.api(`/api/jobs/${jid}/recorded-upload/${i}`,{method:'PUT',headers:{'X-Chunk-SHA256':hash},body:part});
        sent+=part.size;progress(sent,blob.size,false);
      }
      await window.api(`/api/jobs/${jid}/recorded-upload/complete`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({count,bytes:blob.size})});progress(blob.size,blob.size,true);
    }
    if(window.ltBackend()!==backend)throw Error('后端已切换，音频已保存但未发起识别');
    await window.api(`/api/jobs/${jid}/start`,{method:'POST'});
    m.pendingUpload=false;m.uploaded=true;await put('recordings',m);notice('完整音频已提交；可回工作台查看识别进度');window.refresh?.();
  })().finally(()=>running.delete(key));running.set(key,work);return work;
}
export async function resumeQueue(options){if(!window.ltConnected()||!navigator.onLine)return;for(const m of await all('recordings'))if(m.pendingUpload&&m.backend===window.ltBackend())try{await uploadRecording(m,options);}catch(e){options.notice('待上传任务未完成：'+e.message+'；本地音频保留');}}
