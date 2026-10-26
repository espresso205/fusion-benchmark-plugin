"""Download packages contain verified selected models, never private annotations."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile


class ModelDownloadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / 'report'

    def slot(self, name, data):
        path = self.root / name
        path.write_bytes(data)
        return {'path': str(path), 'sha256': hashlib.sha256(data).hexdigest()}

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('src.report_downloads'), 'verified model downloads required')
        from src.report_downloads import build_model_downloads
        return build_model_downloads

    def test_zip_contains_only_original_candidates_and_downloads_preserve_bytes(self):
        sources = {'101': {'world': {'candidate': self.slot('candidate.step', b'candidate'),
                                     'reference': self.slot('gt.step', b'private gt')},
                           'scoring': {'candidate': self.slot('aligned.step', b'aligned')}}}
        result = self.api()(sources, self.output)
        row = result['cases']['101']
        for role, data in [('candidate', b'candidate'), ('reference', b'private gt'), ('aligned_candidate', b'aligned')]:
            self.assertEqual((self.output / row[role]['url']).read_bytes(), data)
        with ZipFile(self.output / result['submission_zip']['url']) as archive:
            self.assertEqual(archive.namelist(), ['101/submission.step'])
            self.assertEqual(archive.read('101/submission.step'), b'candidate')
        self.assertNotIn(str(self.root), str(result))

    def test_changed_or_missing_model_is_not_downloadable_and_does_not_hide_other_side(self):
        bad = self.slot('bad.step', b'old')
        (self.root / 'bad.step').write_bytes(b'new')
        ref = self.slot('gt.step', b'gt')
        result = self.api()({'a': {'world': {'candidate': bad, 'reference': ref}}}, self.output)
        self.assertNotIn('candidate', result['cases']['a'])
        self.assertIn('reference', result['cases']['a'])
        self.assertIsNone(result['submission_zip'])

    def test_zip_entry_names_cannot_escape_and_same_bytes_are_reused(self):
        slot = self.slot('candidate.step', b'candidate')
        result = self.api()({'../../bad': {'world': {'candidate': slot}},
                             '101': {'world': {'candidate': slot}, 'scoring': {'candidate': slot}}}, self.output)
        self.assertEqual(len(list((self.output / 'models').glob('*.step'))), 1)
        self.assertNotIn('aligned_candidate', result['cases']['101'])
        with ZipFile(self.output / result['submission_zip']['url']) as archive:
            self.assertEqual(len(archive.namelist()), 2)
            for name in archive.namelist():
                self.assertNotIn('..', name)
                self.assertFalse(name.startswith('/'))


if __name__ == '__main__':
    unittest.main()
