"""Both distribution formats must contain their declared v5 source identity."""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
def module(name,relative):
    spec=importlib.util.spec_from_file_location(name,ROOT/relative)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result

class ReleaseClosureTests(unittest.TestCase):
    def test_portable_package_contains_every_declared_scoring_source(self):
        builder=module('portable','scripts/build_portable_plugin.py')
        files=builder.source_files(ROOT)
        manifest=json.loads((ROOT/'SCORING_SOURCE_MANIFEST.json').read_text(encoding='utf-8'))
        self.assertFalse(set(manifest['files'])-set(files))

    def test_update_contains_every_new_or_changed_declared_scoring_source(self):
        builder=module('updater','scripts/assembly_update/build_update.py')
        current=json.loads((ROOT/'SCORING_SOURCE_MANIFEST.json').read_text(encoding='utf-8'))['files']
        previous=json.loads((ROOT/'SCORING_SOURCE_MANIFEST_V4.json').read_text(encoding='utf-8'))['files']
        changed={name for name,row in current.items() if row['sha256']!=previous.get(name,{}).get('sha256')}
        self.assertFalse(changed-set(builder.FILES))
        self.assertTrue({'README.md','docs/BUILD.md','docs/V5_V4_COMPARISON.md','docs/RELEASE_V5.md',
                         'scripts/assembly_update/安装与恢复.md'}<=set(builder.FILES))

if __name__=='__main__':unittest.main()
