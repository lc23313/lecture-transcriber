"""Desktop transcription: local storage and opt-in Model Studio temporary upload."""
import json
import re
import sqlite3
import threading
import time
import uuid
import os
import sys
import secrets
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from services import transcripts as T,language
from services import bridge,audio
from fastapi.middleware.cors import CORSMiddleware

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.environ.get('LT_CONFIG_FILE', str(ROOT/'config.json')))
DATA = ROOT / 'data'
MODEL = 'qwen-audio-3.1-asr-flash-filetrans'
MAX_AUDIO_BYTES = 1_000_000_000
FORMATS = {'aac','amr','avi','flac','flv','m4a','mkv','mov','mp3','mp4','mpeg','ogg','opus','wav','webm','wma','wmv'}
POOL = ThreadPoolExecutor(max_workers=2,thread_name_prefix='asr')
AI_POOL = ThreadPoolExecutor(max_workers=2,thread_name_prefix='llm')
CANCEL = set()
MAX_WAIT_SECONDS = 6*3600
LOCK = threading.Lock()
ACTIVE = set()
BUSY = {'WAITING_UPLOAD','UPLOADING','SUBMITTING','PENDING','RUNNING','FETCHING_RESULT'}

@contextmanager
def db():
    c = sqlite3.connect(DATA / 'lectures.db', timeout=30)
    c.row_factory = sqlite3.Row
    try:
        with c:
            yield c
    finally:
        c.close()

def init_db():
    DATA.mkdir(exist_ok=True)
    with db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, name TEXT, status TEXT, created REAL, task TEXT, object_key TEXT, error TEXT, original TEXT, edited TEXT, raw TEXT, cleanup TEXT)')
        columns = {r[1] for r in c.execute('PRAGMA table_info(jobs)')}
        additions = {'storage_mode':"TEXT DEFAULT 'local'", 'upload_sent':'INTEGER DEFAULT 0',
                     'upload_total':'INTEGER DEFAULT 0', 'upload_completed':'INTEGER DEFAULT 0',
                     'upload_started':'REAL', 'recognition_started':'REAL', 'finished':'REAL', 'last_polled':'REAL'}
        for name, definition in additions.items():
            if name not in columns:
                c.execute(f'ALTER TABLE jobs ADD COLUMN {name} {definition}')
        T.migrate(c)

init_db()

def get_job(jid):
    with db() as c:
        row = c.execute('SELECT * FROM jobs WHERE id=?', (jid,)).fetchone()
    if not row:
        raise HTTPException(404, '找不到文件')
    return dict(row)

def update(jid, **values):
    with db() as c:
        c.execute('UPDATE jobs SET '+','.join(k+'=?' for k in values)+' WHERE id=?', (*values.values(), jid))

def config():
    try:
        cfg = json.loads(CONFIG_FILE.read_text('utf-8-sig'))
    except (OSError, ValueError):
        raise ValueError('请先填写 config.json 中的百炼配置；本地导入和查看稿件不需要配置')
    for key in ('api_key','workspace_id','region'):
        if not cfg.get(key):
            raise ValueError('配置缺少：'+key)
    if cfg['region'] not in ('cn-beijing','ap-southeast-1') or not re.fullmatch(r'[A-Za-z0-9-]+',cfg['workspace_id']):
        raise ValueError('地域或 Workspace ID 格式错误')
    return cfg

