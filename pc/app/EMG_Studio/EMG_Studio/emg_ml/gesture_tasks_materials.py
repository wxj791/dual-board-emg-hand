"""Validate local upload assets and known read-only A identities. No board I/O."""
import hashlib
import json
from pathlib import Path

class LocalModelMaterialsError(ValueError):
    local_model_materials=True


def validated_deployment(path, source_sha256, names):
    from pc_ml.gesture_tasks_export import commands
    path=Path(path)
    model=json.loads(path.read_text(encoding='utf-8'))
    if model.get('source_sha256')!=source_sha256 or model.get('class_names')!=names or model.get('active_classes')!=len(names):
        raise ValueError('模型上传材料与当前训练动作不一致，请重新导出')
    list(commands(model))  # Validate every bank and recalculate the transaction CRC.
    model['a_model_path']=str(path.resolve())
    return model


def readonly_identities(package, source_sha256, names, model):
    """Name bindings are local evidence, independent of any B task table."""
    candidates=[]
    if model and model.get('transaction_crc32'):
        candidates.append({'model_crc32':model['transaction_crc32'],'origin':'current_export'})
    path=Path(package).parent/'a_readonly_identities.json'
    if path.is_file():
        doc=json.loads(path.read_text(encoding='utf-8'))
        if doc.get('schema')!=1 or doc.get('source_sha256')!=source_sha256 or doc.get('class_names')!=names:
            raise ValueError('A模型核对材料与当前训练动作不一致')
        for entry in doc['identities']:
            crc=entry.get('model_crc32','')
            if len(crc)!=8 or any(c not in '0123456789abcdef' for c in crc):
                raise ValueError('A模型核对材料格式无效')
            candidates.append(entry)
    return candidates


def require_export_inputs(package):
    package=Path(package)
    for path in (package/'manifest.json',package/'parameters.bin',package.parent/'prepared.npz'):
        if not path.is_file():
            raise LocalModelMaterialsError('本地模型上传材料不完整，请使用修复后的完整候选包；无需重新采集或训练')
    manifest=json.loads((package/'manifest.json').read_text(encoding='utf-8'))
    if hashlib.sha256((package/'parameters.bin').read_bytes()).hexdigest()!=manifest['sha256']:
        raise LocalModelMaterialsError('本地模型参数校验失败，请重新解压完整候选包')
    receipt_path=package.parent/'materials_verification.json'
    if receipt_path.is_file():
        receipt=json.loads(receipt_path.read_text(encoding='utf-8'))
        if (receipt.get('source_sha256')!=manifest['sha256'] or receipt.get('class_names')!=manifest['class_names'] or
            receipt.get('prepared_sha256')!=hashlib.sha256((package.parent/'prepared.npz').read_bytes()).hexdigest()):
            raise LocalModelMaterialsError('本地模型上传材料校验失败，请重新解压完整候选包')
