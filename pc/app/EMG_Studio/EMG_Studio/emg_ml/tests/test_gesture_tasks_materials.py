"""Real default export and portable material checks. Never accesses devices."""
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[4]
STUDIO=ROOT/'EMG_Studio/EMG_Studio'
sys.path[:0]=[str(STUDIO/'backend'),str(STUDIO/'emg_ml'),str(ROOT/'scripts')]
from gesture_tasks_materials import validated_deployment,readonly_identities,require_export_inputs
from pc_gesture_tasks_default_materials import prepare_default
from pc_ml.gesture_tasks_export import commands


class MaterialsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        cls.portable=Path(cls.temp.name)/'portable'
        cls.receipt=prepare_default(cls.portable)
        original=STUDIO/'backend/artifacts/cnn_fusion_pc_trial_20261029'
        shutil.copytree(original/'export',cls.portable/'export')
        shutil.copy2(original/'prepared.npz',cls.portable/'prepared.npz')
        cls.manifest=json.loads((cls.portable/'export/manifest.json').read_text(encoding='utf-8'))

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def test_default_materials_relocate_and_validate_real_model_commands(self):
        p=self.portable
        require_export_inputs(p/'export')
        model=validated_deployment(p/'a_deployment/a_model.json',self.manifest['sha256'],self.manifest['class_names'])
        wire=list(commands(model))
        self.assertEqual(wire[0][0],0x102);self.assertEqual(wire[-1][0],0x104)
        self.assertEqual(model['transaction_crc32'],'eb3518c0')
        self.assertEqual(self.receipt['verification']['windows'],870)
        ids=readonly_identities(p/'export',self.manifest['sha256'],self.manifest['class_names'],model)
        self.assertEqual({x['model_crc32'] for x in ids},{'fc91830a','eb3518c0'})
        factory=next(x for x in ids if x['model_crc32']=='fc91830a')
        self.assertTrue(factory['proof']['all_eight_banks_equal'])
        self.assertEqual(factory['class_names'],self.manifest['class_names'])

    def test_missing_prepared_has_actionable_error_before_board_io(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'export';shutil.copytree(self.portable/'export',path)
            with self.assertRaisesRegex(ValueError,'无需重新采集或训练'):require_export_inputs(path)
            from gesture_tasks_customer import friendly_error
            try:require_export_inputs(path)
            except ValueError as error:self.assertIn('无需重新采集或训练',friendly_error(error,'上传'))

    def test_corrupt_and_wrong_source_assets_cannot_be_used(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'bad.json'
            model=json.loads((self.portable/'a_deployment/a_model.json').read_text(encoding='utf-8'))
            model['banks'][0][0]^=1;path.write_text(json.dumps(model),encoding='utf-8')
            with self.assertRaises(ValueError):validated_deployment(path,self.manifest['sha256'],self.manifest['class_names'])
            with self.assertRaises(ValueError):readonly_identities(self.portable/'export','0'*64,self.manifest['class_names'],None)
            with self.assertRaises(ValueError):readonly_identities(self.portable/'export',self.manifest['sha256'],list(reversed(self.manifest['class_names'])),None)

if __name__=='__main__':unittest.main()
