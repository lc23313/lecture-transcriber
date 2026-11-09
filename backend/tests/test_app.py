import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
from fastapi.testclient import TestClient

CFG={'api_key':'secret','workspace_id':'test','region':'cn-beijing'}
POLICY={'upload_host':'https://temp.oss-cn-beijing.aliyuncs.com','upload_dir':'temp','oss_access_key_id':'id','signature':'sig','policy':'p','x_oss_object_acl':'private','x_oss_forbid_overwrite':'true'}
CLIENT = app.httpx.Client

class AppTests(unittest.TestCase):
    def setUp(self):
        test_root=app.ROOT/'.test-temp'
        test_root.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=test_root)
        self.data=patch.object(app,'DATA',Path(self.tmp.name))
        self.data.start()
        app.init_db()
        self.client=TestClient(app.app)
        app.ACTIVE.clear()

    def tearDown(self):
        self.client.close()
        app.ACTIVE.clear()
        self.data.stop()
        self.tmp.cleanup()

    def upload(self,content=b'RIFF'+b'a'*200_000):
        r=self.client.post('/api/audio?name=lecture.wav',content=content)
        self.assertEqual(r.status_code,200)
        return r.json()['id']

    def test_local_import_does_not_send_audio(self):
        with patch.object(app.httpx,'Client') as remote:
            jid=self.upload()
            remote.assert_not_called()
        self.assertEqual(app.get_job(jid)['status'],'LOCAL')
        self.assertEqual((app.DATA/(jid+'.audio')).stat().st_size,200_004)

    def test_upload_limit_format_and_security(self):
        self.assertEqual(self.client.post('/api/audio?name=x.exe',content=b'x').status_code,400)
        self.assertEqual(self.client.post('/api/audio?name=x.wav',content=b'').status_code,400)
        self.assertEqual(self.client.post('/api/audio?name=x.wav',headers={'Content-Length':'1000000001'},content=b'x').status_code,413)
        self.assertEqual(self.client.post('/api/audio?name=x.wav',content=b'x',headers={'Origin':'https://evil.example'}).status_code,403)
        self.assertEqual(self.client.get('/',headers={'Host':'evil.example'}).status_code,403)

    def test_original_and_edit_versions_remain_separate(self):
        jid=self.upload()
        app.update(jid,status='SUCCEEDED',original='So basically，呃，stochastic process。')
        r=self.client.put(f'/api/jobs/{jid}/edit',json={'text':'用户修订'})
        self.assertEqual(r.status_code,200)
        self.assertIn('呃',app.get_job(jid)['original'])
        self.assertEqual(app.get_job(jid)['edited'],'用户修订')
        self.assertTrue((app.DATA/(jid+'.edits.jsonl')).exists())

    def test_config_only_requires_bailian(self):
        with patch.object(app.Path,'read_text',return_value=json.dumps(CFG)):
            self.assertEqual(app.config(),CFG)
        with patch.object(app.Path,'read_text',side_effect=OSError):
            self.assertFalse(app.configuration()['ready'])

    def test_real_multipart_stream_records_bytes_but_waits_for_confirmation(self):
        jid=self.upload()
        observed=[]
        def handler(request):
            if request.method=='GET':
                return app.httpx.Response(200,json={'data':POLICY})
            request.read()
            j=app.get_job(jid)
            observed.append((j['upload_sent'],j['upload_total'],j['upload_completed']))
            return app.httpx.Response(200)
        with CLIENT(transport=app.httpx.MockTransport(handler)) as client:
            url=app.temporary_upload(client,CFG,jid,'lecture.wav')
        self.assertEqual(observed,[(200_004,200_004,0)])
        self.assertTrue(url.startswith('oss://'))
        self.assertEqual(app.get_job(jid)['upload_completed'],1)

    def test_upload_rejection_never_marks_complete(self):
        jid=self.upload()
        def handler(request):
            if request.method=='GET':
                return app.httpx.Response(200,json={'data':POLICY})
            request.read()
            return app.httpx.Response(403)
        with CLIENT(transport=app.httpx.MockTransport(handler)) as client:
            with self.assertRaises(app.httpx.HTTPStatusError):
                app.temporary_upload(client,CFG,jid,'lecture.wav')
        self.assertEqual(app.get_job(jid)['upload_completed'],0)
        self.assertTrue((app.DATA/(jid+'.audio')).exists())

    def run_worker(self,jid,states=None,submission_timeout=False,upload_failure=False):
        events=[]
        real_update=app.update
        states=iter(states or ['PENDING','RUNNING','SUCCEEDED'])
        def record(jid,**values):
            real_update(jid,**values)
            if 'status' in values:
                events.append(values['status'])
        def handler(request):
            if request.url.path.endswith('/uploads'):
                return app.httpx.Response(200,json={'data':POLICY})
            if request.url.host=='temp.oss-cn-beijing.aliyuncs.com':
                request.read()
                return app.httpx.Response(403 if upload_failure else 200)
            if request.url.path.endswith('/transcription'):
                if submission_timeout:
                    raise app.httpx.ReadTimeout('https://private-signed-url',request=request)
                return app.httpx.Response(200,json={'output':{'task_id':'task-1'}})
            if '/tasks/' in request.url.path:
                state=next(states)
                out={'task_status':state}
                if state=='SUCCEEDED':
                    out['results']=[{'subtask_status':'SUCCEEDED','transcription_url':'https://result.example/file'}]
                if state=='SUBTASK_FAILED':
                    out={'task_status':'SUCCEEDED','results':[{'subtask_status':'FAILED'}]}
                return app.httpx.Response(200,json={'output':out})
            return app.httpx.Response(200,json={'transcripts':[{'text':'Hello，呃，世界。'}]})
        with patch.object(app.audio,'prepare',side_effect=lambda src,dst:(dst.write_bytes(src.read_bytes()) and {'tracks':1})),patch.object(app,'config',return_value=CFG),patch.object(app,'update',side_effect=record),patch.object(app.time,'sleep'),patch.object(app.httpx,'Client',side_effect=lambda **kw:CLIENT(transport=app.httpx.MockTransport(handler),**kw)):
            app.worker(jid)
        return events

    def test_worker_reports_stages_and_only_completes_after_save(self):
        jid=self.upload()
        events=self.run_worker(jid)
        self.assertEqual(events,['WAITING_UPLOAD','UPLOADING','SUBMITTING','PENDING','PENDING','RUNNING','FETCHING_RESULT'])
        job=app.get_job(jid)
        self.assertEqual(job['original'],'Hello，呃，世界。')
        self.assertTrue(job['finished']>=job['recognition_started'])
        self.assertTrue(job['last_polled'])

    def test_submission_timeout_preserves_upload_and_blocks_repeat(self):
        jid=self.upload()
        self.run_worker(jid,submission_timeout=True)
        job=app.get_job(jid)
        self.assertEqual(job['status'],'INTERRUPTED')
        self.assertEqual(job['upload_completed'],1)
        self.assertNotIn('private-signed-url',job['error'])
        self.assertEqual(self.client.post(f'/api/jobs/{jid}/start').status_code,409)

    def test_upload_failure_is_retryable_and_does_not_start_recognition(self):
        jid=self.upload()
        self.run_worker(jid,upload_failure=True)
        job=app.get_job(jid)
        self.assertEqual(job['status'],'FAILED')
        self.assertEqual(job['upload_completed'],0)
        self.assertIsNone(job['recognition_started'])
        self.assertTrue((app.DATA/(jid+'.audio')).exists())

    def test_subtask_failure_is_not_completed(self):
        jid=self.upload()
        self.run_worker(jid,states=['SUBTASK_FAILED'])
        job=app.get_job(jid)
        self.assertEqual(job['status'],'FAILED')
        self.assertIsNone(job['original'])

    def test_queued_start_is_idempotent_and_progress_survives_refresh(self):
        jid=self.upload()
        with patch.object(app,'config',return_value=CFG),patch.object(app.POOL,'submit') as submit:
            self.assertEqual(self.client.post(f'/api/jobs/{jid}/start').status_code,200)
            self.assertEqual(self.client.post(f'/api/jobs/{jid}/start').status_code,200)
            submit.assert_called_once()
        app.update(jid,upload_total=100,upload_sent=45)
        r=self.client.get(f'/api/jobs/{jid}').json()
        self.assertTrue(r['active'])
        self.assertEqual(r['upload_sent'],45)
        self.assertEqual(r['status'],'WAITING_UPLOAD')
        self.assertNotIn('raw',r)
        self.assertNotIn('object_key',r)

    def test_restart_distinguishes_upload_failure_from_uncertain_submission(self):
        upload=self.upload()
        submit=self.upload()
        app.update(upload,status='UPLOADING',upload_sent=10,upload_total=100)
        app.update(submit,status='SUBMITTING')
        with TestClient(app.app):
            self.assertEqual(app.get_job(upload)['status'],'FAILED')
            self.assertEqual(app.get_job(submit)['status'],'INTERRUPTED')
            self.assertEqual(app.get_job(upload)['upload_sent'],10)

    def test_migration_preserves_old_records(self):
        jid=self.upload()
        with app.db() as c:
            c.execute('ALTER TABLE jobs RENAME TO newer_jobs')
            c.execute('CREATE TABLE jobs AS SELECT id,name,status,created,task,object_key,error,original,edited,raw,cleanup FROM newer_jobs')
        app.init_db()
        job=app.get_job(jid)
        self.assertEqual(job['status'],'LOCAL')
        self.assertEqual(job['upload_sent'],0)
        self.assertTrue((app.DATA/(jid+'.audio')).exists())

if __name__=='__main__':
    unittest.main()