def ensure_vocabulary(jid,force=False):
    job=get_job(jid)
    if not job.get('topic'):return
    if not force and (json.loads(job.get('vocab') or '{}').get('entries') or (job.get('vocab_status') or '').startswith('已保存')):return
    update(jid,vocab_status='正在生成专业词汇')
    aid=uuid.uuid4().hex
    with db() as c:c.execute('INSERT INTO actions VALUES(?,?,?,?,?,?,?)',(aid,jid,'vocabulary:auto','RUNNING',None,time.time(),None))
    try:
        result=language.complete(config(),language.VOCAB_SYSTEM,json.dumps({'topic':job['topic']},ensure_ascii=False))
        current=get_job(jid)
        if current.get('topic')==job.get('topic') and current.get('vocab')==job.get('vocab') and not (current.get('vocab_status') or '').startswith('已保存'):
            update(jid,vocab=json.dumps(T.vocabulary(result),ensure_ascii=False),vocab_status='词汇已生成，不需要额外确认')
        with db() as c:c.execute("UPDATE actions SET status='SUCCEEDED',finished=? WHERE id=?",(time.time(),aid))
    except Exception:
        update(jid,vocab_status='词汇生成未完成，转录不受影响；可检查 LLM 配置后重试')
        with db() as c:c.execute("UPDATE actions SET status='FAILED',error='词汇生成失败，转录不受影响',finished=? WHERE id=?",(time.time(),aid))
        if force:raise

class UploadProgress:
    """Persist byte counts with bounded write frequency. Confirmation is separate."""
    def __init__(self, jid, total):
        self.jid, self.total = jid, total
        self.last_write = float('-inf')
        self.sent = 0

    def report(self, position):
        position = min(self.total, max(self.sent, position))
        now = time.monotonic()
        if position == self.sent:
            return
        self.sent = position
        if position == self.total or now - self.last_write >= 0.5:
            update(self.jid, upload_sent=position, upload_total=self.total)
            self.last_write = now

class ProgressFile:
    """HTTPX reads this file as it streams multipart bytes to its connection."""
    def __init__(self, source, reporter):
        self.source, self.reporter = source, reporter

    def read(self, size=-1):
        chunk = self.source.read(size)
        if chunk:
            self.reporter.report(self.source.tell())
        return chunk

    def __getattr__(self, name):
        return getattr(self.source, name)

def temporary_upload(client, cfg, jid, name, path=None):
    path = path or DATA / (jid+'.audio')
    total = path.stat().st_size
    if total > MAX_AUDIO_BYTES:
        raise ValueError('临时上传上限 1 GB，请使用更小的音频；原音频仍保留在本机')
    update(jid,upload_started=time.time(),upload_total=total,upload_sent=0,upload_completed=0)
    host = 'dashscope.aliyuncs.com' if cfg['region']=='cn-beijing' else 'dashscope-intl.aliyuncs.com'
    r = client.get('https://'+host+'/api/v1/uploads', headers={'Authorization':'Bearer '+cfg['api_key']}, params={'action':'getPolicy','model':MODEL})
    r.raise_for_status()
    p = r.json()['data']
    upload_host = p['upload_host']
    if not re.fullmatch(r'https://[A-Za-z0-9.-]+\.aliyuncs\.com/?',upload_host):
        raise ValueError('临时上传目标不是官方 HTTPS 地址')
    filename = jid+'.'+name.rsplit('.',1)[-1].lower()
    key = p['upload_dir']+'/'+filename
    fields = {'OSSAccessKeyId':p['oss_access_key_id'],'Signature':p['signature'],'policy':p['policy'],
              'x-oss-object-acl':p['x_oss_object_acl'],'x-oss-forbid-overwrite':p['x_oss_forbid_overwrite'],
              'key':key,'success_action_status':'200'}
    update(jid,cleanup='百炼临时副本约 48 小时自动清理，应用无法提前删除')
    with path.open('rb') as source:
        stream = ProgressFile(source, UploadProgress(jid,total))
        r = client.post(upload_host,data=fields,files={'file':(filename,stream,'application/octet-stream')},timeout=3600)
    r.raise_for_status()
    # Reading all bytes is not confirmation. Only a successful upload response completes the bar.
    update(jid,upload_sent=total,upload_total=total,upload_completed=1)
    return 'oss://'+key

def extract(result):
    transcripts = result.get('transcripts',[])
    if not transcripts:
        raise ValueError('结果没有 transcripts，请保留任务以便排查')
    return '\n\n'.join(t.get('text','') or '\n'.join(s.get('text','') for s in t.get('sentences',[])) for t in transcripts)

