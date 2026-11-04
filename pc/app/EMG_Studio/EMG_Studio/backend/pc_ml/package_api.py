"""Shared workstation adapter for legacy CNN and the unified hybrid."""
import json
from pathlib import Path


def load_manifest(package):
    from .hybrid import IntegerHybrid
    from .quantize import load_integer
    path=Path(package)
    m=json.loads((path/'manifest.json').read_text(encoding='utf-8'))
    if m['format']=='EMG-CNN-FUSION-PC-1':
        from .cnn_fusion import load
        _,_,_,m=load(path)
    elif m['format']=='EMG-HYBRID-INT-1':
        _,m=IntegerHybrid.load(path)
        m={**m,'class_names':m['metadata']['class_names'],'payload_bytes':m['parameter_bytes']}
        m.setdefault('frontend',{'name':m['metadata']['front']})
    elif m['format']=='EMG-LINEAR-INT-1' and m.get('metadata',{}).get('runtime')=='parallel_fusion_v1':
        from .fusion import load
        _,m=load(path)
        m={**m,'class_names':m['metadata']['class_names'],'payload_bytes':m['parameter_bytes']}
    else:_,_,m=load_integer(path)
    return m


def predictor(package):
    m=load_manifest(package)
    if m['format']=='EMG-CNN-FUSION-PC-1':
        from .cnn_fusion import StreamingCNNFusionPredictor
        return StreamingCNNFusionPredictor(package)
    if m.get('metadata',{}).get('runtime')=='parallel_fusion_v1':
        from .fusion import StreamingFusionPredictor
        return StreamingFusionPredictor(package)
    if m['format']=='EMG-HYBRID-INT-1':
        from .today_live import StreamingFeaturePredictor
        return StreamingFeaturePredictor(package)
    from .replay import StreamingPredictor
    return StreamingPredictor(package)
