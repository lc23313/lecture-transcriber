"""Resumable, hash-checked recording uploads and explicit storage management."""
import asyncio
import hashlib
import time
import uuid
from fastapi import HTTPException,Request

PART_SIZE=4*1024*1024


def install(app,R):
    locks={}
    with R.db() as c:c.execute('CREATE TABLE IF NOT EXISTS upload_parts(job TEXT,seq INTEGER,size INTEGER,hash TEXT,PRIMARY KEY(job,seq))')
    def guard(jid):
        j=R.get_job(jid)
        if not j['recording_id']:raise HTTPException(400,'不是现场录音项目')
        return j
    @app.get('/api/jobs/{jid}/recorded-upload')
    def status(jid:str):
        guard(jid)
        with R.db() as c:parts=[dict(p) for p in c.execute('SELECT seq,size,hash FROM upload_parts WHERE job=? ORDER BY seq',(jid,)) if (R.DATA/f'{jid}.part{p["seq"]}').exists() and hashlib.sha256((R.DATA/f'{jid}.part{p["seq"]}').read_bytes()).hexdigest()==p['hash']]
        return {'completed':(R.DATA/(jid+'.audio')).exists(),'parts':parts,'part_size':PART_SIZE}
    @app.put('/api/jobs/{jid}/recorded-upload/{seq}')
    async def part(jid:str,seq:int,request:Request):
        guard(jid)
        if not 0<=seq<240:raise HTTPException(400,'片段序号超限')
        expected=request.headers.get('x-chunk-sha256','')
        if len(expected)!=64:raise HTTPException(400,'缺少片段校验值')
        async with locks.setdefault(jid,asyncio.Lock()):
            if (R.DATA/(jid+'.audio')).exists():raise HTTPException(409,'完整音频已保存')
            buf=bytearray()
            async for chunk in request.stream():
                buf.extend(chunk)
                if len(buf)>PART_SIZE:raise HTTPException(413,'上传片段超过4MB')
            digest=hashlib.sha256(buf).hexdigest()
            if not buf or digest!=expected:raise HTTPException(400,'片段校验失败，请重试')
            dest=R.DATA/f'{jid}.part{seq}'
            with R.db() as c:prior=c.execute('SELECT hash FROM upload_parts WHERE job=? AND seq=?',(jid,seq)).fetchone()
            if prior and prior['hash']!=digest:raise HTTPException(409,'同序号片段内容不一致')
            temp=R.DATA/(uuid.uuid4().hex+'.uploadtmp')
            try:
                temp.write_bytes(buf);temp.replace(dest)
                with R.db() as c:c.execute('INSERT OR REPLACE INTO upload_parts VALUES(?,?,?,?)',(jid,seq,len(buf),digest))
            finally:temp.unlink(missing_ok=True)
            return {'seq':seq,'hash':digest,'size':len(buf)}
    @app.post('/api/jobs/{jid}/recorded-upload/complete')
    async def complete(jid:str,request:Request):
        guard(jid);body=await request.json()
        try:count=int(body.get('count',0));total=int(body.get('bytes',0))
        except (TypeError,ValueError):raise HTTPException(400,'片段数量或大小格式错误')
        if not 1<=count<=240 or not 0<total<=R.MAX_AUDIO_BYTES:raise HTTPException(400,'片段数量或总大小错误')
        async with locks.setdefault(jid,asyncio.Lock()):
            dest=R.DATA/(jid+'.audio');temp=R.DATA/(jid+'.pending')
            if dest.exists():return {'id':jid,'already_saved':True}
            with R.db() as c:rows=[dict(p) for p in c.execute('SELECT * FROM upload_parts WHERE job=? ORDER BY seq',(jid,))]
            if [r['seq'] for r in rows]!=list(range(count)):raise HTTPException(409,'片段不完整，可继续上传')
            try:
                size=0
                with temp.open('wb') as out:
                    for row in rows:
                        content=(R.DATA/f'{jid}.part{row["seq"]}').read_bytes()
                        if hashlib.sha256(content).hexdigest()!=row['hash']:raise HTTPException(409,'已保存片段校验失败，请重新上传该片段')
                        out.write(content);size+=len(content)
                if size!=total:raise HTTPException(409,'音频总大小不匹配')
                temp.replace(dest);R.update(jid,status='LOCAL')
                for row in rows:(R.DATA/f'{jid}.part{row["seq"]}').unlink(missing_ok=True)
                with R.db() as c:c.execute('DELETE FROM upload_parts WHERE job=?',(jid,))
                return {'id':jid,'bytes':size}
            finally:temp.unlink(missing_ok=True)
    @app.post('/api/jobs/{jid}/stop-waiting')
    def stop_waiting(jid:str):
        R.get_job(jid)
        with R.LOCK:R.CANCEL.add(jid)
        return {'ok':True,'message':'仅停止本地等待；云端可能继续运行，可稍后查询已有任务'}
    @app.get('/api/storage')
    def storage():
        files=[p for p in R.DATA.iterdir() if p.is_file()]
        return {'bytes':sum(p.stat().st_size for p in files),'files':len(files)}
    @app.get('/api/jobs/{jid}/runs')
    def runs(jid:str):
        R.get_job(jid)
        with R.db() as c:return [dict(r) for r in c.execute('SELECT id,revision,task,model,request,created FROM model_runs WHERE job=? ORDER BY created DESC',(jid,))]
    @app.get('/api/jobs/{jid}/runs/{rid}')
    def run(jid:str,rid:str):
        R.get_job(jid)
        with R.db() as c:row=c.execute('SELECT * FROM model_runs WHERE id=? AND job=?',(rid,jid)).fetchone()
        if not row:raise HTTPException(404,'调用记录不存在')
        return dict(row)
    @app.delete('/api/jobs/{jid}')
    async def delete(jid:str,request:Request):
        j=R.get_job(jid);body=await request.json()
        if body.get('confirm')!=jid:raise HTTPException(400,'删除确认不匹配')
        with R.LOCK:
            if jid in R.ACTIVE:raise HTTPException(409,'请先等待任务结束')
            with R.db() as c:
                if c.execute("SELECT 1 FROM actions WHERE job=? AND status='RUNNING'",(jid,)).fetchone():raise HTTPException(409,'请先结束实时或AI任务')
                for table in ('revisions','actions','live_sentences','live_archives','model_runs','upload_parts'):c.execute(f'DELETE FROM {table} WHERE job=?',(jid,))
                c.execute('DELETE FROM jobs WHERE id=?',(jid,))
            for path in R.DATA.glob(jid+'.*'):
                if path.is_file():path.unlink()
        return {'ok':True}
