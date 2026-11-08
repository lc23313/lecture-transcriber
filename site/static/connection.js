/* Static hosting adapter. Stores a backend address and a session-only private access token, never ASR keys. */
(() => {
  const base=new URL('../',document.currentScript.src);
  const staticHost=document.documentElement.dataset.host==='pages';
  const addressKey='lecture-backend:'+base.pathname;
  const tokenKey='lecture-access:'+base.pathname;
  window.ltStaticHost=staticHost;
  window.ltAsset=path=>new URL(path.replace(/^\//,''),base).href;
  window.ltBackend=()=>staticHost?(localStorage.getItem(addressKey)||''):location.origin;
  window.ltConnected=()=>!!window.ltBackend();
  window.ltApiUrl=path=>{
    const backend=window.ltBackend();
    if(!backend)throw Error('尚未连接 HTTPS 后端；可以先本地录音、保存和导出');
    return new URL(path,backend+'/').href;
  };
  window.ltHeaders=()=>{
    const token=staticHost?sessionStorage.getItem(tokenKey):null;
    return token?{Authorization:'Bearer '+token}:{};
  };
  window.ltRememberToken=token=>{if(staticHost)sessionStorage.setItem(tokenKey,token);};
  window.ltSaveBackend=value=>{
    if(!value.trim()){localStorage.removeItem(addressKey);sessionStorage.removeItem(tokenKey);return;}
    const u=new URL(value);
    if(u.protocol!=='https:'||u.username||u.password||u.search||u.hash||!['','/'].includes(u.pathname))throw Error('请输入 HTTPS 后端根地址，不要填写密钥或 /api 路径');
    if(u.origin!==window.ltBackend())sessionStorage.removeItem(tokenKey);
    localStorage.setItem(addressKey,u.origin);
  };
  window.ltLiveUrl=path=>window.ltApiUrl(path).replace(/^http/,'ws');
  window.ltLiveProtocols=()=>{
    const token=staticHost?sessionStorage.getItem(tokenKey):null;
    return token?['lecture-v1','lecture-auth.'+btoa(unescape(encodeURIComponent(token))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'')]:[];
  };
  window.ltDownload=async path=>{
    if(!staticHost){location.href=path;return;}
    const r=await window.api('/api/download-ticket',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})});
    location.href=window.ltApiUrl(r.path);
  };
})();
