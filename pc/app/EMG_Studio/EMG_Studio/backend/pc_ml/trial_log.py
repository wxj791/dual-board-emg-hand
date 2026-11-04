"""Incremental live-trial raw data and prediction capture, without true labels."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import shutil
import time

import numpy as np


class TrialLog:
    def __init__(self,directory,package,manifest):
        self.directory=Path(directory);self.directory.mkdir(parents=True,exist_ok=False)
        self.manifest=manifest;self.package=str(Path(package).resolve())
        snapshot=self.directory/'package';snapshot.mkdir()
        for filename in ('manifest.json','parameters.bin'):
            shutil.copy2(Path(package)/filename,snapshot/filename)
        if (Path(package)/'hand_mapping.json').is_file():
            shutil.copy2(Path(package)/'hand_mapping.json',snapshot/'hand_mapping.json')
        copied=json.loads((snapshot/'manifest.json').read_text(encoding='utf-8'))
        if copied['sha256']!=manifest['sha256'] or hashlib.sha256((snapshot/'parameters.bin').read_bytes()).hexdigest()!=manifest['sha256']:
            raise ValueError('试用参数快照校验失败，请重新加载模型')
        self.raw=(self.directory/'raw_physical.f32').open('wb')
        self.blocks=(self.directory/'blocks.jsonl').open('w',encoding='utf-8')
        self.predictions=(self.directory/'predictions.jsonl').open('w',encoding='utf-8')
        self.count=0;self.prediction_count=0;self.resets=[];self.segment_start=0
        self.started=datetime.now(timezone.utc).isoformat()
        self.closed=False
        self._metadata(False,None)

    def _metadata(self,complete,error):
        data={'purpose':'live_trial_unlabelled','started_utc':self.started,
            'package':self.package,'package_sha256':self.manifest['sha256'],
            'package_snapshot':'package',
            'class_names':self.manifest['class_names'],'gesture_profile':self.manifest.get('gesture_profile'),
            'samples':self.count,'prediction_windows':self.prediction_count,'reset_samples':self.resets,
            'finalized':complete,'error':error,'raw_dtype':'<f4','raw_channels':8,
            'channel_order':'physical recorder order; do not remap',
            'true_gesture_labels_available':False,'prediction_is_not_ground_truth':True,
            'timestamp_note':'Block host wall/monotonic receive times; not per-sample ADC timestamps.'}
        (self.directory/'session.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')

    def append(self,raw,reset):
        raw=np.asarray(raw,dtype='<f4')
        if raw.ndim!=2 or raw.shape[1]!=8 or not np.isfinite(raw).all():raise ValueError('试用日志输入无效')
        if reset:self.segment_start=self.count;self.resets.append(self.count)
        self.raw.write(raw.tobytes());self.raw.flush()
        block={'sample_start':self.count,'samples':len(raw),'reset':bool(reset),
               'host_time':time.time(),'host_monotonic':time.monotonic()}
        self.blocks.write(json.dumps(block)+'\n');self.blocks.flush()
        self.count+=len(raw)

    def append_predictions(self,rows):
        for row in rows:
            value={**row,'global_sample_end':self.segment_start+row['sample_end']}
            self.predictions.write(json.dumps(value,ensure_ascii=False)+'\n');self.prediction_count+=1
        self.predictions.flush()

    def close(self,error=None):
        if self.closed:return
        self.closed=True
        self.raw.close();self.blocks.close();self.predictions.close()
        if self.count:
            raw=np.memmap(self.directory/'raw_physical.f32',dtype='<f4',mode='r',shape=(self.count,8))
            # Deliberately unlabeled; predicted gestures must never become targets.
            np.savez_compressed(self.directory/'live_raw.npz',emg=raw,
                labels=np.full(self.count,-1,dtype=np.int16),reset_samples=np.asarray(self.resets),
                class_names=np.asarray(self.manifest['class_names']),
                package_sha256=self.manifest['sha256'],true_labels_available=False)
        self._metadata(True,error)
