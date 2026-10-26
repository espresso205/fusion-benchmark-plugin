"""Serve only manifest-listed report files on the loopback interface."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
import webbrowser

CSP="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; worker-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"


class ReportHandler(SimpleHTTPRequestHandler):
    extensions_map={**SimpleHTTPRequestHandler.extensions_map,'.js':'text/javascript; charset=utf-8',
                    '.mjs':'text/javascript; charset=utf-8','.json':'application/json; charset=utf-8',
                    '.html':'text/html; charset=utf-8','.svg':'image/svg+xml','.css':'text/css; charset=utf-8'}

    def __init__(self,*args,root,allowed,**kwargs):
        self.root=root; self.allowed=allowed
        super().__init__(*args,directory=str(root),**kwargs)

    def send_head(self):
        port = self.server.server_port
        hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        if port == 80:
            hosts.update(('127.0.0.1', 'localhost'))
        if (self.headers.get('Host') or '').lower() not in hosts:
            self.send_error(403, 'Only the local report address is allowed'); return None
        name=unquote(urlsplit(self.path).path)
        if name=='/': name='/index.html'
        relative=name.lstrip('/')
        parts=PurePosixPath(relative).parts
        if (not relative or '\\' in relative or ':' in relative or '\x00' in relative
                or '..' in parts or relative not in self.allowed):
            self.send_error(404,'Resource is not in this report'); return None
        path=self.root.joinpath(*parts)
        try:
            if not path.resolve().is_relative_to(self.root) or not path.is_file():
                self.send_error(404,'Resource unavailable'); return None
            current=path
            while current!=self.root:
                if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
                    self.send_error(403,'Linked files are not served'); return None
                current=current.parent
        except (OSError,ValueError):
            self.send_error(404,'Resource unavailable'); return None
        self.path='/'+relative
        return super().send_head()

    def end_headers(self):
        self.send_header('Content-Security-Policy',CSP)
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Cache-Control','no-cache')
        super().end_headers()

    def log_message(self,*args): pass


def make_server(root, *, port=0):
    root=Path(root).resolve(strict=True)
    manifest=json.loads((root/'report-manifest.json').read_text(encoding='utf-8'))
    files=manifest.get('files',{})
    if not isinstance(files,dict) or 'index.html' not in files:
        raise ValueError('report manifest must contain index.html')
    allowed=frozenset(files)|{'report-manifest.json'}
    server=ThreadingHTTPServer(('127.0.0.1',port),partial(ReportHandler,root=root,allowed=allowed))
    server.daemon_threads=True
    return server


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument('--port',type=int,default=0)
    parser.add_argument('--open',action='store_true',help='Open the local report in your default browser')
    args=parser.parse_args(argv)
    with make_server(args.root,port=args.port) as server:
        url=f'http://127.0.0.1:{server.server_port}/index.html'
        print(url,flush=True)
        if args.open: webbrowser.open(url)
        try: server.serve_forever()
        except KeyboardInterrupt: pass
    return 0


if __name__=='__main__': raise SystemExit(main())
