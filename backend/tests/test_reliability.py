import hashlib
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
import app as R
from services import transcripts as T,language,audio


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.data=patch.object(R,'DATA',Path(self.tmp.name));self.data.start();R.init_db();R.ACTIVE.clear();R.CANCEL.clear();self.client=TestClient(R.app)
        self.jid=self.client.post('/api/recordings/prepare',json={'id':'reliable-record','name':'record.wav'}).json()['id']
    def tearDown(self):
        self.client.close();self.data.stop();self.tmp.cleanup()
    def send(self,seq,content):
        return self.client.put(f'/api/jobs/{self.jid}/recorded-upload/{seq}',content=content,headers={'X-Chunk-SHA256':hashlib.sha256(content).hexdigest()})
    def test_resumable_upload_hash_retry_and_completion(self):
        self.assertEqual(self.send(0,b'first').status_code,200)
        self.assertEqual(self.send(0,b'first').status_code,200)
        self.assertEqual(self.send(0,b'changed').status_code,409)
        self.assertEqual(len(self.client.get(f'/api/jobs/{self.jid}/recorded-upload').json()['parts']),1)
        self.assertEqual(self.client.post(f'/api/jobs/{self.jid}/recorded-upload/complete',json={'count':2,'bytes':9}).status_code,409)
        self.send(1,b'last')
        self.assertEqual(self.client.post(f'/api/jobs/{self.jid}/recorded-upload/complete',json={'count':2,'bytes':9}).status_code,200)
        self.assertEqual((R.DATA/(self.jid+'.audio')).read_bytes(),b'firstlast')
        self.assertFalse(list(R.DATA.glob('*.part*')))
    def test_bad_upload_hash_is_not_persisted(self):
        r=self.client.put(f'/api/jobs/{self.jid}/recorded-upload/0',content=b'data',headers={'X-Chunk-SHA256':'0'*64})
        self.assertEqual(r.status_code,400);self.assertEqual(self.client.get(f'/api/jobs/{self.jid}/recorded-upload').json()['parts'],[])
    def test_corrupt_persisted_part_blocks_completion(self):
        self.send(0,b'original');(R.DATA/(self.jid+'.part0')).write_bytes(b'corrupt')
        self.assertEqual(self.client.post(f'/api/jobs/{self.jid}/recorded-upload/complete',json={'count':1,'bytes':8}).status_code,409)
        self.assertFalse((R.DATA/(self.jid+'.audio')).exists())
    def test_missing_timestamps_and_english_boundaries_are_preserved(self):
        raw={'transcripts':[{'sentences':[{'begin_time':0,'end_time':1,'text':'Hello'},{'begin_time':1,'end_time':2,'text':'world'},{'text':'unknown time'}]}]}
        parts=T.paragraphs(raw);self.assertEqual(parts[0]['text'],'Hello world');self.assertEqual(parts[1]['start'],None)
        out=T.export_text({'text':'Hello world unknown time','segments':json.dumps(parts)}, {},True)
        self.assertIn('unknown time',out);self.assertIn('时间未知',out)
    def test_aggregate_only_and_incomplete_sentences_are_preserved(self):
        parts=T.paragraphs({'transcripts':[{'text':'complete text','sentences':[{'text':'partial','begin_time':0,'end_time':1}]}]})
        self.assertEqual(parts[0]['text'],'complete text');self.assertIsNone(parts[0]['start'])
    def test_custom_llm_cannot_receive_asr_key(self):
        cfg={'api_key':'never-send','workspace_id':'w','region':'cn-beijing','llm_base_url':'https://other.example/v1'}
        with patch.object(language.httpx,'Client') as client:
            with self.assertRaises(ValueError):language.complete(cfg,'s','u')
            client.assert_not_called()
    def test_tidy_blocks_sign_unit_and_multiplicity_changes(self):
        for original,result in [('rate 3%','rate -3%'),('3 kg','3 mg'),('3 and 3','3'),('3%','3')]:
            with self.subTest(original=original),patch.object(language,'complete',return_value={'text':result}):
                with self.assertRaises(ValueError):language.tidy({},original,'deep')
    def test_backup_removed_after_response_and_restore_run_evidence(self):
        (R.DATA/(self.jid+'.audio')).write_bytes(b'audio')
        with R.db() as c:
            rid=T.add(c,self.jid,'filetrans','words')
            c.execute('INSERT INTO model_runs VALUES(?,?,?,?,?,?,?,?)',('run',self.jid,rid,'task','model','{"vocabulary_snapshot":{}}','{"transcripts":[]}',1))
        r=self.client.get(f'/api/jobs/{self.jid}/backup');self.assertFalse(list(R.DATA.glob('*.backup.zip')))
        restored=self.client.post('/api/restore',content=r.content).json()['id']
        records=self.client.get(f'/api/jobs/{restored}/runs').json();self.assertEqual(records[0]['model'],'model')
        self.assertNotEqual(records[0]['revision'],rid)
    def test_delete_requires_confirmation_and_preserves_other_project(self):
        other=self.client.post('/api/recordings/prepare',json={'id':'other'}).json()['id']
        self.send(0,b'part')
        self.assertEqual(self.client.request('DELETE',f'/api/jobs/{self.jid}',json={}).status_code,400)
        self.assertEqual(self.client.request('DELETE',f'/api/jobs/{self.jid}',json={'confirm':self.jid}).status_code,200)
        self.assertFalse(list(R.DATA.glob(self.jid+'.*')));self.assertEqual(R.get_job(other)['id'],other)
    def test_versions_summary_excludes_body(self):
        with R.db() as c:rid=T.add(c,self.jid,'filetrans','body')
        r=self.client.get(f'/api/jobs/{self.jid}/versions?summary=true').json();self.assertNotIn('text',r[0])
        self.assertEqual(self.client.get(f'/api/jobs/{self.jid}/versions/{rid}').json()['text'],'body')
    def test_audio_validates_stereo_and_preserves_source(self):
        src=R.DATA/'stereo.wav';dest=R.DATA/'mono.flac'
        import math,struct
        with wave.open(str(src),'wb') as w:
            w.setparams((2,2,16000,0,'NONE','not compressed'))
            w.writeframes(b''.join(struct.pack('<hh',int(10000*math.sin(i*.1)),int(8000*math.sin(i*.2))) for i in range(16000)))
        before=src.read_bytes();info=audio.prepare(src,dest)
        self.assertEqual(info['tracks'],1);self.assertEqual(src.read_bytes(),before);self.assertGreater(dest.stat().st_size,100)
        decoded=audio.command(['-i',str(dest),'-f','s16le','-ac','1','-ar','16000','-'],30)
        samples=struct.unpack('<'+'h'*(len(decoded.stdout)//2),decoded.stdout)
        for frequency in (.1,.2):
            amplitude=abs(sum(x*math.sin(i*frequency) for i,x in enumerate(samples))*2/len(samples))
            self.assertGreater(amplitude,1000,'both original channels must be audible')
        bad=R.DATA/'bad.mp3';bad.write_bytes(b'not audio')
        with self.assertRaises(ValueError):audio.prepare(bad,R.DATA/'bad.flac')
    def test_job_pagination(self):
        for i in range(5):self.client.post('/api/recordings/prepare',json={'id':f'p{i}'})
        one=self.client.get('/api/jobs?limit=2&offset=0').json();two=self.client.get('/api/jobs?limit=2&offset=2').json()
        self.assertEqual(len(one),2);self.assertFalse(set(j['id'] for j in one)&set(j['id'] for j in two))
        self.assertNotIn('raw',one[0]);self.assertNotIn('original',one[0])
