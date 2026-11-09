import json,sys,threading,time
from pathlib import Path
from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
from playwright.sync_api import sync_playwright,expect
root=Path(__file__).resolve().parents[2]/'work';repo=root/'github-publish';calls=[]
class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(repo),**kwargs)
    def log_message(self,*args):pass
    def do_GET(self):
        calls.append(self.path)
        if not self.path.startswith('/lecture-transcriber/'):
            self.send_error(404);return
        self.path='/site/'+self.path[len('/lecture-transcriber/'):]
        super().do_GET()
server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
checks=[]
try:
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',headless=True,args=['--use-fake-device-for-media-stream','--use-fake-ui-for-media-stream'])
        ctx=browser.new_context(permissions=['microphone'],viewport={'width':390,'height':844});page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(f'http://127.0.0.1:{server.server_port}/lecture-transcriber/')
        expect(page.locator('#config')).to_contain_text('本地采集');checks.append('项目子路径资源加载正确，无后端时明确显示本地模式')
        page.locator('[data-page=settings]').click();expect(page.locator('#backendConnection')).to_be_visible();checks.append('显示独立HTTPS后端连接设置')
        page.locator('[data-page=workspace]').click();page.locator('#file').set_input_files({'name':'local-upload.wav','mimeType':'audio/wav','buffer':b'RIFFtest'})
        expect(page.locator('#localRecordings')).to_contain_text('local-upload.wav');checks.append('未连接后端也可将导入文件持久化到本机')
        expect(page.locator('#enableLive')).to_be_disabled();page.locator('#startRecord').click();expect(page.locator('#recordSave')).to_contain_text('已保存',timeout=12000);page.locator('#stopRecord').click();expect(page.locator('#recordState')).to_have_text('录音已结束');checks.append('静态站点本地录音可用，不伪造实时识别')
        page.evaluate('navigator.serviceWorker.ready.then(()=>true)');page.reload();page.wait_for_function('navigator.serviceWorker.controller !== null');ctx.set_offline(True);page.reload();page.locator('[data-page=record]').click();expect(page.locator('#localRecordings')).to_contain_text('现场录音');checks.append('Pages子路径Service Worker离线重载与录音恢复')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth');checks.append('手机宽度无横向溢出')
        assert not any('/api/' in x for x in calls),calls
        assert not errors,errors;checks.append('未连接后端时不向GitHub Pages发送API或音频请求')
        page.screenshot(path=str(root/'pages-local-mode-qa.png'),full_page=True)
        browser.close()
    (root/'pages-ui-acceptance.json').write_text(json.dumps({'passed':len(checks),'checks':checks,'errors':errors},ensure_ascii=False,indent=2),'utf-8')
    print('Pages UI checks passed:',len(checks))
finally:server.shutdown();server.server_close()
