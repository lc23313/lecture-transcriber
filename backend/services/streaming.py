import asyncio
import json
import time
import uuid
from fastapi import WebSocket
from websockets.asyncio.client import connect
from . import transcripts as T

MODEL='qwen-audio-3.1-asr-flash-streaming'

def run_message(task,vocab):
    words,context=T.hints(vocab)
    parameters={'format':'pcm','sample_rate':16000,'heartbeat':True}
    if words:parameters['vocabulary']=words
    return {'header':{'action':'run-task','task_id':task,'streaming':'duplex'},
            'payload':{'task_group':'audio','task':'asr','function':'recognition','model':MODEL,
                       'parameters':parameters,'input':{'context':context} if context else {}}}

def install(app,R):
    @app.websocket('/api/live/{jid}')
    async def live(ws:WebSocket,jid:str):
        if not R.allowed(ws) or not R.authorized(ws):
            await ws.close(code=1008);return
        protocols=ws.headers.get('sec-websocket-protocol','').split(',')
        await ws.accept(subprotocol='lecture-v1' if 'lecture-v1' in [p.strip() for p in protocols] else None)
        session=uuid.uuid4().hex;task=str(uuid.uuid4());latest={};offset=0
        aid=uuid.uuid4().hex
        try:
            job=R.get_job(jid);cfg=R.config()
            with R.db() as c:c.execute('INSERT INTO actions VALUES(?,?,?,?,?,?,?)',(aid,jid,'streaming','RUNNING',None,time.time(),None))
            hello=await asyncio.wait_for(ws.receive_json(),10)
            offset=max(0,int(hello.get('offset',0)))
            await asyncio.to_thread(R.ensure_vocabulary,jid)
            job=R.get_job(jid)
            uri=f"wss://{cfg['workspace_id']}.{cfg['region']}.maas.aliyuncs.com/api-ws/v1/inference"
            async with connect(uri,additional_headers={'Authorization':'Bearer '+cfg['api_key']},open_timeout=20,max_size=4_000_000) as upstream:
                await upstream.send(json.dumps(run_message(task,json.loads(job.get('vocab') or '{}'))))
                first=json.loads(await asyncio.wait_for(upstream.recv(),30))
                if first.get('header',{}).get('event')!='task-started':raise ValueError('实时识别未能启动，请检查模型权限')
                await ws.send_json({'type':'ready','session':session})
                async def audio():
                    while True:
                        msg=await ws.receive()
                        if msg['type']=='websocket.disconnect':return
                        if msg.get('bytes') is not None:
                            if len(msg['bytes'])>64000:raise ValueError('实时音频包过大')
                            await upstream.send(msg['bytes'])
                        elif msg.get('text'):
                            value=json.loads(msg['text'])
                            if value.get('type')=='finish':
                                await upstream.send(json.dumps({'header':{'action':'finish-task','task_id':task,'streaming':'duplex'},'payload':{'input':{}}}))
                                await asyncio.sleep(20);return
                async def results():
                    async for data in upstream:
                        value=json.loads(data);event=value.get('header',{}).get('event')
                        if event=='task-failed':raise ValueError('实时服务返回任务失败，本地录音不受影响')
                        if event=='task-finished':return
                        if event!='result-generated':continue
                        s=value.get('payload',{}).get('output',{}).get('sentence',{})
                        if s.get('heartbeat'):continue
                        segment={'start':offset+int(s.get('begin_time') or 0),'end':offset+int(s.get('end_time') or s.get('begin_time') or 0),'speaker':None,'text':s.get('text','')}
                        seq=int(s.get('sentence_id',0));latest[seq]=segment
                        # Persist provisional text too, not only final sentence events.
                        with R.db() as c:c.execute('INSERT OR REPLACE INTO live_sentences VALUES(?,?,?,?)',(jid,session,seq,json.dumps(segment,ensure_ascii=False)))
                        await ws.send_json({'type':'sentence','session':session,'seq':seq,'segment':segment,'final':bool(s.get('sentence_end'))})
                tasks=[asyncio.create_task(audio()),asyncio.create_task(results())]
                try:
                    done,pending=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                    for t in done:t.result()
                finally:
                    for t in tasks:
                        if not t.done():t.cancel()
                    await asyncio.gather(*tasks,return_exceptions=True)
            with R.db() as c:c.execute("UPDATE actions SET status='SUCCEEDED',finished=? WHERE id=?",(time.time(),aid))
        except Exception:
            with R.db() as c:c.execute("UPDATE actions SET status='FAILED',error='实时连接中断或服务失败；本地录音仍保留',finished=? WHERE id=?",(time.time(),aid))
            try:await ws.send_json({'type':'error','message':'实时连接中断或服务失败；本地录音仍在保存，可重连'})
            except Exception:pass
        finally:
            if latest:
                segments=[latest[k] for k in sorted(latest)]
                with R.db() as c:
                    rid=T.add(c,jid,'realtime','\n\n'.join(s['text'] for s in segments),segments,model=MODEL)
                    c.execute('INSERT OR REPLACE INTO live_archives VALUES(?,?,?)',(jid,session,rid))
            try:await ws.close()
            except Exception:pass
