"""Project and immutable-version APIs, installed against the desktop runtime."""
import difflib
import io
import json
import secrets
import time
import uuid
import zipfile
from fastapi import HTTPException,Request
from fastapi.responses import FileResponse,Response
from . import transcripts as T,language

def install(app,R):
    record_uploads=set()
    def revision(jid,rid):
        with R.db() as c:
            row=c.execute('SELECT * FROM revisions WHERE id=? AND job=?',(rid,jid)).fetchone()
        if not row:raise HTTPException(404,'版本不存在')
        return dict(row)

    def action(jid,kind,fn):
        R.get_job(jid)
        aid=uuid.uuid4().hex
        with R.db() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute("SELECT 1 FROM actions WHERE job=? AND status='RUNNING'",(jid,)).fetchone():raise HTTPException(409,'本项目还有 AI 任务执行中')
            c.execute('INSERT INTO actions VALUES(?,?,?,?,?,?,?)',(aid,jid,kind,'RUNNING',None,time.time(),None))
        def run():
            error=None
            try:fn()
            except Exception as e:error=str(e) if isinstance(e,ValueError) else type(e).__name__+'：模型调用失败，请检查配置后重试'
            with R.db() as c:c.execute('UPDATE actions SET status=?,error=?,finished=? WHERE id=?',('FAILED' if error else 'SUCCEEDED',error,time.time(),aid))
        R.POOL.submit(run)
        return {'id':aid}

    @app.patch('/api/jobs/{jid}')
    async def project(jid:str,request:Request):
        prior=R.get_job(jid);body=await request.json()
        values={k:str(body[k])[:2000] for k in ('title','topic','recorded_at') if k in body}
        if 'topic' in values and values['topic']!=(prior.get('topic') or ''):
            values.update(vocab='{}',vocab_status='主题已更新，将在下次转录前生成词汇')
        if 'diarization' in body:values['diarization']=int(bool(body['diarization']))
        if 'speakers' in body:
            if not isinstance(body['speakers'],dict):raise HTTPException(400,'说话人格式错误')
            values['speakers']=json.dumps({str(k)[:100]:str(v)[:100] for k,v in body['speakers'].items()},ensure_ascii=False)
        if values:R.update(jid,**values)
        return {'ok':True}

    @app.get('/api/jobs/{jid}/versions')
    def versions(jid:str):
        R.get_job(jid)
        with R.db() as c:return [dict(r) for r in c.execute('SELECT * FROM revisions WHERE job=? ORDER BY created DESC',(jid,))]

    @app.post('/api/jobs/{jid}/versions')
    async def save_version(jid:str,request:Request):
        body=await request.json();source=revision(jid,body['parent']);text=str(body['text'])
        kind='tidy_edit' if source['kind'].startswith('tidy') else 'user_raw'
        # Paragraph edits carry a stable timestamp/speaker association; arbitrary text edits do not invent one.
        segments=body.get('segments',[])
        if segments:
            prior=json.loads(source['segments'])
            if len(prior)!=len(segments):raise HTTPException(400,'段落数量不匹配')
            segments=[{**p,'text':str(s['text'])} for p,s in zip(prior,segments)]
            text='\n\n'.join(s['text'] for s in segments)
        with R.db() as c:rid=T.add(c,jid,kind,text,segments,parent=source['id'])
        if kind=='user_raw':R.update(jid,edited=text)
        return {'id':rid}

    @app.get('/api/jobs/{jid}/diff')
    def diff(jid:str,before:str,after:str):
        a=revision(jid,before);b=revision(jid,after)
        return {'lines':list(difflib.unified_diff(a['text'].splitlines(),b['text'].splitlines(),fromfile='整理前',tofile='整理后',lineterm=''))}

    @app.put('/api/jobs/{jid}/vocabulary')
    async def vocab_save(jid:str,request:Request):
        R.get_job(jid);v=T.vocabulary(await request.json())
        R.update(jid,vocab=json.dumps(v,ensure_ascii=False),vocab_status='已保存；新任务及下次实时连接将使用')
        return v

    @app.post('/api/jobs/{jid}/vocabulary/generate')
    def vocab_generate(jid:str):
        return action(jid,'vocabulary',lambda:R.ensure_vocabulary(jid,force=True))

    @app.post('/api/jobs/{jid}/tidy')
    async def tidy(jid:str,request:Request):
        body=await request.json();mode=body.get('mode');source=revision(jid,body['version'])
        if mode not in ('light','deep'):raise HTTPException(400,'无效整理模式')
        def perform():
            cfg=R.config();text=language.tidy(cfg,source['text'],mode)
            with R.db() as c:T.add(c,jid,'tidy_'+mode,text,parent=source['id'],model=cfg.get('llm_model') or 'qwen-plus')
        return action(jid,'tidy_'+mode,perform)

    @app.get('/api/jobs/{jid}/actions')
    def actions(jid:str):
        R.get_job(jid)
        with R.db() as c:return [dict(r) for r in c.execute('SELECT * FROM actions WHERE job=? ORDER BY created DESC LIMIT 50',(jid,))]

    @app.get('/api/jobs/{jid}/export')
    def export(jid:str,version:str,format:str='txt',timestamps:bool=False,speakers:bool=False):
        j=R.get_job(jid);v=revision(jid,version)
        if format not in ('txt','md'):raise HTTPException(400,'仅支持 TXT 或 Markdown')
        text=T.export_text(v,json.loads(j['speakers'] or '{}'),timestamps,speakers)
        if format=='md':text='# '+(j['title'] or j['name'])+'\n\n'+text
        return Response(text.encode('utf-8-sig'),media_type='text/plain;charset=utf-8',headers={'Content-Disposition':f'attachment; filename="transcript.{format}"'})

    @app.get('/api/jobs/{jid}/audio')
    def audio(jid:str):
        j=R.get_job(jid);path=R.DATA/(jid+'.audio')
        if not path.exists():raise HTTPException(404,'完整音频尚未上传，片段保留在录音设备')
        return FileResponse(path,filename=j['name'])

    @app.post('/api/jobs/{jid}/recognize-again')
    def again(jid:str):
        j=R.get_job(jid)
        if jid in R.ACTIVE:raise HTTPException(409,'任务执行中')
        # New recognition run on the same project; old immutable revisions and edits remain.
        path=R.DATA/(jid+'.audio')
        if not path.exists():raise HTTPException(400,'尚无完整音频')
        R.start(jid,force=True)
        return {'id':jid}

    @app.get('/api/jobs/{jid}/backup')
    def backup(jid:str):
        j=R.get_job(jid)
        path=R.DATA/(jid+'.audio')
        if not path.exists():raise HTTPException(400,'请先从录音设备导出音频')
        # The archive is streamed from disk, without reading a large lecture into memory.
        out=R.DATA/(jid+'.backup.zip')
        with R.db() as c:
            revs=[dict(r) for r in c.execute('SELECT * FROM revisions WHERE job=?',(jid,))]
        with zipfile.ZipFile(out,'w',zipfile.ZIP_STORED) as z:
            z.writestr('project.json',json.dumps({'schema':1,'project':{k:v for k,v in j.items() if k not in ('task','object_key')},'versions':revs},ensure_ascii=False))
            z.write(path,'audio.bin')
        return FileResponse(out,filename='lecture-backup.zip')

    @app.post('/api/restore')
    async def restore(request:Request):
        temp=R.DATA/(uuid.uuid4().hex+'.restore');size=0
        try:
            with temp.open('wb') as f:
                async for chunk in request.stream():
                    size+=len(chunk)
                    if size>1_050_000_000:raise HTTPException(413,'备份超过大小限制')
                    f.write(chunk)
            with zipfile.ZipFile(temp) as z:
                info=z.getinfo('project.json');audio_info=z.getinfo('audio.bin')
                if info.file_size>20_000_000 or audio_info.file_size>R.MAX_AUDIO_BYTES:raise ValueError('文件超过恢复限制')
                data=json.loads(z.read(info));j=data['project'];nid=uuid.uuid4().hex
                if data['schema']!=1:raise ValueError('备份版本不兼容')
                revs=data.get('versions',[])
                # Parse all metadata before touching the destination; never extract arbitrary archive paths.
                for v in revs:
                    json.loads(v['segments']);assert isinstance(v['text'],str)
                with z.open('audio.bin') as src,(R.DATA/(nid+'.audio')).open('wb') as dst:
                    import shutil
                    shutil.copyfileobj(src,dst)
                try:
                    with R.db() as c:
                        c.execute('INSERT INTO jobs(id,name,status,created,title,topic,recorded_at,vocab,speakers,original,edited,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(nid,str(j['name'])[:200],'SUCCEEDED' if j.get('original') is not None else 'LOCAL',time.time(),j.get('title'),j.get('topic',''),j.get('recorded_at'),j.get('vocab','{}'),j.get('speakers','{}'),j.get('original'),j.get('edited'),j.get('raw')))
                        ids={v['id']:uuid.uuid4().hex for v in revs}
                        for v in revs:c.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?)',(ids[v['id']],nid,v['kind'],v['text'],v['segments'],ids.get(v.get('parent')),v.get('model'),v['created']))
                except BaseException:
                    (R.DATA/(nid+'.audio')).unlink(missing_ok=True);raise
            return {'id':nid}
        except (ValueError,KeyError,zipfile.BadZipFile,AssertionError):raise HTTPException(400,'无效或损坏的项目备份')
        finally:temp.unlink(missing_ok=True)

    @app.post('/api/recordings/prepare')
    async def recording(request:Request):
        b=await request.json();rid=str(b['id'])
        if not rid.replace('-','').isalnum() or len(rid)>100:raise HTTPException(400,'无效录音 ID')
        with R.db() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT id FROM jobs WHERE recording_id=?',(rid,)).fetchone()
            if old:return {'id':old['id']}
            nid=uuid.uuid4().hex
            c.execute('INSERT INTO jobs(id,name,title,topic,status,created,recording_id) VALUES(?,?,?,?,?,?,?)',(nid,str(b.get('name','现场录音.webm'))[:200],str(b.get('title','现场录音'))[:200],str(b.get('topic',''))[:2000],'RECORDING',time.time(),rid))
        return {'id':nid}

    @app.put('/api/jobs/{jid}/recorded-audio')
    async def recorded(jid:str,request:Request):
        j=R.get_job(jid)
        if not j['recording_id']:raise HTTPException(400,'不是现场录音项目')
        dest=R.DATA/(jid+'.audio');temp=R.DATA/(jid+'.pending')
        if dest.exists():return {'id':jid,'already_saved':True}
        if jid in record_uploads:raise HTTPException(409,'完整音频正在上传，请等待当前请求完成')
        record_uploads.add(jid)
        n=0
        try:
            with temp.open('wb') as f:
                async for chunk in request.stream():
                    n+=len(chunk)
                    if n>R.MAX_AUDIO_BYTES:raise HTTPException(413,'录音超过 1 GB，请先导出到本机')
                    f.write(chunk)
            if n==0:raise HTTPException(400,'音频为空')
            temp.replace(dest);R.update(jid,status='LOCAL')
            return {'id':jid}
        finally:
            record_uploads.discard(jid)
            temp.unlink(missing_ok=True)

    @app.put('/api/config')
    async def settings(request:Request):
        b=await request.json()
        try:old=json.loads(R.CONFIG_FILE.read_text('utf-8-sig'))
        except (OSError,ValueError):old={}
        for key in ('api_key','workspace_id','region','llm_model','llm_base_url','llm_api_key'):
            if key in b and (b[key] or 'key' not in key):old[key]=str(b[key]).strip()
        R.CONFIG_FILE.parent.mkdir(parents=True,exist_ok=True)
        tmp=R.CONFIG_FILE.with_suffix('.json.tmp');tmp.write_text(json.dumps(old,indent=2),encoding='utf-8');tmp.replace(R.CONFIG_FILE)
        return {'ok':True}
