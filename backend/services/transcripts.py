import json
import time
import uuid
import re

def migrate(c):
    cols={r[1] for r in c.execute('PRAGMA table_info(jobs)')}
    for name,definition in {'title':'TEXT','topic':"TEXT DEFAULT ''",'recorded_at':'TEXT',
                            'vocab':"TEXT DEFAULT '{}'",'speakers':"TEXT DEFAULT '{}'",'diarization':'INTEGER DEFAULT 0',
                            'vocab_status':"TEXT DEFAULT ''",'recording_id':'TEXT'}.items():
        if name not in cols:
            c.execute(f'ALTER TABLE jobs ADD COLUMN {name} {definition}')
    c.execute('CREATE TABLE IF NOT EXISTS revisions(id TEXT PRIMARY KEY, job TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, segments TEXT NOT NULL, parent TEXT, model TEXT, created REAL)')
    c.execute('CREATE INDEX IF NOT EXISTS revision_job ON revisions(job,created)')
    c.execute('CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY,job TEXT,kind TEXT,status TEXT,error TEXT,created REAL,finished REAL)')
    c.execute('CREATE TABLE IF NOT EXISTS live_sentences(job TEXT,session TEXT,seq INTEGER,data TEXT,PRIMARY KEY(job,session,seq))')
    c.execute('CREATE TABLE IF NOT EXISTS live_archives(job TEXT,session TEXT,revision TEXT,PRIMARY KEY(job,session))')
    c.execute('CREATE TABLE IF NOT EXISTS upload_parts(job TEXT,seq INTEGER,size INTEGER,hash TEXT,PRIMARY KEY(job,seq))')
    c.execute('CREATE TABLE IF NOT EXISTS model_runs(id TEXT PRIMARY KEY,job TEXT,revision TEXT,task TEXT,model TEXT,request TEXT,raw TEXT,created REAL)')
    c.execute('CREATE INDEX IF NOT EXISTS model_run_job ON model_runs(job,created)')
    # Backfill only missing revisions; never replace an existing saved version.
    for row in c.execute("SELECT id,original,edited,raw FROM jobs WHERE original IS NOT NULL AND NOT EXISTS(SELECT 1 FROM revisions WHERE job=jobs.id)").fetchall():
        try:raw=json.loads(row['raw'] or '{}')
        except (TypeError,ValueError):raw={}
        rid=add(c,row['id'],'filetrans',row['original'],paragraphs(raw),model='legacy')
        if row['edited'] is not None:
            add(c,row['id'],'user_raw',row['edited'],parent=rid)

def recover_live(c):
    sessions=c.execute('SELECT DISTINCT job,session FROM live_sentences s WHERE NOT EXISTS(SELECT 1 FROM live_archives a WHERE a.job=s.job AND a.session=s.session)').fetchall()
    for item in sessions:
        parts=[json.loads(r[0]) for r in c.execute('SELECT data FROM live_sentences WHERE job=? AND session=? ORDER BY seq',(item['job'],item['session']))]
        rid=add(c,item['job'],'realtime','\n\n'.join(p.get('text','') for p in parts),parts,model='qwen-audio-3.1-asr-flash-streaming (recovered)')
        c.execute('INSERT INTO live_archives VALUES(?,?,?)',(item['job'],item['session'],rid))

def join_text(left,right):
    if left and right and re.search(r'[A-Za-z0-9]$',left) and re.match(r'[A-Za-z0-9]',right):
        return left+' '+right
    return left+right


def paragraphs(raw,namespace=None):
    result=[]
    for t in raw.get('transcripts',[]):
        channel=t.get('channel_id',0)
        sentences=t.get('sentences') or [{'text':t.get('text','')}]
        for s in sentences:
            start=s.get('begin_time');end=s.get('end_time')
            known=start is not None and end is not None
            speaker=None if s.get('speaker_id') is None else (str(namespace)+':' if namespace else '')+f"channel-{channel}:speaker-{s['speaker_id']}"
            piece={'start':max(0,int(start)) if known else None,'end':max(int(start),int(end)) if known else None,
                   'speaker':speaker,'channel':channel,'text':s.get('text','')}
            previous=result[-1] if result else None
            if known and previous and previous['end'] is not None and previous.get('channel')==channel and previous['speaker']==speaker and 0<=piece['start']-previous['end']<2000 and piece['end']-previous['start']<30000 and len(previous['text'])+len(piece['text'])<250:
                previous['end']=piece['end'];previous['text']=join_text(previous['text'],piece['text'])
            else:result.append(piece)
        # Preserve any aggregate-only text when sentence data is incomplete. Do not guess timestamps.
        aggregate=t.get('text','')
        compact=lambda text: re.sub(r'\s+','',text)
        sentence_text=''.join(item.get('text','') for item in sentences)
        if aggregate and compact(aggregate)!=compact(sentence_text):
            result=[p for p in result if p['channel']!=channel]
            result.append({'start':None,'end':None,'speaker':None,'channel':channel,'text':aggregate})
    return result


def add(c,jid,kind,text,segments=None,parent=None,model=None):
    rid=uuid.uuid4().hex
    c.execute('INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?)',(rid,jid,kind,text,json.dumps(segments or [],ensure_ascii=False),parent,model,time.time()))
    return rid

def vocabulary(value):
    """Keep a short, deduplicated relevant list; weights are hints, never replacements."""
    if not isinstance(value,dict) or not isinstance(value.get('entries',[]),list) or any(not isinstance(e,dict) for e in value.get('entries',[])):raise ValueError('词汇表格式错误')
    entries=[];seen=set()
    for item in value.get('entries',[])[:200]:
        en=str(item.get('en','')).strip()[:100];zh=str(item.get('zh','')).strip()[:100]
        key=(en or zh).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        try: weight=int(item.get('weight',2))
        except (TypeError,ValueError): weight=2
        entries.append({'en':en,'zh':zh,'category':str(item.get('category','term'))[:40],
                        'weight':min(5,max(1,weight)),'note':str(item.get('note',''))[:200]})
        if len(entries)>=100:
            break
    return {'entries':entries,'background':str(value.get('background',''))[:1000],'context':str(value.get('context',''))[:400]}

def hints(value):
    v=vocabulary(value);words={}
    for e in sorted(v['entries'],key=lambda e:e['weight'],reverse=True)[:60]:
        for key in ('en','zh'):
            if e[key]:words[e[key]]=e['weight']
    context=[{'role':'user','content':[{'type':'input_text','text':v['context']}]}] if v['context'] else []
    return words,context

def stamp(ms):
    if ms is None:return '时间未知'
    n=max(0,int(ms)//1000)
    return f'{n//3600:02}:{n//60%60:02}:{n%60:02}'

def export_text(revision,speakers,timestamps=False,labels=False):
    parts=json.loads(revision['segments'])
    if not parts or not (timestamps or labels):return revision['text']
    lines=[]
    for p in parts:
        prefix=''
        if timestamps:prefix+=f"[{stamp(p['start'])}–{stamp(p['end'])}] "
        if labels and p.get('speaker'):
            prefix+=speakers.get(p['speaker'],p['speaker'])+': '
        lines.append(prefix+p['text'])
    return '\n\n'.join(lines)
