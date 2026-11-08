const opened=new Promise((resolve,reject)=>{
  const r=indexedDB.open('lecture-transcriber',1);
  r.onupgradeneeded=()=>{
    r.result.createObjectStore('recordings',{keyPath:'id'});
    const chunks=r.result.createObjectStore('chunks',{keyPath:['id','seq']});chunks.createIndex('recording','id');
    r.result.createObjectStore('projects',{keyPath:'id'});
  };
  r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);
});
export async function all(store){const db=await opened;return new Promise((ok,no)=>{const r=db.transaction(store).objectStore(store).getAll();r.onsuccess=()=>ok(r.result);r.onerror=()=>no(r.error);});}
export async function put(store,value){const db=await opened;return new Promise((ok,no)=>{const tx=db.transaction(store,'readwrite');tx.objectStore(store).put(value);tx.oncomplete=()=>ok();tx.onerror=()=>no(tx.error);tx.onabort=()=>no(tx.error||Error('本地保存被中断'));});}
export async function saveChunk(meta,blob,seq){const db=await opened;return new Promise((ok,no)=>{const tx=db.transaction(['recordings','chunks'],'readwrite');tx.objectStore('chunks').put({id:meta.id,seq,blob});tx.objectStore('recordings').put(meta);tx.oncomplete=()=>ok();tx.onerror=()=>no(tx.error);tx.onabort=()=>no(tx.error||Error('音频未能保存'));});}
export async function audioBlob(meta){const db=await opened;const parts=await new Promise((ok,no)=>{const r=db.transaction('chunks').objectStore('chunks').index('recording').getAll(meta.id);r.onsuccess=()=>ok(r.result);r.onerror=()=>no(r.error);});parts.sort((a,b)=>a.seq-b.seq);return new Blob(parts.map(p=>p.blob),{type:meta.mime});}
export function downloadBlob(blob,name){const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),30000);}
export {opened};
