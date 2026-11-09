import asyncio
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import app as R
from services import language,streaming,transcripts as T
import uvicorn
from playwright.sync_api import sync_playwright,expect

root=Path(__file__).resolve().parents[2]/'work'
class FakeStream:
    def __init__(self,*args,**kwargs):self.queue=asyncio.Queue();self.sent=False
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    async def send(self,value):
        if isinstance(value,bytes):
            if not self.sent:
                self.sent=True;await self.queue.put(json.dumps({'header':{'event':'result-generated'},'payload':{'output':{'sentence':{'sentence_id':1,'begin_time':0,'end_time':1000,'text':'呃，stochastic process。','sentence_end':True}}}}))
        elif json.loads(value)['header']['action']=='run-task':await self.queue.put(json.dumps({'header':{'event':'task-started'}}))
        else:await self.queue.put(json.dumps({'header':{'event':'task-finished'}}))
    async def recv(self):return await self.queue.get()
    def __aiter__(self):return self
    async def __anext__(self):return await self.recv()

checks=[]
with tempfile.TemporaryDirectory(dir=root) as temp,patch.object(R,'DATA',Path(temp)),patch.object(R,'config',return_value={'api_key':'test','workspace_id':'test','region':'cn-beijing','llm_model':'test'}),patch.object(language,'complete',return_value={'entries':[{'en':'Ito','zh':'伊藤','weight':2}],'context':'随机微积分'}),patch.object(language,'tidy',return_value='整理后的 stochastic process。'),patch.object(streaming,'connect',FakeStream):
    R.init_db()
    jid='ui-test'
    (R.DATA/(jid+'.audio')).write_bytes(b'RIFFtest')
    with R.db() as c:
        c.execute('INSERT INTO jobs(id,name,status,created,original,speakers) VALUES(?,?,?,?,?,?)',(jid,'UI验收.wav','SUCCEEDED',time.time(),'呃，stochastic process。',json.dumps({'channel-0:speaker-0':'Speaker 1'})))
        T.add(c,jid,'filetrans','呃，stochastic process。',[{'start':0,'end':1000,'speaker':'channel-0:speaker-0','text':'呃，stochastic process。'}])
    server=uvicorn.Server(uvicorn.Config(R.app,host='127.0.0.1',port=8765,log_level='error'))
    thread=threading.Thread(target=server.run,daemon=True);thread.start()
    while not server.started:time.sleep(.1)
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',headless=True,args=['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream'])
            icons=browser.new_page()
            svg=(R.ROOT/'static/icon.svg').read_text('utf-8')
            for n in (180,192,512):
                icons.set_viewport_size({'width':n,'height':n});icons.set_content('<style>html,body{margin:0}svg{width:100vw;height:100vh;display:block}</style>'+svg);icons.screenshot(path=str(R.ROOT/f'static/icon-{n}.png'))
            icons.close()
            context=browser.new_context(permissions=['microphone'],viewport={'width':1440,'height':1100},service_workers='allow')
            page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept())
            page.goto('http://127.0.0.1:8765')
            expect(page.locator('#revision')).to_contain_text('完整音频原稿');expect(page.locator('#paragraphs')).to_contain_text('stochastic process')
            checks.append('工作台原稿与时间戳段落加载')
            page.locator('#editVersion').click();page.locator('#paragraphs textarea').fill('用户编辑的原话');page.locator('#save').click()
            expect(page.locator('#revision')).to_contain_text('用户编辑原稿');checks.append('编辑生成新版本，来源原稿仍保留')
            page.locator('#lightTidy').click();page.wait_for_timeout(5500)
            expect(page.locator('#revision')).to_contain_text('轻度整理');checks.append('手动整理生成独立版本')
            page.locator('#revision').select_option(label=page.locator('#revision option').filter(has_text='轻度整理').first.text_content())
            page.locator('#showDiff').click();expect(page.locator('#diff')).to_be_visible();checks.append('整理前后差异比较')
            page.locator('summary').filter(has_text='专业词汇表').click();page.locator('#addVocab').click();page.locator('#vocabEntries input').nth(0).fill('Brownian motion');page.locator('#vocabEntries input').nth(1).fill('布朗运动');page.locator('#saveVocab').click();expect(page.locator('#vocabStatus')).to_contain_text('已保存');checks.append('词条可添加与保存')
            page.locator('[data-page=record]').click();page.locator('#enableLive').check();page.locator('#startRecord').click()
            expect(page.locator('#recordState')).to_have_text('正在录音');expect(page.locator('#recordSave')).to_contain_text('已保存',timeout=12000);expect(page.locator('#liveSubtitle')).to_contain_text('stochastic process',timeout=12000)
            checks.append('真实浏览器录音分段落盘，实时代理使用模拟云连接')
            page.locator('#pauseRecord').click();expect(page.locator('#recordState')).to_have_text('已暂停');page.locator('#pauseRecord').click();expect(page.locator('#recordState')).to_have_text('正在录音');checks.append('暂停与继续录音')
            page.locator('#liveMode').select_option('document');expect(page.locator('#liveDocument')).to_be_visible();checks.append('字幕与文档模式切换')
            context.set_offline(True);before=page.locator('#recordSave').text_content();page.wait_for_timeout(3000);after=page.locator('#recordSave').text_content();assert before!=after,(before,after)
            expect(page.locator('#recordNetwork')).to_contain_text('离线');checks.append('断网后本地录音继续持久化')
            page.locator('#stopRecord').click();expect(page.locator('#recordState')).to_have_text('录音已结束');expect(page.locator('#localRecordings')).to_contain_text('导出已保存音频');checks.append('停止后保存最后片段并提供音频导出')
            context.set_offline(False);page.evaluate('navigator.serviceWorker.ready.then(()=>true)');page.reload();page.wait_for_function('navigator.serviceWorker.controller !== null');context.set_offline(True);page.reload();page.locator('[data-page=record]').click();expect(page.locator('#localRecordings')).to_contain_text('现场录音');checks.append('离线重载后恢复已持久化的录音')
            page.locator('[data-page=workspace]').click();page.locator('#list .item').filter(has_text='UI验收').click();expect(page.locator('#revision')).to_contain_text('整理');checks.append('离线可查看已缓存的项目与稿件版本');page.locator('[data-page=record]').click()
            page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');checks.append('移动端无横向溢出')
            page.screenshot(path=str(root/'full-record-qa.png'),full_page=True)
            context.set_offline(False);page.evaluate('''()=>{window.realTransaction=IDBDatabase.prototype.transaction;IDBDatabase.prototype.transaction=function(names,mode,...args){if(mode==='readwrite'&&Array.from(typeof names==='string'?[names]:names).includes('chunks'))throw new DOMException('test quota','QuotaExceededError');return window.realTransaction.call(this,names,mode,...args);};}''')
            page.locator('#startRecord').click();expect(page.locator('#recordNotice')).to_contain_text('本地写入失败',timeout=10000);expect(page.locator('#recordState')).to_have_text('已暂停');page.locator('#stopRecord').click();expect(page.locator('#recordState')).to_contain_text('存储异常');checks.append('存储配额错误时暂停并明确警告，不宣称安全保存')
            page.evaluate('()=>{IDBDatabase.prototype.transaction=window.realTransaction;}');page.locator('#restoreRecording').set_input_files([{'name':'recording.json','mimeType':'application/json','buffer':json.dumps({'title':'恢复验收','duration':2}).encode()},{'name':'restored.webm','mimeType':'audio/webm','buffer':b'backup-audio'}]);expect(page.locator('#localRecordings')).to_contain_text('恢复验收');checks.append('录音音频与元数据可离线恢复为独立记录')
            page.evaluate('()=>{IDBDatabase.prototype.transaction=window.realTransaction;}');page.set_viewport_size({'width':1440,'height':1100});page.locator('[data-page=workspace]').click();page.locator('#list .item').filter(has_text='UI验收').click();expect(page.locator('#revision')).to_contain_text('整理');page.wait_for_timeout(1000);page.screenshot(path=str(root/'full-workspace-qa.png'),full_page=True)
            assert not errors,errors
            browser.close()
        (root/'ui-acceptance.json').write_text(json.dumps({'checks':checks,'passed':len(checks),'errors':errors},ensure_ascii=False,indent=2),'utf-8')
        print('UI checks passed:',len(checks))
    finally:
        server.should_exit=True;thread.join(10)
