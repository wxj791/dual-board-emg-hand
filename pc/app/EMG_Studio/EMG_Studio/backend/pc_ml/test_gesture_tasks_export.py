"""Dynamic freshly trained tensor shapes and independent bank round-trip."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
import torch
from .cnn_fusion import RECIPE, load, export
from .cnn_fusion_field import fit
from .gesture_tasks_export import (quantize_current, validate_banks, metadata, commands,
                                  transaction_crc, canonical_statistics, deployment_statistics)
from .quantize import IntegerModel

class ExportTests(unittest.TestCase):
    def test_shared_attenuation_preserves_fusion_gate_and_scores(self):
        from types import SimpleNamespace
        h=SimpleNamespace(classes_=np.arange(3),weights_=np.zeros((3,304),dtype=np.int16),
                          bias_=np.array([-100,300,0]),offset_=np.zeros(304,dtype=np.int16),fraction_=21)
        h.weights_[:,0]=[-30000,0,30000]
        before=copy.deepcopy(h)
        with self.assertRaises(OverflowError):canonical_statistics(h)
        w,b,common,shift=deployment_statistics(h)
        self.assertEqual(shift,2);self.assertLessEqual(abs(w).max(),32767)
        np.testing.assert_array_equal(h.weights_,before.weights_)
        self.assertEqual(h.fraction_,before.fraction_)
        x=np.zeros((5,304),dtype=np.int64);x[:,0]=[-40,-2,0,2,40]
        base=x@w.T+b
        source_q23=(x@h.weights_.astype(np.int64).T+h.bias_)*4
        cnn=np.array([[24,-16,4],[-4,8,16],[0,-12,20],[20,8,-4],[-16,4,24]],dtype=np.int64)
        source_gate=np.ptp(np.sort(source_q23,axis=1)[:,-2:],axis=1)>=2097152
        target_gate=np.ptp(np.sort(base,axis=1)[:,-2:],axis=1)>=(2097152>>shift)
        np.testing.assert_array_equal(source_gate,target_gate)
        self.assertTrue(source_gate.any());self.assertFalse(source_gate.all())
        original=source_q23+(cnn<<np.where(source_gate,13,15)[:,None])
        converted=base+((cnn>>shift)<<np.where(target_gate,13,15)[:,None])
        np.testing.assert_array_equal(original,converted*4)
        np.testing.assert_array_equal(original.argmax(1),converted.argmax(1))

    def test_scaled_quantization_roundtrip_and_gate_validation(self):
        torch.set_num_threads(2);rng=np.random.default_rng(47)
        d={'x':rng.integers(-500,500,(30,304),dtype=np.int16),'y':np.repeat(np.arange(3),10),
           'group':np.arange(30),'waves':rng.integers(-100,100,(30,8,256),dtype=np.int32)}
        with patch.dict(RECIPE,epochs=1):h,m,shifts=fit(d,np.ones(30,bool))
        h.fraction_=20;h.weights_[:,0]=[-30000,0,30000]
        source_head=copy.deepcopy(h);source_state=copy.deepcopy(m.state_dict())
        _,_,_,scale=deployment_statistics(h)
        self.assertEqual(scale,3)
        banks,integer,w,b=quantize_current(h,m,shifts,d['waves'][:8])
        self.assertEqual(banks[4][152],15)
        self.assertEqual(banks[4][153],2097152>>scale)
        validate_banks(banks,3)
        unpacked=np.array([v-65536 if v>=32768 else v for v in banks[0]]).reshape(3,304)
        np.testing.assert_array_equal(unpacked,w)
        np.testing.assert_array_equal(h.weights_,source_head.weights_)
        for name,tensor in m.state_dict().items():
            torch.testing.assert_close(tensor,source_state[name],rtol=0,atol=0)
        for gate in (0,3,2097153):
            bad=copy.deepcopy(banks);bad[4][153]=gate
            with self.assertRaises(ValueError):validate_banks(bad,3)
        bad=copy.deepcopy(banks);bad[4][152]=14
        with self.assertRaises(ValueError):validate_banks(bad,3)

    def test_common_score_removal_preserves_differences_and_keeps_valid_bytes(self):
        from types import SimpleNamespace
        rng=np.random.default_rng(28)
        weights=rng.integers(-9000,9000,(10,304),dtype=np.int64)
        weights[:,6]=np.arange(10)*3000-20348
        h=SimpleNamespace(classes_=np.arange(10),weights_=weights,bias_=np.arange(10)*100,
                          offset_=np.zeros(304,dtype=np.int64),fraction_=22)
        before=weights.copy();w,b,common=canonical_statistics(h)
        self.assertTrue(any(common));self.assertLess(abs(w).max(),32768)
        np.testing.assert_array_equal(h.weights_,before)
        x=rng.integers(-32768,32768,(100,304),dtype=np.int64)
        original=x@(weights*2).T+h.bias_*2
        converted=x@w.T+b
        np.testing.assert_array_equal(original-original[:,:1],converted-converted[:,:1])
        np.testing.assert_array_equal(np.diff(np.sort(original,axis=1)[:,-2:],axis=1),
                                      np.diff(np.sort(converted,axis=1)[:,-2:],axis=1))
        cnn=rng.integers(-30000,30000,original.shape,dtype=np.int64)
        np.testing.assert_array_equal((original+cnn).argmax(1),(converted+cnn).argmax(1))
        h.fraction_=23
        w,b,common=canonical_statistics(h)
        self.assertFalse(any(common));np.testing.assert_array_equal(w,weights)
        h.weights_[0,6]=-60000;h.weights_[1,6]=60000
        with self.assertRaises(OverflowError):canonical_statistics(h)
        # Python-integer range checks also reject huge class spans before casts.
        h.weights_[0,6]=-(1<<62);h.weights_[1,6]=1<<62;h.fraction_=0
        with self.assertRaises(OverflowError):canonical_statistics(h)

    def test_new_training_2_7_14_32_and_bank_roundtrip(self):
        torch.set_num_threads(2);rng=np.random.default_rng(20261031)
        for k in (2,7,14,32):
            n=k*6
            d={'x':rng.integers(-500,500,(n,304),dtype=np.int16),'y':np.repeat(np.arange(k),6),
               'group':np.arange(n),'waves':rng.integers(-100,100,(n,8,256),dtype=np.int32)}
            with patch.dict(RECIPE,epochs=1):
                h,m,shifts=fit(d,np.ones(n,bool))
                self.assertEqual(h.weights_.shape,(k,304));self.assertEqual(tuple(m.fc.weight.shape),(k,32))
                # Canonical A Q23 is an explicit conversion with bounds, not the adaptive PC fraction.
                banks,integer,w,b=quantize_current(h,m,shifts,d['waves'][:8])
                self.assertEqual(w.shape,(k,304));self.assertEqual(list(map(len,banks)),[304*k,304,2*k,1280,160,32*k,2*k,k])
                layers={}; offset=0; ci=0
                for name,shape in (('stem',(16,8,5)),('conv2',(24,16,3)),('conv3',(32,24,3))):
                    cout,cin,kernel=shape;terms=cin*kernel
                    values=np.empty((cout,terms),dtype=np.int64)
                    for oc in range(cout):
                        for t in range(terms):
                            v=(banks[3][(offset+t)*8+oc//4]>>(8*(oc%4)))&255
                            values[oc,t]=v-256 if v>=128 else v
                    layer=copy.deepcopy(integer.layers[name]);layer['weights']=values.reshape(shape)
                    cfg=banks[4][ci:ci+2*cout];layer['bias']=np.array([v-(1<<32) if v>=1<<31 else v for v in cfg[::2]])
                    layer['shifts']=np.array(cfg[1::2]);layers[name]=layer;offset+=terms;ci+=2*cout
                fc=copy.deepcopy(integer.layers['fc'])
                fc['weights']=np.array([v-256 if v>=128 else v for v in banks[5]]).reshape(k,32)
                fc['bias']=np.array([v-(1<<32) if v>=1<<31 else v for v in banks[6][::2]])
                fc['shifts']=np.array(banks[6][1::2]);layers['fc']=fc
                reconstructed=IntegerModel('sequential',layers,integer.feature_fraction)
                q=d['waves'][:1].clip(-127,127)
                np.testing.assert_array_equal(integer.trace(q)['logits'],reconstructed.trace(q)['logits'])
                package={'active_classes':k,'banks':banks,'transaction_crc32':f'{transaction_crc(metadata(k),banks):08x}'}
                self.assertEqual(len(metadata(k)),99);self.assertEqual(list(commands(package))[-1][0],0x104)
                with tempfile.TemporaryDirectory() as root:
                    names=[f'class{i}' for i in range(k)]
                    export(root,h,m,shifts,{'class_names':names})
                    loaded,model,_,_=load(root)
                    self.assertEqual(loaded.weights_.shape,(k,304));self.assertEqual(tuple(model.fc.weight.shape),(k,32))
                bad=copy.deepcopy(banks);bad[5].pop()
                with self.assertRaises(ValueError):validate_banks(bad,k)
        with self.assertRaises(ValueError):metadata(33)
        huge=copy.deepcopy(h);huge.weights_[:]=0;huge.bias_[0]=1<<46;huge.fraction_=-16
        with self.assertRaises(OverflowError):quantize_current(huge,m,shifts,d['waves'][:8])

if __name__=='__main__':unittest.main()