def worker(jid):
    call_id=uuid.uuid4().hex
    try:
        with db() as c:c.execute('INSERT INTO actions VALUES(?,?,?,?,?,?,?)',(call_id,jid,'filetrans:'+MODEL,'RUNNING',None,time.time(),None))
        cfg = config()
        job = get_job(jid)
        if jid in CANCEL:raise ValueError('已取消本地排队，尚未提交云端识别')
        prepared=DATA/(jid+'.asr.flac')
        if not job['task']:
            update(jid,status='WAITING_UPLOAD',error='正在本地校验音频并准备完整声道识别副本')
            info=audio.prepare(DATA/(jid+'.audio'),prepared)
            if prepared.stat().st_size>MAX_AUDIO_BYTES:raise ValueError('识别副本超过临时上传1GB限制，请先分段')
            ensure_vocabulary(jid)
            job=get_job(jid)
        base = f"https://{cfg['workspace_id']}.{cfg['region']}.maas.aliyuncs.com/api/v1"
        headers = {'Authorization':'Bearer '+cfg['api_key']}
        with httpx.Client(timeout=60,follow_redirects=False) as client:
            if not job['task']:
                if jid in CANCEL:raise ValueError('已取消本地等待，尚未提交云端识别')
                update(jid,status='UPLOADING',error=None)
                url = temporary_upload(client,cfg,jid,'recognition.flac',prepared)
                if jid in CANCEL:raise ValueError('已取消本地等待，尚未提交云端识别')
                update(jid,status='SUBMITTING',recognition_started=time.time())
                words,context=T.hints(json.loads(job.get('vocab') or '{}'))
                parameters={'channel_id':[0]}
                if words:parameters['vocabulary']=words
                if job.get('diarization'):parameters['diarization_enabled']=True
                input_data={'file_urls':[url]}
                if context:input_data['context']=context
                # Do not retry submission timeouts: they may already have created a billed task.
                request_snapshot={'model':MODEL,'input':{'context':context},'parameters':parameters,'vocabulary_snapshot':json.loads(job.get('vocab') or '{}'),'audio':info}
                with db() as c:c.execute('INSERT INTO model_runs VALUES(?,?,?,?,?,?,?,?)',(call_id,jid,None,None,MODEL,json.dumps(request_snapshot,ensure_ascii=False),None,time.time()))
                r = client.post(base+'/services/audio/asr/transcription',headers={**headers,'X-DashScope-Async':'enable','X-DashScope-OssResourceResolve':'enable'},json={'model':MODEL,'input':input_data,'parameters':parameters})
                r.raise_for_status()
                task = r.json()['output']['task_id']
                update(jid,task=task,status='PENDING')
                with db() as c:c.execute('UPDATE model_runs SET task=? WHERE id=?',(task,call_id))
            else:
                task = job['task']
                update(jid,status='PENDING',error=None,finished=None,
                       recognition_started=job.get('recognition_started') or time.time())
            failures = 0
            deadline=time.monotonic()+MAX_WAIT_SECONDS
            while True:
                if jid in CANCEL:raise ValueError('已停止本地等待；云端任务可能仍在运行，可继续查询原任务，不会重新提交')
                if time.monotonic()>deadline:raise ValueError('查询超过六小时，已暂停等待；可继续查询原任务')
                try:
                    r = client.get(base+'/tasks/'+task,headers=headers)
                    r.raise_for_status()
                    out = r.json()['output']
                    state = out['task_status']
                    update(jid,last_polled=time.time())
                    if state == 'SUCCEEDED':
                        results = out.get('results',[])
                        if not results or results[0].get('subtask_status') != 'SUCCEEDED':
                            update(jid,status='FAILED',finished=time.time(),error='音频子任务失败，请检查格式和云端文件可达性')
                            return
                        update(jid,status='FETCHING_RESULT',error=None)
                        result_url = results[0]['transcription_url']
                        if not result_url.startswith('https://'):
                            raise ValueError('识别结果地址不是 HTTPS')
                        result_response = client.get(result_url)
                        result_response.raise_for_status()
                        raw = result_response.json()
                        segments=T.paragraphs(raw,namespace=task)
                        names=json.loads(job.get('speakers') or '{}')
                        run_speakers=[]
                        for segment in segments:
                            sid=segment['speaker']
                            if sid and sid not in run_speakers:run_speakers.append(sid)
                            if sid and sid not in names:names[sid]=f'Speaker {run_speakers.index(sid)+1}'
                        with db() as c:
                            rid=T.add(c,jid,'filetrans',extract(raw),segments,model=MODEL)
                            if not c.execute('SELECT 1 FROM model_runs WHERE task=? AND job=?',(task,jid)).fetchone():
                                c.execute('INSERT INTO model_runs VALUES(?,?,?,?,?,?,?,?)',(call_id,jid,None,task,MODEL,'{}',None,time.time()))
                            c.execute('UPDATE model_runs SET revision=?,raw=? WHERE task=? AND job=?',(rid,json.dumps(raw,ensure_ascii=False),task,jid))
                            c.execute('UPDATE jobs SET speakers=?,original=?,raw=?,status=?,error=NULL,finished=? WHERE id=?',
                                      (json.dumps(names,ensure_ascii=False),extract(raw),json.dumps(raw,ensure_ascii=False),'SUCCEEDED',time.time(),jid))
                        return
                    if state in ('FAILED','CANCELED','UNKNOWN'):
                        update(jid,status='FAILED',finished=time.time(),error='云端任务失败：'+str(out.get('code',state)))
                        return
                    update(jid,status='RUNNING' if state=='RUNNING' else 'PENDING',error=None)
                    failures = 0
                    time.sleep(5)
                except httpx.HTTPError:
                    failures += 1
                    if failures >= 6:
                        raise
                    update(jid,error='查询暂时失败，正在重试；原音频安全保留')
                    time.sleep(min(2**failures,30))
    except Exception as exc:
        job = get_job(jid)
        # Never reveal signed URLs, headers, or secrets in the browser or logs.
        message = str(exc) if isinstance(exc,ValueError) else type(exc).__name__+'：连接或服务异常，请检查配置后重试'
        update(jid,status='INTERRUPTED' if job['task'] or job['status']=='SUBMITTING' else 'FAILED',error=message,finished=time.time())
    finally:
        j=get_job(jid)
        with db() as c:c.execute('UPDATE actions SET status=?,error=?,finished=? WHERE id=?',('SUCCEEDED' if j['status']=='SUCCEEDED' else 'FAILED',j['error'],time.time(),call_id))
        (DATA/(jid+'.asr.flac')).unlink(missing_ok=True)
        with LOCK:
            ACTIVE.discard(jid);CANCEL.discard(jid)

