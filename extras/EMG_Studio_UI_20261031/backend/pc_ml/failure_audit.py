"""Diagnose short field calibration without confusing training fit with tests."""
import json
from pathlib import Path
import time

import numpy as np
import torch
from threadpoolctl import threadpool_limits

from .hybrid import IntegerHybrid
from .today_hybrid import fit,infer
from .train import metrics


def run(root=Path('artifacts/fast_field'),out=Path('artifacts/failure_audit')):
    torch.set_num_threads(2);out.mkdir(parents=True,exist_ok=True);reports=[]
    with threadpool_limits(limits=2):
        for directory in sorted(p for p in root.iterdir() if (p/'prepared.npz').exists()):
            start=time.perf_counter()
            old=json.loads((directory/'field_report.json').read_text(encoding='utf-8'))
            meta=json.loads((directory/'data.json').read_text(encoding='utf-8'))
            with np.load(directory/'prepared.npz') as z:d={k:z[k] for k in z.files}
            x,w,y,split=[d[k] for k in ['x','wave','y','split']]
            calibrated,_=IntegerHybrid.load(directory/'validation_export')
            final,_=IntegerHybrid.load(directory/'export')
            stages={}
            for name,model in [('first_part_model',calibrated),('all_data_model',final)]:
                logits=model.predict_logits(w,x)
                stages[name]={str(i):metrics(y[split==i],logits[split==i].argmax(1),7) for i in range(3)}
                stages[name]['all']=metrics(y,logits.argmax(1),7)
            # Freeze the epoch selected on validation, then refit train+val.
            # The previously reported pipeline omitted this refit before test.
            epoch=old['training']['best_epoch']
            floats,refit,_,_,nx=fit(w,x,y,split!=2,None,epoch)
            logits=refit.predict_logits(w[split==2],x[split==2])
            test=metrics(y[split==2],logits.argmax(1),7)
            dest=out/directory.name;dest.mkdir(exist_ok=True)
            refit.package(dest/'refit_export',{**meta,'best_epoch':epoch,'training_scope':'first training and validation bouts; last test excluded',
                'prospective_live_verified':False,'exploratory_retest':True})
            worst=[]
            oldtest=old['evaluation']['test']['integer']
            for c in range(7):
                cm=np.asarray(oldtest['confusion']);errors=cm[c].copy();errors[c]=0
                worst.append({'class':meta['class_names'][c],'before_recall':oldtest['recall'][c],
                    'refit_recall':test['recall'][c],'all_data_training_recall':stages['all_data_model']['all']['recall'][c],
                    'previous_main_confusion':meta['class_names'][int(errors.argmax())] if errors.max() else None})
            row={'recording':directory.name,'class_names':meta['class_names'],'selected_epoch':epoch,
                 'before_test':oldtest,'after_train_val_refit_test':test,'fit_diagnostics':stages,'classes':worst,
                 'seconds':time.perf_counter()-start,
                 'note':'Exploratory re-evaluation of previously inspected recordings; all-data model scores include its training data.'}
            (dest/'report.json').write_text(json.dumps(row,ensure_ascii=False,indent=2),encoding='utf-8')
            reports.append(row)
            (out/'reports.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({'recording':directory.name,'before_test':oldtest['accuracy'],
                'refit_test':test['accuracy'],'refit_worst':min(test['recall']),
                'all_data_fit':stages['all_data_model']['all']['accuracy'],
                'all_data_worst':min(stages['all_data_model']['all']['recall'])},ensure_ascii=False),flush=True)
    return reports


if __name__=='__main__':run()
