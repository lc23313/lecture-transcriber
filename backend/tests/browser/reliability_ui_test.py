import asyncio,json,sys,tempfile,threading,time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import app as R
from services import transcripts as T,language,streaming
import uvicorn
from playwright.sync_api import sync_playwright,expect
root=Path(__file__).resolve().parents[2]/'work'
class FakeStream:
    def __init__(self,*args,**kwargs):self.q=asyncio.Queue();self.sent=False
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    async def send(self,value):
        if isinstance(value,bytes):
            if not self.sent:
                self.sent=True;await self.q.put(json.dumps({'header':{'event':'result-generated'},'payload':{'output':{'sentence':{'sentence_id':1,'begin_time':0,'end_time':1000,'text':'session word','sentence_end':True}}}}))
        elif json.loads(value)['header']['action']=='run-task':await self.q.put(json.dumps({'header':{'event':'task-started'}}))
        else:await self.q.put(json.dumps({'header':{'event':'task-finished'}}))
    async def recv(self):return await self.q.get()
    def __aiter__(self):return self
    async def __anext__(self):return await self.recv()
checks=[]
with tempfile.TemporaryDirectory(dir=root) as tmp,patch.object(R,'DATA',Path(tmp)),patch.object(R,'config',return_value={'workspace_id':'test','region':'cn-beijing','api_key':'test'}),patch.object(R.POOL,'submit'),patch.object(streaming,'connect',FakeStream):
    R.init_db()
    for jid,title in [('qa-a','Review A'),('qa-b','Review B')]:
        (R.DATA/(jid+'.audio')).write_bytes(b'qa')
        with R.db() as c:
            c.execute('INSERT INTO jobs(id,name,title,status,created,original) VALUES(?,?,?,?,?,?)',(jid,title+'.wav',title,'SUCCEEDED',time.time(),'original words'))
            T.add(c,jid,'filetrans','original words',[{'start':0,'end':1000,'speaker':None,'text':'original words'}])
    server=uvicorn.Server(uvicorn.Config(R.app,host='127.0.0.1',port=8765,log_level='error'));thread=threading.Thread(target=server.run,daemon=True);thread.start()
    while not server.started:time.sleep(.1)
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',headless=True,args=['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream'])
            ctx=browser.new_context(permissions=['microphone']);page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept())
            page.goto('http://127.0.0.1:8765');page.locator('#list .item').filter(has_text='Review A').click();expect(page.locator('#paragraphs')).to_contain_text('original words')
            page.locator('#editVersion').click();page.locator('#paragraphs textarea').fill('recover this unsaved edit');page.wait_for_timeout(800);page.reload();expect(page.locator('#restoreDraft')).to_be_visible();page.locator('#restoreDraft').click();expect(page.locator('#paragraphs textarea')).to_have_value('recover this unsaved edit');checks.append('未保存稿件重载后可恢复')
            page.locator('#save').click();expect(page.locator('#revision')).to_contain_text('用户编辑原稿')
            page.locator('#list .item').filter(has_text='Review B').click();expect(page.locator('#paragraphs')).to_contain_text('original words');page.wait_for_timeout(600)
            ctx.set_offline(True);page.evaluate('refresh()');page.locator('#list .item').filter(has_text='Review A').click();expect(page.locator('#paragraphs')).to_contain_text('recover this unsaved edit');checks.append('在线列表断网后能切换本地项目副本')
            ctx.set_offline(False);page.locator('[data-page=record]').click();page.locator('#enableLive').check();page.locator('#recordTitle').fill('Session A');page.locator('#startRecord').click();expect(page.locator('#recordSave')).to_contain_text('已保存',timeout=12000);expect(page.locator('#liveSubtitle')).to_contain_text('session word');page.locator('#stopRecord').click();expect(page.locator('#recordState')).to_have_text('录音已结束')
            page.locator('#recordTitle').fill('Session B');page.locator('#startRecord').click();expect(page.locator('#recordState')).to_have_text('正在录音');page.wait_for_timeout(21000);expect(page.locator('#recordLive')).to_contain_text('已连接');expect(page.locator('#recordSave')).to_contain_text('已保存');page.locator('#stopRecord').click();expect(page.locator('#recordState')).to_have_text('录音已结束');checks.append('连续两场录音超过旧清理定时器仍保持新连接')
            page.evaluate("""async()=>{const db=await import('/static/local-db.js');const blob=new Blob([new Uint8Array(5*1024*1024)],{type:'audio/webm'});const m={id:'upload-qa',title:'Resume Test',name:'resume.webm',created:new Date().toISOString(),mime:'audio/webm',state:'stopped',duration:30,seq:1,bytes:blob.size,live:[]};await db.saveChunk(m,blob,0);await window.refreshLocalRecordings();window.failChunk=true;const original=window.api;window.api=async(path,...args)=>{if(window.failChunk&&path.endsWith('/recorded-upload/1'))throw Error('injected network failure');return original(path,...args);};}""")
            box=page.locator('.local-recording').filter(has_text='Resume Test');box.get_by_role('button',name='上传完整音频并转录').click();expect(page.locator('#recordNotice')).to_contain_text('injected network failure');
            with R.db() as c:jid=c.execute("SELECT id FROM jobs WHERE recording_id='upload-qa'").fetchone()[0];assert c.execute('SELECT count(*) FROM upload_parts WHERE job=?',(jid,)).fetchone()[0]==1
            page.reload();page.locator('[data-page=record]').click();expect(page.locator('#localRecordings').filter(has_text='Resume Test')).to_contain_text('已提交',timeout=15000)
            assert (R.DATA/(jid+'.audio')).stat().st_size==5*1024*1024;checks.append('4MB分片上传中断后重载自动恢复并完成校验')
            integrity=page.evaluate("""async()=>{const db=await import('/static/local-db.js');for(const m of await db.all('recordings')){await db.audioBlob(m);}return true;}""");assert integrity;checks.append('真实浏览器录音片段序号大小与哈希一致')
            page.screenshot(path=str(root/'reliability-qa.png'),full_page=True);assert not errors,errors;browser.close()
        (root/'reliability-ui.json').write_text(json.dumps({'passed':len(checks),'checks':checks,'errors':errors},ensure_ascii=False,indent=2),'utf-8');print('Reliability browser checks:',len(checks))
    finally:server.should_exit=True;thread.join(10)