@asynccontextmanager
async def lifespan(app):
    if PUBLIC_ORIGIN and (not PUBLIC_ORIGIN.startswith('https://') or len(ACCESS_TOKEN)<24 or ACCESS_TOKEN.startswith('replace-')):
        raise RuntimeError('公网模式需要 HTTPS 来源和至少24字符的随机访问口令')
    with db() as c:
        # A stopped upload has not submitted ASR; a stopped submission may already be billed.
        c.execute("UPDATE jobs SET status='FAILED',error='上传曾中断，可重新上传；本机音频保留',finished=? WHERE status IN ('WAITING_UPLOAD','UPLOADING')",(time.time(),))
        c.execute("UPDATE jobs SET status='INTERRUPTED',error='程序曾退出；可继续查询已有任务。无任务 ID 的提交请先核对百炼控制台。',finished=? WHERE status IN ('SUBMITTING','PENDING','RUNNING','FETCHING_RESULT')",(time.time(),))
        c.execute("UPDATE actions SET status='FAILED',error='程序退出导致任务中断，可手动重试',finished=? WHERE status='RUNNING'",(time.time(),))
        T.recover_live(c)
    yield

app = FastAPI(lifespan=lifespan)

PUBLIC_ORIGIN=os.environ.get('LT_PUBLIC_ORIGIN','').rstrip('/')
FRONTEND_ORIGIN=os.environ.get('LT_FRONTEND_ORIGIN','').rstrip('/')
ACCESS_TOKEN=os.environ.get('LT_ACCESS_TOKEN','')

