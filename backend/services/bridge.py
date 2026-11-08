"""Explicit cross-origin access for a separate private backend and static frontend."""
import base64
import hashlib
import hmac
import re
import time
from urllib.parse import parse_qsl,urlencode,urlsplit
from fastapi import HTTPException,Request

def signature(secret,path,params):
    canonical=path+'?'+urlencode(sorted(params))
    return hmac.new(secret.encode(),canonical.encode(),hashlib.sha256).hexdigest()

def ticket_valid(secret,connection):
    values=list(connection.query_params.multi_items());params=[(k,v) for k,v in values if k!='ticket']
    if len([k for k,v in values if k=='ticket'])!=1 or len([k for k,v in values if k=='expires'])!=1:return False
    try:expires=int(connection.query_params.get('expires','0'))
    except ValueError:return False
    if not time.time()<=expires<=time.time()+120:return False
    if not re.fullmatch(r'/api/jobs/[A-Za-z0-9-]+/(?:audio|backup|export)',connection.url.path):return False
    return hmac.compare_digest(connection.query_params.get('ticket',''),signature(secret,connection.url.path,params))

def access_token(connection):
    authorization=connection.headers.get('authorization','')
    if authorization.startswith('Bearer '):return authorization[7:]
    protocols=connection.headers.get('sec-websocket-protocol','')
    for value in protocols.split(','):
        value=value.strip()
        if value.startswith('lecture-auth.'):
            try:
                encoded=value[len('lecture-auth.'):];return base64.urlsafe_b64decode(encoded+'='*(-len(encoded)%4)).decode()
            except (ValueError,UnicodeDecodeError):return ''
    return connection.cookies.get('lecture_access','')

def install(app,R):
    @app.post('/api/download-ticket')
    async def download_ticket(request:Request):
        body=await request.json();url=urlsplit(str(body.get('path','')))
        if url.scheme or url.netloc or url.fragment or not re.fullmatch(r'/api/jobs/[A-Za-z0-9-]+/(?:audio|backup|export)',url.path):raise HTTPException(400,'下载路径无效')
        R.get_job(url.path.split('/')[3])
        params=parse_qsl(url.query,keep_blank_values=True)
        if any(k not in ('version','format','timestamps','speakers') for k,v in params):raise HTTPException(400,'下载参数无效')
        params.append(('expires',str(int(time.time()+60))))
        params.append(('ticket',signature(R.ACCESS_TOKEN,url.path,params)))
        return {'path':url.path+'?'+urlencode(params),'expires_in':60}
