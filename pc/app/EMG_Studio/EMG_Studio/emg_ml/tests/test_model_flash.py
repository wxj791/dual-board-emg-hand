"""Offline Flash protocol tests; no serial port or hardware access."""
import json
from pathlib import Path
import struct
import sys
import unittest

APP=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(APP/'emg_ml'),str(APP/'backend')]
from gesture_tasks_flash import model_image,save_model,flash_state,ModelFlashError
from gesture_tasks_protocol import Parser
from pc_ml.gesture_tasks_export import commands

ROOT=APP.parents[2]/'a_model_flash_candidate_20261102/sim'

class Fake:
    def __init__(self,model):
        self.model=model;self.epoch=1;self.calls=[];self.cap=True;self.available=True
        self.committed=False;self.image=bytearray();self.fail=None;self.bad_saved=False
    def request(self,target,op,data=b'',**kwargs):
        self.calls.append((target,op,data))
        assert target==1
        if self.fail==op:raise TimeoutError('injected failure')
        if op==1:return json.dumps({'flash':self.cap}).encode()
        if op==2:return json.dumps(dict(valid=1,loading=0,model_crc32_valid=1,
            generation_hex='0001',active_hex=f'{self.model["active_classes"]:04x}',
            model_crc32_hex=self.model['transaction_crc32'])).encode()
        if op==0x10b:return struct.pack('<BB3sBHIIII',1,int(self.available)|(2 if self.committed else 0),
            bytes.fromhex('684015'),0,self.model['active_classes'],
            int(self.model['transaction_crc32'],16)^int(self.bad_saved),1,len(self.image),61440)
        if op==0x109:self.expected=struct.unpack('<IIIHH',data);self.image.clear()
        elif op==0x10a:
            offset=struct.unpack_from('<I',data)[0];assert offset==len(self.image)
            assert 0<len(data)-4<=256 and offset%256==0
            self.image.extend(data[4:])
        elif op==0x106:
            import zlib
            assert len(self.image)==self.expected[0] and zlib.crc32(self.image)==self.expected[1]
            self.committed=True
        else:raise AssertionError(op)
        return b''

class FlashTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.model=json.loads((ROOT/'model11/model.json').read_text())
    def test_image_uses_unchanged_export_commands(self):
        for k in (11,32):
            m=json.loads((ROOT/f'model{k}/model.json').read_text());image=model_image(m)
            parser=Parser();frames=parser.feed(image);self.assertEqual(parser.errors,0)
            decoded=[]
            for kind,payload in frames:
                ver,target,rid,op,size=struct.unpack_from('<BBHHH',payload)
                self.assertEqual((kind,ver,target),(0x50,1,1));self.assertEqual(size,len(payload)-8)
                decoded.append((op,payload[8:]))
            self.assertEqual(decoded,list(commands(m)));self.assertLessEqual(len(image),61440)
    def test_verified_save_has_no_b_commands(self):
        fake=Fake(self.model);result=save_model(fake,self.model)
        self.assertTrue(result['saved']);self.assertEqual(result['model_crc32'],self.model['transaction_crc32'])
        self.assertEqual(bytes(fake.image),model_image(self.model))
        self.assertEqual([op for _,op,_ in fake.calls].count(0x106),1)
    def test_old_firmware_does_not_receive_flash_commands(self):
        fake=Fake(self.model);fake.cap=False
        with self.assertRaises(ModelFlashError):save_model(fake,self.model)
        self.assertEqual([op for _,op,_ in fake.calls],[1])
    def test_missing_chip_does_not_erase(self):
        fake=Fake(self.model);fake.available=False
        with self.assertRaises(ModelFlashError):save_model(fake,self.model)
        self.assertNotIn(0x109,[op for _,op,_ in fake.calls])
    def test_timeout_does_not_commit_or_retry(self):
        fake=Fake(self.model);fake.fail=0x10a
        with self.assertRaises(TimeoutError):save_model(fake,self.model)
        self.assertFalse(fake.committed);self.assertEqual([op for _,op,_ in fake.calls].count(0x10a),1)
    def test_wrong_saved_crc_does_not_claim_success(self):
        fake=Fake(self.model);fake.bad_saved=True
        with self.assertRaises(ModelFlashError):save_model(fake,self.model)
    def test_status_rejects_malformed(self):
        for raw in (b'',b'\0'*24,b'\xff'*24):
            with self.assertRaises(ModelFlashError):flash_state(raw)

if __name__=='__main__':unittest.main()