def authorized(connection):
    if not PUBLIC_ORIGIN:return True
    if len(ACCESS_TOKEN)<24:return False
    token=bridge.access_token(connection)
    return secrets.compare_digest(token,ACCESS_TOKEN) or bridge.ticket_valid(ACCESS_TOKEN,connection)

def allowed(connection):
    host=connection.headers.get('host','')
    origin=connection.headers.get('origin')
    hosts={'127.0.0.1:8765','localhost:8765','testserver'}
    origins={'http://127.0.0.1:8765','http://localhost:8765'}
    if PUBLIC_ORIGIN:
        hosts.add(urlparse(PUBLIC_ORIGIN).netloc);origins.add(PUBLIC_ORIGIN)
    if FRONTEND_ORIGIN:origins.add(FRONTEND_ORIGIN)
    return host in hosts and (origin is None or origin in origins)

@app.middleware('http')
async def local_only(request,call_next):
    if not allowed(request):
        return JSONResponse({'detail':'仅允许本机访问'},403)
    if request.url.path.startswith('/api/') and request.url.path!='/api/login' and not authorized(request):
        return JSONResponse({'detail':'请先输入私人访问口令'},401)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    return response

@app.post('/api/login')
async def login(request:Request):
    body=await request.json()
    if not PUBLIC_ORIGIN or len(ACCESS_TOKEN)<24 or not secrets.compare_digest(str(body.get('token','')),ACCESS_TOKEN):
        raise HTTPException(403,'访问口令无效或服务未启用公网模式')
    response=JSONResponse({'ok':True})
    response.set_cookie('lecture_access',ACCESS_TOKEN,httponly=True,secure=True,samesite='strict',max_age=86400*7)
    return response

@app.get('/api/config')
def configuration():
    try:
        config()
        cfg=config()
        return {'ready':True,'message':'百炼配置已填写 · 使用临时上传，无需自有 OSS','llm_model':cfg.get('llm_model') or 'qwen-plus','llm_base_url':cfg.get('llm_base_url',''),'llm_ready':True,'workspace_id':cfg['workspace_id'],'region':cfg['region'],'public':bool(PUBLIC_ORIGIN)}
    except ValueError as e:
        return {'ready':False,'message':str(e)}

def public_job(row, summary=False):
    hidden = {'object_key','raw'}
    if summary:
        hidden |= {'original','edited','task','vocab','speakers'}
    result = {k:v for k,v in row.items() if k not in hidden}
    with LOCK:
        result['active'] = row['id'] in ACTIVE
    result['server_time'] = time.time()
    return result

@app.get('/api/jobs')
def jobs(limit:int=50,offset:int=0):
    with db() as c:
        rows = [dict(r) for r in c.execute('SELECT id,name,status,created,title,topic,recorded_at,storage_mode,upload_sent,upload_total,upload_completed,upload_started,recognition_started,finished,last_polled,error,cleanup FROM jobs ORDER BY created DESC LIMIT ? OFFSET ?',(min(200,max(1,limit)),max(0,offset)))]
    return [public_job(r,summary=True) for r in rows]

@app.get('/api/jobs/{jid}')
def detail(jid: str):
    return public_job(get_job(jid))

