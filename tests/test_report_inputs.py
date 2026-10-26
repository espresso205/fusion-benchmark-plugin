"""Report attachments retain order, isolate failures and stay within their root."""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aF9sAAAAASUVORK5CYII=')
JPEG = base64.b64decode('/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD3+iiigD//2Q==')
WEBP = base64.b64decode('UklGRiQAAABXRUJQVlA4IBgAAAAwAQCdASoBAAEAAUAmJaQAA3AA/vz0AAA=')
DXF = b'0\nSECTION\n2\nENTITIES\n0\nLINE\n10\n0\n20\n0\n11\n10\n21\n10\n0\nENDSEC\n0\nEOF\n'


def small_pdf():
    result = bytearray(b'%PDF-1.4\n')
    offsets = [0]
    for number, body in enumerate([
            b'<< /Type /Catalog /Pages 2 0 R >>',
            b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
            b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 10 10] >>'], 1):
        offsets.append(len(result))
        result.extend(str(number).encode() + b' 0 obj\n' + body + b'\nendobj\n')
    start = len(result)
    result.extend(b'xref\n0 4\n0000000000 65535 f \n')
    for offset in offsets[1:]:
        result.extend(f'{offset:010d} 00000 n \n'.encode())
    result.extend(f'trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode())
    return bytes(result)


class ReportInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sources = self.root / 'private drawings'
        self.sources.mkdir()
        self.output = self.root / 'report'

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('src.report_inputs'),
                             'report input attachment adapter is required')
        from src import report_inputs
        return report_inputs

    def asset(self, path, data, label='资料', **extra):
        source = self.sources / path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(data)
        return {'path': path, 'sha256': hashlib.sha256(data).hexdigest(), 'label': label, **extra}

    def manifest(self, cases):
        path = self.sources / 'inputs.json'
        path.write_text(json.dumps({'schema_version': 'fusion-report-inputs/1', 'cases': cases}), encoding='utf-8')
        return path

    def build(self, assets, case='101'):
        return self.api().build_input_assets(self.manifest([{'case': case, 'assets': assets}]),
                                            self.output, [case])

    def test_image_pdf_dxf_and_unknown_keep_list_order_and_safe_copy_names(self):
        assets = [self.asset('views/front.png', PNG, '正视图'),
                  self.asset('drawing.pdf', small_pdf(), '工程图'),
                  self.asset('drawing.dxf', DXF, '轮廓'),
                  self.asset('notes.html', b'<script>bad()</script>', '其他资料')]
        data = self.build(assets)
        self.assertEqual(data['101']['status'], 'completed')
        items = data['101']['items']
        self.assertEqual([item['label'] for item in items], ['正视图', '工程图', '轮廓', '其他资料'])
        self.assertEqual([item['kind'] for item in items], ['image', 'pdf', 'dxf', 'download'])
        self.assertEqual([item['url'] for item in items],
                         ['inputs/0/0.png', 'inputs/0/1.pdf', 'inputs/0/2.dxf', 'inputs/0/3.bin'])
        for item, original in zip(items, [PNG, small_pdf(), DXF, b'<script>bad()</script>']):
            self.assertEqual((self.output / item['url']).read_bytes(), original)
        public = json.dumps(data)
        self.assertNotIn('private drawings', public)
        self.assertNotIn('notes.html', public)
        self.assertNotIn(assets[0]['sha256'], public)

    def test_pdf_pages_preserve_order_even_with_duplicate_labels(self):
        pages = [self.asset('page2.png', PNG, '页'), self.asset('page1.png', PNG, '页')]
        original = self.asset('drawing.pdf', small_pdf(), '图纸', previews=pages)
        item = self.build([original])['101']['items'][0]
        self.assertEqual([page['label'] for page in item['previews']], ['页', '页'])
        self.assertEqual([page['url'] for page in item['previews']], ['inputs/0/1.png', 'inputs/0/2.png'])
        self.assertTrue(all((self.output / page['url']).is_file() for page in item['previews']))

    def test_jpeg_webp_and_uppercase_extensions_are_previewable(self):
        assets = [self.asset('one.JPG', JPEG), self.asset('two.jpeg', JPEG),
                  self.asset('three.WebP', WEBP), self.asset('four.PNG', PNG)]
        result = self.build(assets)['101']
        self.assertEqual(result['status'], 'completed')
        self.assertEqual([item['kind'] for item in result['items']], ['image'] * 4)
        self.assertEqual([Path(item['url']).suffix for item in result['items']], ['.jpg', '.jpeg', '.webp', '.png'])

    def test_missing_and_wrong_hash_items_do_not_discard_valid_neighbours(self):
        wrong = self.asset('wrong.png', PNG, '校验失败')
        wrong['sha256'] = '0' * 64
        missing = {'path': 'missing.pdf', 'sha256': '1' * 64, 'label': '缺失文件'}
        valid = self.asset('valid.png', PNG, '可用图片')
        result = self.build([wrong, valid, missing])['101']
        self.assertEqual(result['status'], 'partial')
        self.assertEqual([item['status'] for item in result['items']], ['unavailable', 'completed', 'unavailable'])
        self.assertEqual([item['url'] for item in result['items']], [None, 'inputs/0/1.png', None])
        self.assertTrue(all(item['message'] for item in (result['items'][0], result['items'][2])))
        self.assertEqual(len(list(self.output.rglob('*.*'))), 1)

    def test_bad_preview_keeps_original_and_other_pages(self):
        page = self.asset('good.png', PNG, '第1页')
        bad = self.asset('bad.png', b'<html>not an image</html>', '第2页')
        item = self.build([self.asset('drawing.pdf', small_pdf(), previews=[page, bad])])['101']['items'][0]
        self.assertEqual(item['status'], 'partial')
        self.assertTrue((self.output / item['url']).is_file())
        self.assertEqual([page['status'] for page in item['previews']], ['completed', 'unavailable'])
        self.assertIsNone(item['previews'][1]['url'])

    def test_wrong_magic_is_never_published_as_image_or_pdf(self):
        assets = [self.asset('fake.png', b'<svg></svg>'),
                  self.asset('fake.jpg', PNG), self.asset('fake.webp', PNG),
                  self.asset('fake.pdf', b'<html>payload</html>')]
        result = self.build(assets)['101']
        self.assertEqual(result['status'], 'unavailable')
        self.assertTrue(all(item['url'] is None for item in result['items']))
        self.assertEqual(list(self.output.rglob('*.*')), [])

    def test_path_traversal_absolute_drive_unc_and_stream_paths_are_unavailable(self):
        outside = self.root / 'secret.png'
        outside.write_bytes(PNG)
        paths = ['../secret.png', '..\\secret.png', str(outside), '/secret.png',
                 'C:/secret.png', 'C:secret.png', '\\\\server\\secret.png', 'safe.png:secret']
        assets = [{'path': value, 'sha256': hashlib.sha256(PNG).hexdigest()} for value in paths]
        result = self.build(assets)['101']
        self.assertEqual(result['status'], 'unavailable')
        self.assertTrue(all(item['url'] is None for item in result['items']))
        self.assertNotIn('secret.png', json.dumps(result))

    def test_symlink_source_or_parent_is_rejected(self):
        target = self.sources / 'real'
        target.mkdir()
        (target / 'view.png').write_bytes(PNG)
        link = self.sources / 'linked'
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            self.skipTest(f'symlinks unavailable: {error.__class__.__name__}')
        result = self.build([{'path': 'linked/view.png', 'sha256': hashlib.sha256(PNG).hexdigest()}])['101']
        self.assertEqual(result['status'], 'unavailable')
        self.assertIsNone(result['items'][0]['url'])

    @unittest.skipUnless(os.name == 'nt', 'Windows junction behavior')
    def test_junction_parent_is_rejected(self):
        target, link = self.sources / 'real', self.sources / 'junction'
        target.mkdir()
        (target / 'view.png').write_bytes(PNG)
        run = subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], capture_output=True)
        if run.returncode:
            self.skipTest('junction creation unavailable')
        result = self.build([{'path': 'junction/view.png', 'sha256': hashlib.sha256(PNG).hexdigest()}])['101']
        self.assertEqual(result['status'], 'unavailable')
        self.assertIsNone(result['items'][0]['url'])

    def test_changed_source_between_validation_and_copy_is_not_published(self):
        asset = self.asset('source.png', PNG)
        original_digest = hashlib.file_digest
        changed = False

        def digest_then_change(stream, algorithm):
            nonlocal changed
            digest = original_digest(stream, algorithm)
            if not changed:
                changed = True
                (self.sources / 'source.png').write_bytes(PNG + b'changed')
            return digest

        with patch('hashlib.file_digest', side_effect=digest_then_change):
            result = self.build([asset])['101']
        self.assertEqual(result['status'], 'unavailable')
        self.assertIsNone(result['items'][0]['url'])
        self.assertEqual(list(self.output.rglob('*.*')), [])

    def test_empty_cases_absent_cases_and_unrequested_cases_do_not_emit_sections(self):
        one = self.asset('one.png', PNG)
        manifest = self.manifest([{'case': '101', 'assets': []}, {'case': '102', 'assets': [one]},
                                  {'case': '103', 'assets': [one]}])
        result = self.api().build_input_assets(manifest, self.output, ['103', '101', 'missing'])
        self.assertEqual(list(result), ['103'])
        self.assertEqual(result['103']['items'][0]['url'], 'inputs/0/0.png')

    def test_duplicate_case_and_malformed_manifest_fail_before_copying(self):
        asset = self.asset('one.png', PNG)
        malformed = [
            [{'case': '101', 'assets': [asset]}, {'case': '101', 'assets': [asset]}],
            [{'case': '101', 'assets': 'bad'}],
            [{'case': '101', 'assets': [{'path': 'one.png'}]}],
            [{'case': '101', 'assets': [{**asset, 'previews': [asset, None]}]}],
            [{'case': '101', 'assets': [{**asset, 'label': 12}]}],
        ]
        for cases in malformed:
            with self.subTest(cases=cases), self.assertRaises(ValueError):
                self.api().build_input_assets(self.manifest(cases), self.output, ['101'])
        self.assertFalse(self.output.exists())

    def test_unsupported_schema_and_mapping_argument_are_rejected(self):
        path = self.manifest([])
        path.write_text('{"schema_version":"future/2","cases":[]}', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.api().build_input_assets(path, self.output, ['101'])
        with self.assertRaises((TypeError, ValueError)):
            self.api().build_input_assets({'cases': []}, self.output, ['101'])

    def test_file_and_total_limits_fail_explicitly_without_partial_files(self):
        assets = [self.asset('one.txt', b'12345'), self.asset('large.txt', b'123456789'),
                  self.asset('two.txt', b'12345')]
        api = self.api()
        with patch.object(api, 'MAX_FILE_BYTES', 8), patch.object(api, 'MAX_TOTAL_BYTES', 8):
            result = self.build(assets)['101']
        self.assertEqual([item['status'] for item in result['items']], ['completed', 'unavailable', 'unavailable'])
        self.assertTrue(all(item['message'] for item in result['items'][1:]))
        self.assertEqual([file.read_bytes() for file in self.output.rglob('*') if file.is_file()], [b'12345'])

    def test_file_count_limit_rejects_manifest_instead_of_truncating(self):
        api = self.api()
        asset = self.asset('one.png', PNG)
        with patch.object(api, 'MAX_FILES', 2), self.assertRaises(ValueError):
            self.build([asset, asset, asset])
        self.assertFalse(self.output.exists())

    def test_preview_must_be_an_image_even_when_original_is_a_document(self):
        page = self.asset('page.html', b'<html>unsafe preview</html>')
        result = self.build([self.asset('drawing.dxf', DXF, previews=[page])])['101']['items'][0]
        self.assertEqual(result['status'], 'partial')
        self.assertIsNone(result['previews'][0]['url'])

    def test_import_needs_only_standard_library(self):
        self.api()
        run = subprocess.run([sys.executable, '-S', '-c',
            'import src.report_inputs; import sys; assert "PIL" not in sys.modules; assert "OCP" not in sys.modules'],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)


if __name__ == '__main__':
    unittest.main()
