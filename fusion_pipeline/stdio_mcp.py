"""Owned synchronous JSON-lines MCP transport; uncertain calls are never retried."""
import hashlib
import json
import os
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path


class CadCallUnknown(RuntimeError):
    completion_state = 'unknown'
    reconciliation_required = True


class StdioMCP:
    def __init__(self, command, args, *, cwd, audit_dir, env=None, audit=None,
                 timeout=195, deadline=None):
        self.audit = audit
        self.timeout = float(timeout)
        self.deadline = deadline
        self.unknown = False
        self._lock = threading.Lock()
        self._responses = queue.Queue()
        self._closed = False
        self.cleanup_evidence = []
        self.tools = {}
        folder = Path(audit_dir)
        folder.mkdir(parents=True, exist_ok=True)
        self.stderr_path = folder / ('stdio-stderr-' + uuid.uuid4().hex + '.log')
        self._stderr = self.stderr_path.open('xb')
        self.worker_record_path = folder / ('stdio-owned-workers-' + uuid.uuid4().hex + '.jsonl')
        environment = dict(os.environ if env is None else env)
        environment['CAD_BENCHMARK_WORKER_RECORD'] = str(self.worker_record_path)
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        try:
            self.process = subprocess.Popen([str(command), *map(str, args)], cwd=str(cwd), env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr, **options)
            self._reader = threading.Thread(target=self._read, daemon=True)
            self._reader.start()
            self.server = self.rpc('initialize', {'protocolVersion': '2025-03-26',
                'clientInfo': {'name': 'cad-benchmark', 'version': '1'}, 'capabilities': {}})
            if not isinstance(self.server, dict) or not isinstance(self.server.get('serverInfo'), dict):
                raise ValueError('MCP initialize returned invalid server identity')
            self.rpc('notifications/initialized', notification=True)
            self.refresh_tools()
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(32 * 1024 * 1024 + 1)
                if not line:
                    raise EOFError('stdio closed')
                if len(line) > 32 * 1024 * 1024 or not line.endswith(b'\n'):
                    raise ValueError('oversized or unterminated MCP frame')
                message = json.loads(line.decode('utf-8'))
                if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
                    raise ValueError('invalid MCP frame')
                self._responses.put(message)
        except BaseException as error:
            self._responses.put(error)

    def _event(self, event, **fields):
        if self.audit:
            self.audit({'event': event, 'cad_backend': 'solidworks', **fields})

    def rpc(self, method, params=None, *, notification=False, timeout=None):
        with self._lock:
            if self._closed or self.unknown:
                raise CadCallUnknown('CAD transport cannot continue; reconciliation required')
            budget = self.timeout if timeout is None else float(timeout)
            if self.deadline is not None:
                remaining = float(self.deadline) - time.time()
                if remaining <= 0:
                    raise TimeoutError('CAD deadline expired before request was sent')
                budget = min(budget, remaining)
            request_id = None if notification else uuid.uuid4().hex
            request = {'jsonrpc': '2.0', 'method': method}
            if request_id is not None:
                request['id'] = request_id
            if params is not None:
                request['params'] = params
            body = json.dumps(request, ensure_ascii=False, allow_nan=False).encode('utf-8') + b'\n'
            self._event('mcp_request_started', request_id=request_id, method=method,
                        request_sha256=hashlib.sha256(body).hexdigest(), timeout_s=budget)
            sent = False
            try:
                sent = True
                self.process.stdin.write(body)
                self.process.stdin.flush()
                if notification:
                    return None
                until = time.monotonic() + budget
                while True:
                    remaining = until - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('CAD MCP response deadline expired')
                    response = self._responses.get(timeout=remaining)
                    if isinstance(response, BaseException):
                        raise response
                    if 'id' not in response and 'method' in response:
                        self._event('mcp_notification', method=response['method'])
                        continue
                    if response.get('id') != request_id:
                        raise ValueError('MCP response ID mismatch')
                    if 'error' in response:
                        # A server RPC failure does not certify CAD postconditions.
                        raise ValueError('MCP RPC error')
                    if 'result' not in response:
                        raise ValueError('MCP result missing')
                    self._event('mcp_request_completed', request_id=request_id, method=method)
                    return response['result']
            except BaseException as error:
                if sent:
                    self.unknown = True
                    self._event('mcp_request_unknown', request_id=request_id, method=method,
                                error_type=type(error).__name__)
                if not isinstance(error, Exception):
                    raise
                raise CadCallUnknown('CAD MCP response is unconfirmed; reconciliation required') from None

    def refresh_tools(self):
        descriptors = []
        cursor = None
        seen = set()
        while True:
            result = self.rpc('tools/list', {'cursor': cursor} if cursor else {})
            if not isinstance(result, dict) or not isinstance(result.get('tools'), list):
                raise ValueError('MCP tools/list returned invalid schema')
            descriptors.extend(result['tools'])
            cursor = result.get('nextCursor')
            if not cursor:
                break
            if cursor in seen:
                raise ValueError('MCP tools/list cursor cycle')
            seen.add(cursor)
        for item in descriptors:
            if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not isinstance(item.get('inputSchema'), dict):
                raise ValueError('MCP tool descriptor invalid')
            if item['name'] in self.tools:
                raise ValueError('Duplicate MCP tool name')
            self.tools[item['name']] = item
        return self.tools

    def call_tool(self, name, arguments, *, timeout=None):
        return self.rpc('tools/call', {'name': name, 'arguments': arguments}, timeout=timeout)

    def close(self):
        if self._closed:
            return
        self._closed = True
        process = getattr(self, 'process', None)
        if process is not None:
            if process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                # Only this owned transport process. Never terminate SLDWORKS.exe.
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
        if os.name == 'nt' and self.worker_record_path.is_file():
            from .solidworks_helper import terminate_owned_worker
            try:
                records = [json.loads(line) for line in self.worker_record_path.read_text(encoding='utf-8').splitlines()]
                for record in records:
                    result = terminate_owned_worker(record)
                    self.cleanup_evidence.append(result)
                    if result.get('terminated') or not result.get('already_exited'):
                        self.unknown = True
            except (OSError, ValueError, TypeError) as error:
                self.unknown = True
                self.cleanup_evidence.append({'error': str(error), 'scope': 'owned_worker_record'})
            self._event('mcp_process_cleanup', evidence=self.cleanup_evidence, reconciliation_required=self.unknown)
        self._stderr.close()
