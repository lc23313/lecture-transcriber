import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app as R
from fastapi.testclient import TestClient
from services import transcripts as T,language
from services.streaming import run_message

class FullTests(unittest.TestCase):
    def setUp(self):
        root=R.ROOT/'.test-temp';root.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root);self.data=patch.object(R,'DATA',Path(self.temp.name));self.data.start();R.init_db();self.client=TestClient(R.app);R.ACTIVE.clear()
        self.jid=self.client.post('/api/audio?name=test.wav',content=b'RIFF-qa').json()['id']
    def tearDown(self):
        self.client.close();R.ACTIVE.clear();self.data.stop();self.temp.cleanup()
    def version(self):
        with R.db() as c:return T.add(c,self.jid,'filetrans','原话，呃，Brownian motion。',[{'start':0,'end':5000,'speaker':'channel-0:speaker-0','text':'原话，呃，Brownian motion。'}])
    def test_project_and_vocabulary_validation(self):
        self.assertEqual(self.client.patch('/api/jobs/'+self.jid,json={'title':'讲座','topic':'随机微积分','diarization':True}).status_code,200)
        value=self.client.put('/api/jobs/'+self.jid+'/vocabulary',json={'entries':[{'en':'Brownian motion','weight':500},{'en':'brownian motion','weight':0}],'context':'x'*500}).json()
        self.assertEqual(len(value['entries']),1);self.assertEqual(value['entries'][0]['weight'],5);self.assertEqual(len(value['context']),400)
        words,context=T.hints(value);self.assertEqual(words['Brownian motion'],5);self.assertEqual(context[0]['role'],'user')
    def test_version_edit_preserves_time_speaker_and_parent(self):
        rid=self.version()
        out=self.client.post(f'/api/jobs/{self.jid}/versions',json={'parent':rid,'text':'ignored','segments':[{'text':'用户修改'}]}).json()
        values=self.client.get(f'/api/jobs/{self.jid}/versions').json();new=next(v for v in values if v['id']==out['id']);old=next(v for v in values if v['id']==rid)
        self.assertIn('呃',old['text']);self.assertEqual(new['parent'],rid)
        segment=json.loads(new['segments'])[0];self.assertEqual(segment['start'],0);self.assertEqual(segment['speaker'],'channel-0:speaker-0');self.assertEqual(segment['text'],'用户修改')
    def test_export_optional_labels_and_diff(self):
        rid=self.version();self.client.patch('/api/jobs/'+self.jid,json={'speakers':{'channel-0:speaker-0':'主讲人'}})
        response=self.client.get(f'/api/jobs/{self.jid}/export?version={rid}&timestamps=true&speakers=true&format=md')
        self.assertIn('00:00:05',response.text);self.assertIn('主讲人',response.text);self.assertIn('Brownian motion',response.text)
        n=self.client.post(f'/api/jobs/{self.jid}/versions',json={'parent':rid,'text':'新稿'}).json()['id']
        self.assertTrue(self.client.get(f'/api/jobs/{self.jid}/diff?before={rid}&after={n}').json()['lines'])
    def test_backup_restore_new_project_and_versions(self):
        rid=self.version();backup=self.client.get(f'/api/jobs/{self.jid}/backup')
        with zipfile.ZipFile(io.BytesIO(backup.content)) as z:self.assertEqual(set(z.namelist()),{'project.json','audio.bin'})
        response=self.client.post('/api/restore',content=backup.content);self.assertEqual(response.status_code,200)
        restored=response.json()['id'];self.assertNotEqual(restored,self.jid)
        versions=self.client.get(f'/api/jobs/{restored}/versions').json();self.assertEqual(versions[0]['text'],'原话，呃，Brownian motion。');self.assertNotEqual(versions[0]['id'],rid)
        self.assertEqual((R.DATA/(restored+'.audio')).read_bytes(),b'RIFF-qa')
        self.assertEqual(self.client.post('/api/restore',content=b'bad').status_code,400)
    def test_record_prepare_idempotent_and_audio_upload(self):
        body={'id':'qa-record','name':'record.webm','title':'现场课','topic':'量化'}
        first=self.client.post('/api/recordings/prepare',json=body).json()['id'];second=self.client.post('/api/recordings/prepare',json=body).json()['id'];self.assertEqual(first,second)
        self.assertEqual(self.client.put('/api/jobs/'+first+'/recorded-audio',content=b'original').status_code,200)
        self.client.put('/api/jobs/'+first+'/recorded-audio',content=b'overwrite')
        self.assertEqual((R.DATA/(first+'.audio')).read_bytes(),b'original')
    def test_tidy_only_on_explicit_post_and_new_version(self):
        rid=self.version()
        with patch.object(language,'tidy',return_value='整理文本') as tidy,patch.object(R,'config',return_value={'llm_model':'qa'}),patch.object(R.POOL,'submit') as pool:
            self.client.get('/api/jobs');tidy.assert_not_called()
            response=self.client.post(f'/api/jobs/{self.jid}/tidy',json={'version':rid,'mode':'light'});self.assertEqual(response.status_code,200)
            pool.call_args.args[0]()
        versions=self.client.get(f'/api/jobs/{self.jid}/versions').json();self.assertEqual(versions[0]['kind'],'tidy_light');self.assertEqual(versions[0]['parent'],rid)
        self.assertTrue(any('呃' in v['text'] for v in versions))
    def test_tidy_does_not_silently_truncate_long_source(self):
        source='原始内容\n'*3000;seen=[]
        def call(cfg,system,text):
            data=json.loads(text)['source'];seen.append(data);return {'text':data}
        with patch.object(language,'complete',side_effect=call):language.tidy({},source,'deep')
        self.assertEqual(''.join(seen),source)
    def test_tidy_rejects_english_translation_or_number_invention(self):
        with patch.object(language,'complete',return_value={'text':'布朗运动，利率4%'}):
            with self.assertRaises(ValueError):language.tidy({},'Brownian motion，利率3%','deep')
    def test_streaming_exact_protocol_and_no_speaker_claim(self):
        value=run_message('uuid',{'entries':[{'en':'Ito','weight':3}],'context':'金融'})
        self.assertEqual(value['payload']['model'],'qwen-audio-3.1-asr-flash-streaming')
        self.assertEqual(value['payload']['parameters']['format'],'pcm');self.assertEqual(value['payload']['parameters']['sample_rate'],16000)
        self.assertNotIn('diarization_enabled',value['payload']['parameters'])
        self.assertEqual(value['payload']['parameters']['vocabulary']['Ito'],3)
    def test_paragraph_merge_respects_speakers_and_silence(self):
        raw={'transcripts':[{'sentences':[{'begin_time':0,'end_time':1000,'speaker_id':0,'text':'呃，'},{'begin_time':1000,'end_time':2000,'speaker_id':0,'text':'Monte Carlo。'},{'begin_time':2100,'end_time':3000,'speaker_id':1,'text':'问题？'}]}]}
        parts=T.paragraphs(raw);self.assertEqual(len(parts),2);self.assertEqual(parts[0]['text'],'呃，Monte Carlo。');self.assertNotEqual(parts[0]['speaker'],parts[1]['speaker'])
        self.assertNotEqual(T.paragraphs(raw,namespace='task-a')[0]['speaker'],T.paragraphs(raw,namespace='task-b')[0]['speaker'])
    def test_private_auth_covers_api_and_cross_origin(self):
        with patch.object(R,'PUBLIC_ORIGIN','https://lecture.example'),patch.object(R,'ACCESS_TOKEN','t'*32):
            self.assertEqual(self.client.get('/api/jobs').status_code,401)
            r=self.client.post('/api/login',json={'token':'t'*32});self.assertEqual(r.status_code,200);self.assertIn('Secure',r.headers['set-cookie']);self.assertIn('HttpOnly',r.headers['set-cookie'])
            self.assertEqual(self.client.get('/api/jobs',headers={'Cookie':'lecture_access='+'t'*32}).status_code,200)
            self.assertEqual(self.client.post('/api/recordings/prepare',json={},headers={'Origin':'https://evil.example'}).status_code,403)
    def test_no_automatic_user_edit_overwrite_on_new_result(self):
        rid=self.version();self.client.post(f'/api/jobs/{self.jid}/versions',json={'parent':rid,'text':'重要编辑'})
        with R.db() as c:T.add(c,self.jid,'filetrans','重新识别结果')
        texts=[v['text'] for v in self.client.get(f'/api/jobs/{self.jid}/versions').json()]
        self.assertIn('重要编辑',texts);self.assertIn('原话，呃，Brownian motion。',texts)
    def test_recognition_again_stays_in_project_and_keeps_edits(self):
        rid=self.version();R.update(self.jid,status='SUCCEEDED',task='old-task',edited='重要编辑')
        with patch.object(R,'config',return_value={'ready':True}),patch.object(R.POOL,'submit'):
            r=self.client.post(f'/api/jobs/{self.jid}/recognize-again')
        self.assertEqual(r.json()['id'],self.jid);self.assertEqual(R.get_job(self.jid)['edited'],'重要编辑');self.assertIsNone(R.get_job(self.jid)['task'])
        self.assertEqual(self.client.get(f'/api/jobs/{self.jid}/versions').json()[0]['id'],rid)
    def test_topic_change_invalidates_unrelated_vocabulary(self):
        R.update(self.jid,topic='金融',vocab=json.dumps({'entries':[{'en':'Ito'}]}))
        self.client.patch('/api/jobs/'+self.jid,json={'topic':'生物'})
        self.assertEqual(json.loads(R.get_job(self.jid)['vocab']),{})
    def test_intentionally_empty_vocabulary_is_not_regenerated(self):
        R.update(self.jid,topic='金融',vocab='{}',vocab_status='已保存；新任务使用')
        with patch.object(language,'complete') as model:R.ensure_vocabulary(self.jid);model.assert_not_called()
    def test_crash_saved_live_sentences_recover_once(self):
        with R.db() as c:c.execute('INSERT INTO live_sentences VALUES(?,?,?,?)',(self.jid,'crashed-session',1,json.dumps({'start':0,'end':1000,'speaker':None,'text':'实时原话'})))
        with TestClient(R.app):pass
        with TestClient(R.app):pass
        with R.db() as c:self.assertEqual(c.execute("SELECT count(*) FROM revisions WHERE job=? AND kind='realtime'",(self.jid,)).fetchone()[0],1)
    def test_pages_origin_bearer_auth_and_scoped_download_ticket(self):
        with patch.object(R,'PUBLIC_ORIGIN','https://backend.example'),patch.object(R,'FRONTEND_ORIGIN','https://lc23313.github.io'),patch.object(R,'ACCESS_TOKEN','t'*32):
            headers={'Origin':'https://lc23313.github.io','Authorization':'Bearer '+'t'*32}
            self.assertEqual(self.client.get('/api/jobs',headers=headers).status_code,200)
            self.assertEqual(self.client.get('/api/jobs',headers={'Origin':'https://lc23313.github.io'}).status_code,401)
            r=self.client.post('/api/download-ticket',json={'path':f'/api/jobs/{self.jid}/audio'},headers=headers)
            self.assertEqual(r.status_code,200);path=r.json()['path']
            self.assertEqual(self.client.get(path).content,b'RIFF-qa')
            self.assertEqual(self.client.get(path.replace('/audio?','/backup?')).status_code,401)
            self.assertEqual(self.client.get(path+'&extra=1').status_code,401)
            self.assertEqual(self.client.post('/api/download-ticket',json={'path':'https://evil.example'},headers=headers).status_code,400)
    def test_pages_websocket_protocol_auth_token(self):
        from types import SimpleNamespace
        import base64
        token='test-private-access'
        encoded=base64.urlsafe_b64encode(token.encode()).decode().rstrip('=')
        connection=SimpleNamespace(headers={'sec-websocket-protocol':'lecture-v1, lecture-auth.'+encoded},cookies={})
        self.assertEqual(R.bridge.access_token(connection),token)

if __name__=='__main__':unittest.main()