@app.post('/api/audio')
async def upload(request: Request,name: str):
    name = name.replace('\\','/').split('/')[-1][:200]
    if '.' not in name or name.rsplit('.',1)[-1].lower() not in FORMATS:
        raise HTTPException(400,'不支持的文件扩展名')
    length = request.headers.get('content-length')
    if length and length.isdecimal() and int(length)>MAX_AUDIO_BYTES:
        raise HTTPException(413,'临时上传支持的音频上限为 1 GB，请选择更小的文件')
    jid = uuid.uuid4().hex
    path = DATA / (jid+'.audio')
    size = 0
    try:
        with path.open('wb') as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size>MAX_AUDIO_BYTES:
                    raise HTTPException(413,'临时上传支持的音频上限为 1 GB')
                f.write(chunk)
        if not size:
            raise HTTPException(400,'文件为空')
        with db() as c:
            c.execute('INSERT INTO jobs(id,name,status,created,storage_mode) VALUES(?,?,?,?,?)',(jid,name,'LOCAL',time.time(),'local'))
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return {'id':jid}

@app.post('/api/jobs/{jid}/start')
def start(jid: str,force:bool=False):
    job = get_job(jid)
    if job['status']=='SUCCEEDED' and not force:
        return {'ok':True}
    if job['status']=='INTERRUPTED' and not job['task']:
        raise HTTPException(409,'提交结果不确定，为避免重复计费，请先在百炼控制台核对任务。保留原音频后可重新导入。')
    if not (DATA/(jid+'.audio')).exists():raise HTTPException(400,'完整音频尚未保存')
    if (force or not job['task']) and (DATA/(jid+'.audio')).stat().st_size>MAX_AUDIO_BYTES:
        raise HTTPException(413,'临时上传上限 1 GB；这份旧音频仍安全保留在本机，请使用更小的文件')
    try:
        config()
    except ValueError as e:
        raise HTTPException(400,str(e))
    with LOCK:
        if jid in ACTIVE:
            return {'ok':True}
        values = {'status':'PENDING' if job['task'] and not force else 'WAITING_UPLOAD','error':None,'finished':None}
        if force or not job['task']:
            values.update(storage_mode='temporary',upload_sent=0,upload_total=0,upload_completed=0,upload_started=None,recognition_started=None,last_polled=None)
            if force:values['task']=None
        update(jid,**values)
        CANCEL.discard(jid)
        ACTIVE.add(jid)
    try:
        POOL.submit(worker,jid)
    except Exception:
        with LOCK:
            ACTIVE.discard(jid)
        update(jid,status='FAILED',error='任务启动失败，请重试',finished=time.time())
        raise HTTPException(503,'任务启动失败，请重试')
    return {'ok':True}

class Edit(BaseModel):
    text: str

@app.put('/api/jobs/{jid}/edit')
def edit(jid: str,body: Edit):
    if get_job(jid)['status']!='SUCCEEDED':
        raise HTTPException(409,'尚未完成识别')
    with (DATA/(jid+'.edits.jsonl')).open('a',encoding='utf-8') as f:
        f.write(json.dumps({'time':time.time(),'text':body.text},ensure_ascii=False)+'\n')
    update(jid,edited=body.text)
    with db() as c:T.add(c,jid,'user_raw',body.text)
    return {'ok':True}

app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')

@app.get('/')
def index():
    return FileResponse(ROOT/'static'/'index.html')

@app.get('/sw.js')
def service_worker():
    return FileResponse(ROOT/'static'/'sw.js',media_type='application/javascript',headers={'Service-Worker-Allowed':'/'})

from services.workspace import install
install(app,sys.modules[__name__])
from services.streaming import install as install_streaming
install_streaming(app,sys.modules[__name__])
bridge.install(app,sys.modules[__name__])
from services.transfers import install as install_transfers
install_transfers(app,sys.modules[__name__])
if FRONTEND_ORIGIN:
    app.add_middleware(CORSMiddleware,allow_origins=[FRONTEND_ORIGIN],allow_credentials=True,
                       allow_methods=['GET','POST','PUT','PATCH','DELETE'],allow_headers=['Content-Type','Authorization','X-Chunk-SHA256'])
