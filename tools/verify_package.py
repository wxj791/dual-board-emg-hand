"""Read-only integrity, source and model checks. Standard library only."""
from pathlib import Path
import hashlib,json,sys,ast
R=Path(__file__).resolve().parents[1]
def verify():
    errors=[];m=json.loads((R/'MANIFEST.json').read_text(encoding='utf-8'))
    for row in m['files']:
        p=R/row['path']
        if not p.is_file():errors.append('missing: '+row['path']);continue
        if hashlib.sha256(p.read_bytes()).hexdigest()!=row['sha256']:errors.append('hash: '+row['path'])
    py=list(R.rglob('*.py'))
    for p in py:
        if any(x in {'.venv','__pycache__','local_verification'} for x in p.relative_to(R).parts):continue
        try:compile(p.read_bytes(),str(p),'exec')
        except (SyntaxError,UnicodeError) as e:errors.append('syntax: '+str(p.relative_to(R))+' '+str(e))
    a=R/'fpga/A_recognition/原版R4源码与备用'
    for rel,sha in json.loads((a/'sources.json').read_text(encoding='utf-8-sig')).items():
        p=a/'source'/rel.replace('\\','/')
        if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=sha:errors.append('A RTL: '+rel)
    b=R/'fpga/B_execution/基准RTL源码'
    sources=(b/'fpga_b/gesture_tasks/sources.txt').read_text(encoding='utf-8-sig').splitlines()
    for rel in sources:
        if rel.strip() and not (b/rel.strip()).is_file():errors.append('B RTL: '+rel)
    checked=0
    for p in R.rglob('manifest.json'):
        if 'local_verification' in p.parts:continue
        try:d=json.loads(p.read_text(encoding='utf-8-sig'))
        except (ValueError,UnicodeError):continue
        q=p.parent/'parameters.bin'
        if isinstance(d,dict) and q.is_file() and isinstance(d.get('sha256'),str):
            if hashlib.sha256(q.read_bytes()).hexdigest()!=d['sha256']:errors.append('model: '+str(p.relative_to(R)))
            checked+=1
    return {'passed':not errors,'manifest_files':len(m['files']),'python_files':len(py),'A_frozen_sources':len(json.loads((a/'sources.json').read_text(encoding='utf-8-sig'))),'B_sources':len(sources),'model_parameter_packages':checked,'hardware_accessed':False,'errors':errors}
if __name__=='__main__':
    result=verify();print(json.dumps(result,ensure_ascii=False,indent=2));raise SystemExit(0 if result['passed'] else 1)
