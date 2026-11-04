"""Create a new standalone PC snapshot; never overwrite releases or copy A/B BITs."""
from datetime import datetime
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import zipfile
ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'artifacts/pc_gesture_tasks'
STUDIO=ROOT/'EMG_Studio/EMG_Studio'

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--recovery-session',type=Path)
    args=parser.parse_args()
    from pc_gesture_tasks_default_materials import prepare_default
    verification=None
    for path in sorted(BASE.glob('verify_*/summary.json'),reverse=True):
        doc=json.loads(path.read_text(encoding='utf8'))
        if doc['checks'] and all(r['returncode']==0 for r in doc['checks']):verification=path.parent;break
    if verification is None:raise SystemExit('No complete passing offline verification; run pc_gesture_tasks_verify.py first.')
    materials=BASE/datetime.now().strftime('default_materials_%Y%m%d_%H%M%S_%f')
    materials_receipt=prepare_default(materials)
    name=datetime.now().strftime('pc_candidate_%Y%m%d_%H%M%S_%f')
    out=BASE/name;out.mkdir(parents=True,exist_ok=False)
    package=out/'PC_GESTURE_TASKS';package.mkdir()
    selected=[]
    for path in STUDIO.rglob('*'):
        if not path.is_file():continue
        rel=path.relative_to(STUDIO)
        if '__pycache__' in rel.parts or path.suffix=='.pyc':continue
        if rel.parts[0] not in ('emg_ml','backend'):
            if len(rel.parts)!=1 or path.suffix not in ('.py','.txt'):continue
        elif rel.parts[0]=='emg_ml':
            if len(rel.parts)>1 and rel.parts[1] in ('data','settings'):continue
            if path.name.endswith('_default.json'):continue
        elif rel.parts[0]=='backend':
            if len(rel.parts)<2:continue
            if rel.parts[1]!='pc_ml' and rel.parts[:4]!=('backend','artifacts','cnn_fusion_pc_trial_20261029','export'):continue
        selected.append((path,Path('EMG_Studio/EMG_Studio')/rel))
    for path in (ROOT/'scripts').glob('pc_gesture_tasks_*.py'):
        if path.name=='pc_gesture_tasks_ui_preview.py':continue
        selected.append((path,Path('scripts')/path.name))
    for path in (ROOT/'docs').glob('PC_GESTURE_TASKS_*.md'):selected.append((path,Path('docs')/path.name))
    for source,rel in selected:
        target=package/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
    default=package/'EMG_Studio/EMG_Studio/backend/artifacts/cnn_fusion_pc_trial_20261029'
    shutil.copy2(STUDIO/'backend/artifacts/cnn_fusion_pc_trial_20261029/prepared.npz',default/'prepared.npz')
    shutil.copytree(materials/'a_deployment',default/'a_deployment')
    shutil.copy2(materials/'a_readonly_identities.json',default/'a_readonly_identities.json')
    shutil.copy2(materials/'materials_verification.json',default/'materials_verification.json')
    # Use portable paths in the snapshot; runtime resolves them against this package.
    model_path=default/'a_deployment/a_model.json'
    model=json.loads(model_path.read_text(encoding='utf-8'))
    model['source_package']='../export';model['a_model_path']='a_model.json'
    model_path.write_text(json.dumps(model,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    from gesture_tasks_materials import validated_deployment, require_export_inputs
    require_export_inputs(default/'export')
    validated_deployment(model_path,materials_receipt['source_sha256'],materials_receipt['class_names'])
    recovery=None
    if args.recovery_session:
        source=args.recovery_session.resolve()
        recovery=package/'EMG_Studio/EMG_Studio/backend/artifacts/pc_ml/sessions/recovered_model'
        require_export_inputs(source/'export')
        manifest=json.loads((source/'export/manifest.json').read_text(encoding='utf8'))
        validated_deployment(source/'a_deployment/a_model.json',manifest['sha256'],manifest['class_names'])
        recovery.mkdir(parents=True)
        for item in ('export','validation_export','a_deployment','a_validation'):
            shutil.copytree(source/item,recovery/item)
        for item in ('prepared.npz','field_report.json','data.json'):
            shutil.copy2(source/item,recovery/item)
        for item,origin in (('a_deployment','export'),('a_validation','validation_export')):
            path=recovery/item/'a_model.json'
            doc=json.loads(path.read_text(encoding='utf8'))
            doc['source_package']='../'+origin;doc['a_model_path']='a_model.json'
            path.write_text(json.dumps(doc,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        if (source.parent/'diagnosis.json').is_file():
            dest=package/'evidence/model_fix/diagnosis.json';dest.parent.mkdir(parents=True)
            shutil.copy2(source.parent/'diagnosis.json',dest)
    for source in verification.glob('*'):
        if source.is_file():
            dest=package/'evidence/verification'/source.name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,dest)
    if (verification/'runtime/gesture_tasks.log').is_file():
        dest=package/'evidence/verification/gesture_tasks.log'
        shutil.copy2(verification/'runtime/gesture_tasks.log',dest)
    layouts=sorted(BASE.glob('customer_layout_*/layout.json'),reverse=True)
    if layouts:
        dest=package/'evidence/verification/customer_layout.json'
        shutil.copy2(layouts[0],dest)
    vectors=sorted(BASE.glob('vectors_*/golden_vectors.json'),reverse=True)
    if vectors:
        for source in vectors[0].parent.iterdir():
            if source.is_file():
                dest=package/'evidence/vectors'/source.name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,dest)
    smoke=sorted(BASE.glob('synthetic_training_*/run/field_report.json'),reverse=True)
    if smoke:
        for rel in ('field_report.json','a_validation/verification.json','a_deployment/verification.json'):
            source=smoke[0].parent/rel
            if source.is_file():
                dest=package/'evidence/synthetic_training'/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,dest)
    compact=sorted(BASE.glob('raw43_*/a_consumer.json'),reverse=True)
    if compact:
        for rel in ('a_consumer.json','a_output_fixture.bin','training/field_report.json',
                    'training/a_validation/verification.json','training/a_deployment/verification.json'):
            source=compact[0].parent/rel
            if source.is_file():
                dest=package/'evidence/raw43'/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,dest)
    readme='''PC gesture task candidate — offline software delivery\n\nStart manually: py -3.11 EMG_Studio/EMG_Studio/start_gesture_tasks.py\nDependencies: EMG_Studio/EMG_Studio/requirements.txt\nRead docs/PC_GESTURE_TASKS_CUSTOMER_UI_20261101.md first.\nSelect training actions, set their hand responses, train, upload and verify, then explicitly enable.\nAutomatic upload after training is optional and initially disabled. It never automatically arms the hand.\nDiagnostics log: EMG_Studio/EMG_Studio/runtime/gesture_tasks.log (relative to this package).\nNo automatic serial connection, flashing or servo action at startup.\nA/B wire interfaces and firmware are unchanged. No A/B BIT is included.\nA full-rate raw collection is implemented. Existing B pose/live-feedback/range limits remain.\nVerification evidence uses mocks/synthetic data and hidden Tk windows. No hardware sign-off.\n'''
    readme += '\nRaw stream background: docs/PC_GESTURE_TASKS_RAW43_20261101.md.\nA compact raw 0x43 collection and model CRC identity query are implemented.\nExisting B pose/live-feedback/range boundaries are documented; this candidate does not change interfaces.\nOffline compact capture/training evidence and a copied synthetic A byte fixture are included.\n'
    readme += '\nDEFAULT MODEL FIX: read docs/PC_GESTURE_TASKS_A_READONLY_MATERIALS_20261101.md.\nThe prepared.npz and verified integer export are included. No retraining required.\nUse the A-only identity check to show training actions without deploying to B.\nSTART_PC_GESTURE_TASKS.cmd selects the correct working directory.\n'
    readme += '\nRECOGNITION SOURCE: docs/PC_GESTURE_TASKS_RECOGNITION_SOURCE_20261101.md.\nChoose A or PC on the real-time recognition page, then explicitly start.\nPC inference uses the current model and full-rate raw samples from A; it needs no B deployment.\nPC output is a labeled preview. Mechanical control continues on A/B.\n'
    if recovery:
        readme += f'\nRECOVERED CURRENT MODEL: use START_RECOVERED_MODEL.cmd for the preserved {len(manifest["class_names"])}-class session.\nRead docs/PC_GESTURE_TASKS_MODEL_CHECK_FIX_20261101.md and docs/PC_GESTURE_TASKS_B_SYNC_FIX_20261101.md when included.\nNo retraining, automatic upload, board connection or mechanical action is performed.\n'
    (package/'README.txt').write_text(readme,encoding='utf8')
    (package/'START_PC_GESTURE_TASKS.cmd').write_text('@echo off\ncd /d "%~dp0"\npy -3.11 EMG_Studio\\EMG_Studio\\start_gesture_tasks.py\npause\n',encoding='ascii')
    (out/'START_PC_GESTURE_TASKS.cmd').write_text('@echo off\ncd /d "%~dp0PC_GESTURE_TASKS"\npy -3.11 EMG_Studio\\EMG_Studio\\start_gesture_tasks.py\npause\n',encoding='ascii')
    if recovery:
        command='py -3.11 EMG_Studio\\EMG_Studio\\start_gesture_tasks.py --model-package EMG_Studio\\EMG_Studio\\backend\\artifacts\\pc_ml\\sessions\\recovered_model\\export\npause\n'
        (package/'START_RECOVERED_MODEL.cmd').write_text('@echo off\ncd /d "%~dp0"\n'+command,encoding='ascii')
        (out/'START_RECOVERED_MODEL.cmd').write_text('@echo off\ncd /d "%~dp0PC_GESTURE_TASKS"\n'+command,encoding='ascii')
    entries=[]
    for path in sorted(package.rglob('*')):
        if path.is_file():entries.append({'path':path.relative_to(package).as_posix(),'bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    manifest={'schema':1,'created':datetime.now().isoformat(),'files':entries,'verification_directory':verification.name,
        'source_root':str(ROOT),'hardware_verified':False,'bit_included':False,
        'scope':'PC software only; unresolved interfaces documented'}
    (package/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    archive=out/(name+'.zip')
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z:
        for path in sorted(package.rglob('*')):
            if path.is_file():z.write(path,Path(package.name)/path.relative_to(package))
    with zipfile.ZipFile(archive) as z:
        if z.testzip() is not None:raise RuntimeError('zip integrity check failed')
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    (out/'archive.sha256').write_text(digest+'  '+archive.name+'\n',encoding='ascii')
    print(json.dumps({'directory':str(out),'archive':str(archive),'sha256':digest,'files':len(entries)},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
