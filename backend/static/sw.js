const BASE=new URL('./',self.location.href);
const PREFIX='lecture-shell:'+BASE.pathname+':';
const CACHE=PREFIX+'reliability-20261109-v2';
const FILES=['','static/style.css','static/connection.js','static/app.js','static/workspace.js','static/recorder.js','static/recording-session.js','static/upload-queue.js','static/local-db.js','static/pcm-worklet.js','static/icon.svg','static/icon-180.png','static/icon-192.png','static/icon-512.png','static/manifest.webmanifest'].map(p=>new URL(p,BASE).href);
self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(FILES))));
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k.startsWith(PREFIX)&&k!==CACHE).map(k=>caches.delete(k))))));
self.addEventListener('fetch',e=>{
  const url=new URL(e.request.url);
  if(e.request.method!=='GET'||url.origin!==BASE.origin||url.pathname.startsWith('/api/'))return;
  if(e.request.mode==='navigate'){e.respondWith(fetch(e.request).catch(()=>caches.match(BASE.href)));return;}
  if(FILES.includes(url.href))e.respondWith(caches.match(e.request).then(cached=>cached||fetch(e.request)));
});
