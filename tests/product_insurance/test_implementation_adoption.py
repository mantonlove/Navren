"""Changing a result manifest cannot quietly change gold or the admitted API."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from evaluation.product_insurance import adoption


class ImplementationAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='navren-pi-adoption-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.out=self.root/'acceptance';self.out.mkdir()
        for name in adoption.ORIGINAL_DIGESTS:
            target=self.root/'evaluation/product_insurance'/name;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(adoption.ROOT/'evaluation/product_insurance'/name,target)
        shutil.copytree(adoption.OUT/'frozen-original-inputs',self.out/'frozen-original-inputs')
        self.manifest=self.out/'implementation-input-adoption.json'
        shutil.copyfile(adoption.OUT/'implementation-input-adoption.json',self.manifest)
        value=json.loads(self.manifest.read_text())
        api=self.root/value['api_contract']['path'];api.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(adoption.ROOT/value['api_contract']['path'],api)
        self.addCleanup(patch.stopall)
        patch.object(adoption,'ROOT',self.root).start();patch.object(adoption,'OUT',self.out).start()

    def test_final_api_adoption_preserves_original_gold(self):
        self.assertEqual(adoption.current_adoption()['api_contract']['sha256'],adoption.ADOPTED_API)

    def test_rewriting_gold_copy_and_manifest_is_not_an_authorized_adoption(self):
        name='casebook.json';target=self.root/'evaluation/product_insurance'/name
        value=json.loads(target.read_text());value['cases'][0]['required_observations']=['weakened expectation']
        raw=json.dumps(value).encode();target.write_bytes(raw)
        (self.out/'frozen-original-inputs'/name).write_bytes(raw)
        manifest=json.loads(self.manifest.read_text());manifest['original_files'][name]=hashlib.sha256(raw).hexdigest()
        self.manifest.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError,'original_freeze_identity_changed'):adoption.current_adoption()

    def test_changing_api_and_its_manifest_requires_explicit_successor(self):
        manifest=json.loads(self.manifest.read_text());target=self.root/manifest['api_contract']['path']
        target.write_text('{"status":"pretend_pass"}')
        manifest['api_contract']['sha256']=hashlib.sha256(target.read_bytes()).hexdigest()
        manifest['api_contract']['status']='pretend_pass';self.manifest.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError,'successor_requires_new_explicit_adoption'):adoption.current_adoption()
