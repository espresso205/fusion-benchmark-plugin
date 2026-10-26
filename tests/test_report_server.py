"""Exercise the real report HTTP boundary on a temporary loopback port."""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request


class ServerTests(unittest.TestCase):
    def test_only_manifest_assets_are_served_without_directory_escape(self):
        from serve_report import make_server
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)/'报告 空格'; root.mkdir()
            (root/'index.html').write_text('中文报告',encoding='utf-8')
            (root/'unlisted.json').write_text('private',encoding='utf-8')
            (root.parent/'secret.json').write_text('secret',encoding='utf-8')
            digest=hashlib.sha256((root/'index.html').read_bytes()).hexdigest()
            (root/'report-manifest.json').write_text(json.dumps({'files':{'index.html':{'sha256':digest}}}),encoding='utf-8')
            server=make_server(root,port=0)
            thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
            base='http://127.0.0.1:'+str(server.server_port)
            try:
                with urllib.request.urlopen(base+'/index.html') as response:
                    self.assertIn('text/html',response.headers['Content-Type'])
                    self.assertEqual(response.read().decode('utf-8'),'中文报告')
                    self.assertIn("object-src 'none'",response.headers['Content-Security-Policy'])
                request = urllib.request.Request(base + '/index.html', headers={'Host': 'attacker.example:' + str(server.server_port)})
                with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(request)
                self.assertEqual(error.exception.code, 403)
                for url in ('/unlisted.json','/../secret.json','/%2e%2e/secret.json','/folder/', '/report-manifest.json/..'):
                    with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(base+url)
                    self.assertIn(error.exception.code,(403,404))
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=3)


if __name__=='__main__': unittest.main()
