"""Prepare PC-owned default upload material; read A artifacts solely as provenance."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import struct
import sys
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
STUDIO=ROOT/'EMG_Studio/EMG_Studio'
sys.path[:0]=[str(STUDIO/'backend'),str(STUDIO/'emg_ml')]


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def factory_proof(root,pc_manifest):
    """Rebuild all factory banks from the two named quantized model exports."""
    from pc_ml.fixed_linear import FixedLinear
    from pc_ml.gesture_tasks_export import metadata,transaction_crc,validate_banks
    base=root/'artifacts/a_cnn256/final'
    factory_path=root/'artifacts/a_gesture_tasks/a_release_20261101_r4/factory_model.json'
    factory=json.loads(factory_path.read_text(encoding='utf-8'))
    head,linear=FixedLinear.load(base/'linear')
    cnn=json.loads((base/'cnn/manifest.json').read_text(encoding='utf-8'))
    names=pc_manifest['class_names'];source=pc_manifest['sha256'];k=len(names)
    for descriptor in (linear,cnn):
        if descriptor['metadata']['pc_source_sha256']!=source or descriptor['metadata']['class_names']!=names:
            raise ValueError('Factory provenance does not match the default PC class ordering')
    if linear['metadata']['sha256']!=pc_manifest['metadata']['sha256']:
        raise ValueError('Factory and PC recording provenance mismatch')
    with np.load(base/'cnn/integer.npz',allow_pickle=False) as z:a={key:z[key] for key in z.files}
    words=np.zeros((160,8),dtype=np.uint32);cfg=[0]*160;pos=0;ci=0
    for name in ('stem','conv2','conv3'):
        w=a[name+'.weights'].reshape(len(a[name+'.weights']),-1)
        for term in range(w.shape[1]):
            for oc in range(len(w)):words[pos,oc//4]|=np.uint32((int(w[oc,term])&255)<<(8*(oc%4)))
            pos+=1
        for bias,shift in zip(a[name+'.bias'],a[name+'.shifts']):cfg[ci:ci+2]=[int(bias)&0xffffffff,int(shift)];ci+=2
    cfg[144:152]=list(map(int,a['wave_shifts']));cfg[152:154]=[15,2097152]
    banks=[(head.weights_.reshape(-1).astype(np.int64)&65535).tolist(),(head.offset_.astype(np.int64)&65535).tolist(),
        sum([[int(b)&0xffffffff,(int(b)>>32)&65535] for b in head.bias_],[]),list(map(int,words.reshape(-1))),cfg,
        (a['fc.weights'].reshape(-1).astype(np.int64)&255).tolist(),
        sum([[int(b)&0xffffffff,int(s)] for b,s in zip(a['fc.bias'],a['fc.shifts'])],[]),
        [r['output']['id'] for r in linear['metadata']['gesture_profile']['classes']]]
    validate_banks(banks,k)
    crc=f'{transaction_crc(metadata(k),banks):08x}'
    if banks!=factory['banks'] or crc!=factory['transaction_crc32'] or factory['active_classes']!=k:
        raise ValueError('Factory banks differ from the model provenance')
    paths=[factory_path,base/'linear/manifest.json',base/'linear/parameters.bin',base/'cnn/manifest.json',base/'cnn/integer.npz']
    return {'model_crc32':crc,'origin':'factory_quantized_export','source_sha256':source,'class_names':names,
        'proof':{'all_eight_banks_equal':True,'transaction_crc_recalculated':True,
                 'bank_sha256':[hashlib.sha256(b''.join(struct.pack('<I',v) for v in bank)).hexdigest() for bank in banks],
                 'recording_sha256':linear['metadata']['sha256'],
                 'source_files_sha256':{p.relative_to(root).as_posix():sha(p) for p in paths}}}


def prepare_default(output):
    from pc_ml.gesture_tasks_export import export_current
    from gesture_tasks_materials import validated_deployment,require_export_inputs
    package=STUDIO/'backend/artifacts/cnn_fusion_pc_trial_20261029/export'
    require_export_inputs(package)
    manifest=json.loads((package/'manifest.json').read_text(encoding='utf-8'))
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    with np.load(package.parent/'prepared.npz',allow_pickle=False) as z:d={k:z[k] for k in z.files}
    model=export_current(package,d,output/'a_deployment')
    validated_deployment(output/'a_deployment/a_model.json',manifest['sha256'],manifest['class_names'])
    if (ROOT/'artifacts/a_gesture_tasks/a_release_20261101_r4/factory_model.json').is_file():
        identity=factory_proof(ROOT,manifest)
    else:
        # A standalone PC package reuses its previously verified identity binding.
        saved=json.loads((package.parent/'a_readonly_identities.json').read_text(encoding='utf-8'))
        if saved['source_sha256']!=manifest['sha256'] or saved['class_names']!=manifest['class_names']:
            raise ValueError('Packaged default identity source mismatch')
        identity=next(x for x in saved['identities'] if x['origin']=='factory_quantized_export')
    registry={'schema':1,'source_sha256':manifest['sha256'],'class_names':manifest['class_names'],'identities':[identity]}
    (output/'a_readonly_identities.json').write_text(json.dumps(registry,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    receipt={'schema':1,'source_sha256':manifest['sha256'],'class_names':manifest['class_names'],
        'prepared_sha256':sha(package.parent/'prepared.npz'),'upload_crc':model['transaction_crc32'],
        'readonly_factory_crc':identity['model_crc32'],'verification':model['verification'],'hardware_accessed':False}
    if 'bank_sha256' in identity['proof']:
        receipt['factory_upload_equal_banks']=[hashlib.sha256(b''.join(struct.pack('<I',v) for v in bank)).hexdigest()==expected
            for bank,expected in zip(model['banks'],identity['proof']['bank_sha256'])]
    (output/'materials_verification.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    return receipt


if __name__=='__main__':
    out=ROOT/'artifacts/pc_gesture_tasks'/datetime.now().strftime('default_materials_%Y%m%d_%H%M%S_%f')
    print(json.dumps({'output':str(out),**prepare_default(out)},ensure_ascii=False,indent=2))
