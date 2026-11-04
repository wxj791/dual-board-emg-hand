"""Fixed discriminative-head control: same hardware and honest fit/test reports."""
import json
import argparse
from pathlib import Path
import time

import numpy as np
import torch
from threadpoolctl import threadpool_limits

from .hybrid import IntegerHybrid
from .robust_linear import pipeline,integer_head
from .train import metrics
from .fast_field import stable_metrics


def main():
    p=argparse.ArgumentParser();p.add_argument('--only');args=p.parse_args()
    torch.set_num_threads(2);out=Path('artifacts/calibration_logistic');out.mkdir(exist_ok=True);rows=[]
    with threadpool_limits(limits=2):
        for directory in sorted(Path('artifacts/fast_field').iterdir()):
            if not (directory/'prepared.npz').exists():continue
            if args.only and directory.name!=args.only:continue
            start=time.perf_counter()
            with np.load(directory/'prepared.npz') as z:d={k:z[k] for k in z.files}
            x,w,y,split=[d[k] for k in ['x','wave','y','split']]
            base,meta=IntegerHybrid.load(directory/'export')
            config={'kind':'logistic','C':100}
            fitted=pipeline(config).fit(x[split!=2],y[split!=2])
            tested=integer_head(base,fitted,x[split!=2])
            scores=tested.predict_logits(w[split==2],x[split==2])
            test=metrics(y[split==2],scores.argmax(1),7)
            test_float=metrics(y[split==2],fitted.predict(x[split==2]),7)
            stable=stable_metrics(y[split==2],scores,d['group'][split==2])
            dest=out/directory.name;dest.mkdir(exist_ok=True)
            tested.package(dest/'validation_export',{**meta['metadata'],'head_recipe':config,'trained_all_bouts':False,'exploratory_retest':True})
            fitted=pipeline(config).fit(x,y);final=integer_head(base,fitted,x)
            training=metrics(y,final.predict_logits(w,x).argmax(1),7)
            descriptor=final.package(dest/'export',{**meta['metadata'],'head_recipe':config,'trained_all_bouts':True,'exploratory_retest':True})
            profile=meta['metadata'].get('gesture_profile')
            descriptor.update(class_names=meta['metadata']['class_names'],gesture_profile=profile,
                action_mapping=[c['output'] for c in profile['classes']] if profile else [],
                payload_bytes=descriptor['parameter_bytes'],frontend={'name':'clean'},decision={'margin':0,'persistence':3})
            (dest/'export/manifest.json').write_text(json.dumps(descriptor,ensure_ascii=False,indent=2),encoding='utf-8')
            from scripts.hybrid_memory import export_memory
            export_memory(dest/'export',dest/'export/fpga')
            np.savez_compressed(dest/'export/golden.npz',wave=w[:4],features=x[:4],logits=final.predict_logits(w[:4],x[:4]))
            row={'recording':directory.name,'head':config,'test':test,'all_data_fit':training,
                'class_names':meta['metadata']['class_names'],'seconds':time.perf_counter()-start,'cnn_active':False,
                'limitation':'Training fit is not online accuracy; fixed recipe tested on previously inspected held-out bouts, exploratory only.'}
            (dest/'report.json').write_text(json.dumps(row,ensure_ascii=False,indent=2),encoding='utf-8')
            acceptance={'passed_offline':test['accuracy']>=.95 and min(test['recall'])>=.9 and test['rest_false_activation']<=.01,
                'weak_classes':[meta['metadata']['class_names'][c] for c,r in enumerate(test['recall']) if r<.9],
                'prospective_online_acceptance':False,'exploratory_retest':True}
            field={'recording':meta['metadata']['path'],'preset':'clean128_discriminative_C100_candidate',
                'training':{'manual_parameters_required':False,'classifier':'balanced multinomial logistic'},
                'evaluation':{'test':{'float':test_float,'integer':test,'stable':stable}},
                'calibration_fit':training,'acceptance':acceptance,'parameters':descriptor,
                'timing_seconds':{'total':row['seconds']},'limitations':[row['limitation']]}
            (dest/'field_report.json').write_text(json.dumps(field,ensure_ascii=False,indent=2),encoding='utf-8')
            rows.append(row)
            print(directory.name,'test',round(test['accuracy'],4),'testworst',round(min(test['recall']),4),
                  'fit',round(training['accuracy'],4),'fitworst',round(min(training['recall']),4),'restfit',round(training['rest_false_activation'],4),flush=True)
    # A targeted rerun must retain reports for the other recordings.
    reports=[json.loads(path.read_text(encoding='utf-8')) for path in sorted(out.glob('*/report.json'))]
    (out/'reports.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':main()
