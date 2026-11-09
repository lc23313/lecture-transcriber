const opened=new Promise((resolve,reject)=>{
  const r=indexedDB.open('lecture-transcriber',2);
  r.onupgradeneeded=()=>{
    const db=r.result;
    if(!db.objectStoreNames.contains('recordings'))db.createObjectStore('recordings',{keyPath:'id'});
    if(!db.objectStoreNames.contains('chunks')){const chunks=db.createObjectStore('chunks',{keyPath:['id','seq']});chunks.createIndex('recording','id');}
    if(!db.objectStoreNames.contains('projects'))db.createObjectStore('projects',{keyPath:'id'});
    if(!db.objectStoreNames.contains('drafts'))db.createObjectStore('drafts',{keyPath:'id'});
  };
  r.onsuccess=()=>{r.result.onversionchange=()=>r.result.close();resolve(r.result);};r.onerror=()=>reject(r.error);
  r.onblocked=()=>reject(Error('请关闭其他旧版页面后重试本地存储升级'));
});
function complete(tx){return new Promise((ok,no)=>{tx.oncomplete=()=>ok();tx.onerror=()=>no(tx.error);tx.onabort=()=>no(tx.error||Error('本地保存被中断'));});}
export async function all(store){const db=await opened;return new Promise((ok,no)=>{const r=db.transaction(store).objectStore(store).getAll();r.onsuccess=()=>ok(r.result);r.onerror=()=>no(r.error);});}
export async function get(store,id){const db=await opened;return new Promise((ok,no)=>{const r=db.transaction(store).objectStore(store).get(id);r.onsuccess=()=>ok(r.result);r.onerror=()=>no(r.error);});}
export async function put(store,value){const db=await opened;const tx=db.transaction(store,'readwrite');tx.objectStore(store).put(value);await complete(tx);}
export async function remove(store,id){const db=await opened;const tx=db.transaction(store,'readwrite');tx.objectStore(store).delete(id);await complete(tx);}
export async function digest(blob){return [...new Uint8Array(await crypto.subtle.digest('SHA-256',await blob.arrayBuffer()))].map(v=>v.toString(16).padStart(2,'0')).join('');}
export async function saveChunk(meta,blob,seq){const hash=await digest(blob);const db=await opened;const tx=db.transaction(['recordings','chunks'],'readwrite');
  tx.objectStore('chunks').put({id:meta.id,seq,blob,hash});tx.objectStore('recordings').put(meta);await complete(tx);return hash;}
export async function audioBlob(meta){const db=await opened;const parts=await new Promise((ok,no)=>{const r=db.transaction('chunks').objectStore('chunks').index('recording').getAll(meta.id);r.onsuccess=()=>ok(r.result);r.onerror=()=>no(r.error);});
  parts.sort((a,b)=>a.seq-b.seq);
  if(!parts.length||parts.some((p,i)=>p.seq!==i)||parts.length!==meta.seq)throw Error('录音片段不连续或数量不符，请保留原数据并检查恢复记录');
  let size=0;for(const p of parts){size+=p.blob.size;if(p.hash&&await digest(p.blob)!==p.hash)throw Error('录音片段校验失败，请保留原数据');}
  if(size!==meta.bytes)throw Error('音频大小与保存记录不符，请保留原数据');
  return new Blob(parts.map(p=>p.blob),{type:meta.mime});}
export async function deleteRecording(id){const db=await opened;const tx=db.transaction(['recordings','chunks'],'readwrite');
  tx.objectStore('recordings').delete(id);const cursor=tx.objectStore('chunks').index('recording').openKeyCursor(IDBKeyRange.only(id));cursor.onsuccess=()=>{const c=cursor.result;if(c){tx.objectStore('chunks').delete(c.primaryKey);c.continue();}};await complete(tx);}
export function downloadBlob(blob,name){const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);}
export {opened};

export async function merge(store,id,fields,combine){const db=await opened;const tx=db.transaction(store,'readwrite');const object=tx.objectStore(store);const r=object.get(id);r.onsuccess=()=>object.put(combine?combine(r.result||{},fields):{...r.result,...fields});await complete(tx);}

export async function saveAudio(meta,blob,progress=()=>{}){let bytes=0;const size=4*1024*1024;for(let offset=0,seq=0;offset<blob.size;offset+=size,seq++){const part=blob.slice(offset,offset+size);bytes+=part.size;await saveChunk({...meta,seq:seq+1,bytes,state:bytes===blob.size?meta.state:'importing'},part,seq);progress(bytes,blob.size);}return meta;}
