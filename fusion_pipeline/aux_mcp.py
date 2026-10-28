"""Attempt-owned, read-only stdio auxiliary MCP services.

Gateway owns authorization and the shared controlled-call budget. This module
never retries calls and never classifies auxiliary failures as unknown CAD writes.
The deadline is an absolute time.monotonic() value.
"""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
import uuid

from .adapters import spawn_owned_process


class AuxiliaryMCPError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _strict_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def constant(value):
        raise ValueError('nonfinite JSON')
    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


def _check_schema_references(schema):
    pending = [schema]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key in ('$ref', '$dynamicRef'):
                if key in value and (not isinstance(value[key], str) or not value[key].startswith('#')):
                    raise AuxiliaryMCPError('auxiliary_invalid_schema')
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)


class _Service:
    def __init__(self, manager, server_id, definition):
        self.manager, self.server_id, self.definition = manager, server_id, definition
        self.responses = queue.Queue(maxsize=8)
        self.reader_error = None
        self.closed = False
        self.process = None
        self.schema = None
        self.lock = threading.Lock()
        self.receipt = None
        self.writer = None

    def start(self):
        definition = self.definition
        self.manager._verify_sources(definition)
        environment = {key: self.manager.inherited_env[key]
                       for key in definition['env_allowlist'] if key in self.manager.inherited_env}
        # Windows needs its system directory, not the caller's entire environment.
        if os.name == 'nt' and 'SystemRoot' in os.environ:
            environment.setdefault('SystemRoot', os.environ['SystemRoot'])
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        self.process = spawn_owned_process([definition['command'], *definition['args']],
            cwd=str(self.manager.audit_dir), env=environment, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
        self.manager._event('auxiliary_started', server_id=self.server_id, pid=self.process.pid)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self.reader.start()
        self.stderr_reader.start()

    def _read(self):
        limit = self.definition['max_response_bytes']
        try:
            while not self.closed:
                line = self.process.stdout.readline(limit + 1)
                if not line:
                    raise AuxiliaryMCPError('auxiliary_process_exited')
                if len(line) > limit or not line.endswith(b'\n'):
                    raise AuxiliaryMCPError('auxiliary_response_too_large')
                try:
                    message = _strict_json(line.decode('utf-8'))
                except (ValueError, UnicodeError):
                    raise AuxiliaryMCPError('auxiliary_invalid_json') from None
                if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
                    raise AuxiliaryMCPError('auxiliary_invalid_response')
                try:
                    self.responses.put_nowait((message, len(line)))
                except queue.Full:
                    raise AuxiliaryMCPError('auxiliary_response_too_large') from None
        except (OSError, ValueError, AuxiliaryMCPError) as error:
            self.reader_error = error if isinstance(error, AuxiliaryMCPError) else AuxiliaryMCPError('auxiliary_process_exited')

    def _read_stderr(self):
        # Retain bounded diagnostic bytes only; stderr cannot fill a disk or pipe.
        remaining = self.definition['max_response_bytes']
        path = self.manager.audit_dir / ('auxiliary-' + self.server_id + '-' + uuid.uuid4().hex + '.stderr')
        try:
            with path.open('xb') as stream:
                while not self.closed:
                    chunk = self.process.stderr.read(4096)
                    if not chunk:
                        break
                    if remaining:
                        stream.write(chunk[:remaining])
                        remaining = max(0, remaining - len(chunk))
        except (OSError, ValueError):
            pass

    def rpc(self, method, params=None, notification=False):
        with self.lock:
            self.manager._check()
            if self.closed:
                raise AuxiliaryMCPError('auxiliary_transport_closed')
            request_id = uuid.uuid4().hex
            request = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
            if not notification:
                request['id'] = request_id
            body = _json(request) + b'\n'
            if len(body) > min(self.definition['max_response_bytes'], 1024 * 1024):
                raise AuxiliaryMCPError('auxiliary_request_too_large')
            until = min(self.manager.deadline, time.monotonic() + self.definition['timeout_s'])
            self.manager._event('auxiliary_request_started', server_id=self.server_id, method=method,
                                 request_sha256=hashlib.sha256(body).hexdigest())
            if method == 'tools/call':
                self.manager._calls[self.server_id] += 1
            sent, write_errors = threading.Event(), []
            def write():
                try:
                    self.process.stdin.write(body)
                    self.process.stdin.flush()
                except (OSError, ValueError) as error:
                    write_errors.append(error)
                finally:
                    sent.set()
            self.writer = threading.Thread(target=write, daemon=True)
            self.writer.start()
            while not sent.wait(timeout=0.02):
                self.manager._check()
                if time.monotonic() >= until:
                    raise AuxiliaryMCPError('auxiliary_timeout')
            self.manager._check()
            if write_errors:
                raise AuxiliaryMCPError('auxiliary_process_exited')
            if notification:
                return None
            received = 0
            while True:
                self.manager._check()
                remaining = until - time.monotonic()
                if remaining <= 0:
                    raise AuxiliaryMCPError('auxiliary_timeout')
                try:
                    message, size = self.responses.get(timeout=min(0.05, remaining))
                except queue.Empty:
                    self.manager._check()
                    if self.reader_error:
                        raise self.reader_error
                    continue
                received += size
                if received > self.definition['max_response_bytes']:
                    raise AuxiliaryMCPError('auxiliary_response_too_large')
                if 'id' not in message and isinstance(message.get('method'), str):
                    if message['method'] == 'notifications/tools/list_changed':
                        raise AuxiliaryMCPError('auxiliary_schema_drift')
                    continue
                if message.get('id') != request_id or ('result' in message) == ('error' in message):
                    raise AuxiliaryMCPError('auxiliary_invalid_response')
                if 'error' in message:
                    raise AuxiliaryMCPError('auxiliary_rpc_error')
                self.manager._event('auxiliary_request_completed', server_id=self.server_id, method=method,
                    response_sha256=hashlib.sha256(_json(message)).hexdigest(), response_bytes=received)
                return message['result']

    def list_tools(self):
        descriptors, cursors, cursor, total = {}, set(), None, 0
        while True:
            result = self.rpc('tools/list', {'cursor': cursor} if cursor else {})
            total += len(_json(result))
            if total > self.definition['max_response_bytes']:
                raise AuxiliaryMCPError('auxiliary_response_too_large')
            if not isinstance(result, dict) or not isinstance(result.get('tools'), list):
                raise AuxiliaryMCPError('auxiliary_invalid_schema')
            for item in result['tools']:
                if (not isinstance(item, dict) or not isinstance(item.get('name'), str)
                        or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', item['name'])
                        or not isinstance(item.get('inputSchema'), dict)
                        or item['inputSchema'].get('type') != 'object'
                        or not isinstance(item.get('description', ''), str)):
                    raise AuxiliaryMCPError('auxiliary_invalid_schema')
                name = item['name']
                if name in descriptors:
                    raise AuxiliaryMCPError('auxiliary_tool_name_conflict')
                annotations = item.get('annotations', {})
                if (not isinstance(annotations, dict) or annotations.get('readOnlyHint') is False
                        or annotations.get('destructiveHint') is True):
                    raise AuxiliaryMCPError('auxiliary_read_only_required')
                _check_schema_references(item['inputSchema'])
                if 'outputSchema' in item:
                    if not isinstance(item['outputSchema'], dict):
                        raise AuxiliaryMCPError('auxiliary_invalid_schema')
                    _check_schema_references(item['outputSchema'])
                descriptors[name] = item
            cursor = result.get('nextCursor')
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise AuxiliaryMCPError('auxiliary_invalid_schema')
            cursors.add(cursor)
        if not set(self.definition['allowed_tools']).issubset(descriptors):
            raise AuxiliaryMCPError('auxiliary_allowed_tool_missing')
        return descriptors

    def close(self):
        self.closed = True
        if self.receipt and self.receipt['verified_stopped']:
            return copy.deepcopy(self.receipt)
        receipt = {'server_id': self.server_id, 'pid': self.process.pid if self.process else None,
                   'verified_stopped': self.process is None, 'remaining_processes': None}
        if self.process:
            try:
                tree = self.process._benchmark_owned_tree
                tree.stop()
                self.process.wait(timeout=5)
                receipt.update(verified_stopped=tree.verified_stopped, remaining_processes=tree.remaining_processes,
                               exit_code=self.process.returncode, ownership='windows_job' if os.name == 'nt' else 'process_group')
                if self.writer:
                    self.writer.join(timeout=1)
                for stream in [self.process.stdin, self.process.stdout, self.process.stderr]:
                    try:
                        stream.close()
                    except (OSError, ValueError):
                        pass
                for thread in [self.reader, self.stderr_reader]:
                    thread.join(timeout=1)
            except Exception as error:
                receipt['error_type'] = type(error).__name__
        else:
            receipt['remaining_processes'] = 0
        self.receipt = receipt
        return copy.deepcopy(receipt)


class AuxiliaryMCPManager:
    """Discover once, freeze tool schemas, and forward only approved names.

    Construction does not start a process. discover() and call() are serialized;
    cancel()/revoke()/close() can interrupt an in-flight request from another thread.
    """
    def __init__(self, manifest, audit_dir, deadline, inherited_env):
        self.audit_dir = Path(audit_dir)
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.deadline = float(deadline)
        except (ValueError, TypeError, OverflowError):
            raise AuxiliaryMCPError('auxiliary_invalid_deadline') from None
        if isinstance(deadline, bool) or not math.isfinite(self.deadline):
            raise AuxiliaryMCPError('auxiliary_invalid_deadline')
        self.inherited_env = dict(inherited_env)
        self._manifest = copy.deepcopy(manifest)
        self._services, self._surface = {}, {}
        self._identity, self._calls = {}, {}
        self._audit_failures, self._cleanup_receipt = [], None
        self._revoked = threading.Event()
        self._closed, self._discovered = False, False
        self._lock, self._lifecycle, self._audit_lock = threading.RLock(), threading.RLock(), threading.Lock()
        self._definitions = {}
        ids = manifest.get('ordered_enabled_mcp_ids', [])
        definitions = manifest.get('requested_server_definitions', {})
        if not isinstance(ids, list) or not isinstance(definitions, dict) or len(ids) != len(set(ids)):
            raise AuxiliaryMCPError('auxiliary_invalid_manifest')
        for server_id in ids:
            if server_id == 'cad_native':
                continue
            if not isinstance(server_id, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,47}', server_id):
                raise AuxiliaryMCPError('auxiliary_invalid_manifest')
            definition = copy.deepcopy(definitions.get(server_id))
            self._validate_definition(definition)
            self._definitions[server_id] = definition

    @staticmethod
    def _validate_definition(value):
        if (not isinstance(value, dict) or value.get('kind') != 'stdio'
                or value.get('side_effect_policy') != 'read_only'
                or not isinstance(value.get('command'), str) or not Path(value['command']).is_absolute()
                or not isinstance(value.get('args'), list) or not all(isinstance(v, str) for v in value['args'])
                or not isinstance(value.get('allowed_tools'), list) or not value['allowed_tools']
                or not all(isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', v) for v in value['allowed_tools'])
                or len(value['allowed_tools']) != len(set(value['allowed_tools']))
                or not isinstance(value.get('env_allowlist'), list)
                or not all(isinstance(v, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', v) for v in value['env_allowlist'])):
            raise AuxiliaryMCPError('auxiliary_invalid_definition')
        timeout, bound = value.get('timeout_s'), value.get('max_response_bytes')
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout)
                or timeout <= 0 or isinstance(bound, bool) or not isinstance(bound, int) or not 256 <= bound <= 32 * 1024 * 1024):
            raise AuxiliaryMCPError('auxiliary_invalid_definition')
        if (not isinstance(value.get('source_files'), list) or not value['source_files']
                or not isinstance(value.get('source_file_hashes'), list)
                or len(value['source_files']) != len(value['source_file_hashes'])
                or not isinstance(value.get('command_sha256'), str)):
            raise AuxiliaryMCPError('auxiliary_source_identity_required')

    @staticmethod
    def _verify_sources(definition):
        try:
            if _digest(definition['command']) != definition['command_sha256']:
                raise AuxiliaryMCPError('auxiliary_source_drift')
            for original, record in zip(definition['source_files'], definition['source_file_hashes']):
                if (not isinstance(record, dict) or record.get('path') != original
                        or _digest(original) != record.get('sha256')):
                    raise AuxiliaryMCPError('auxiliary_source_drift')
        except (OSError, ValueError, TypeError):
            raise AuxiliaryMCPError('auxiliary_source_drift') from None

    def _check(self):
        if self._revoked.is_set():
            raise AuxiliaryMCPError('auxiliary_revoked')
        if self._closed:
            raise AuxiliaryMCPError('auxiliary_closed')
        if time.monotonic() >= self.deadline:
            raise AuxiliaryMCPError('auxiliary_deadline_expired')

    def _event(self, event, **fields):
        body = _json({'event': event, 'monotonic': time.monotonic(), **fields}) + b'\n'
        with self._audit_lock:
            with (self.audit_dir / 'auxiliary-mcp.jsonl').open('ab') as stream:
                stream.write(body)

    def _lifecycle_event(self, event, **fields):
        # Cleanup may never depend on a writable audit sink. Retain bounded,
        # non-sensitive failure evidence in memory for the caller's receipt.
        try:
            self._event(event, **fields)
        except Exception as error:
            failure = {'event': event, 'error_type': type(error).__name__}
            if failure not in self._audit_failures:
                self._audit_failures.append(failure)

    def discover(self):
        with self._lock:
            self._check()
            if self._discovered:
                return copy.deepcopy(self._surface)
            try:
                for server_id, definition in self._definitions.items():
                    with self._lifecycle:
                        self._check()
                        service = _Service(self, server_id, definition)
                        self._services[server_id] = service
                        service.start()
                    identity = service.rpc('initialize', {'protocolVersion': '2025-03-26',
                        'clientInfo': {'name': 'benchmark-auxiliary-gateway', 'version': '1'}, 'capabilities': {}})
                    if (not isinstance(identity, dict) or identity.get('protocolVersion') != '2025-03-26'
                            or not isinstance(identity.get('serverInfo'), dict)
                            or not isinstance(identity['serverInfo'].get('name'), str)
                            or not isinstance(identity['serverInfo'].get('version'), str)):
                        raise AuxiliaryMCPError('auxiliary_invalid_identity')
                    self._identity[server_id] = identity['serverInfo']
                    service.rpc('notifications/initialized', notification=True)
                    service.schema = service.list_tools()
                    self._calls[server_id] = 0
                    for name in definition['allowed_tools']:
                        public_name = 'aux__' + server_id + '__' + name
                        if public_name in self._surface:
                            raise AuxiliaryMCPError('auxiliary_tool_name_conflict')
                        descriptor = copy.deepcopy(service.schema[name])
                        descriptor.update(name=public_name, server_id=server_id, upstream_name=name)
                        self._surface[public_name] = descriptor
                self._discovered = True
                self._event('auxiliary_discovery_completed', tools=self._surface,
                            schema_sha256=hashlib.sha256(_json(self._surface)).hexdigest())
                return copy.deepcopy(self._surface)
            except Exception as error:
                self._fail(error)

    def _fail(self, error):
        code = error.code if isinstance(error, AuxiliaryMCPError) else 'auxiliary_transport_failure'
        try:
            self._lifecycle_event('auxiliary_failure', code=code, error_type=type(error).__name__)
        finally:
            receipt = self.close()
        failure = AuxiliaryMCPError(code)
        failure.cleanup_receipt = receipt
        failure.audit_failure_evidence = copy.deepcopy(self._audit_failures)
        raise failure from None

    def call(self, public_tool_name, arguments):
        with self._lock:
            self._check()
            # Unknown names cannot trigger service startup.
            possible = {'aux__' + sid + '__' + tool for sid, definition in self._definitions.items()
                        for tool in definition['allowed_tools']}
            if not isinstance(public_tool_name, str) or public_tool_name not in possible:
                raise AuxiliaryMCPError('auxiliary_tool_not_enabled')
            if not isinstance(arguments, dict):
                raise AuxiliaryMCPError('auxiliary_invalid_arguments')
            try:
                _json(arguments)
            except (ValueError, TypeError, RecursionError):
                raise AuxiliaryMCPError('auxiliary_invalid_arguments') from None
            self.discover()
            descriptor = self._surface[public_tool_name]
            service = self._services[descriptor['server_id']]
            try:
                if _json(service.list_tools()) != _json(service.schema):
                    raise AuxiliaryMCPError('auxiliary_schema_drift')
                self._check()
                result = service.rpc('tools/call', {'name': descriptor['upstream_name'], 'arguments': arguments})
                if not isinstance(result, dict) or not isinstance(result.get('content'), list):
                    raise AuxiliaryMCPError('auxiliary_invalid_result')
                self._event('auxiliary_call_completed', server_id=service.server_id, tool_name=public_tool_name)
                return result
            except Exception as error:
                self._fail(error)

    def evidence(self):
        return copy.deepcopy({'observed_server_identity': self._identity,
            'candidate_visible_tool_schema_hash': hashlib.sha256(_json(self._surface)).hexdigest() if self._discovered else None,
            'observed_tool_surface': self._surface, 'per_server_tool_calls': self._calls,
            'audit_failure_evidence': self._audit_failures, 'cleanup_evidence': self._cleanup_receipt,
            'exposure_verification_status': 'discovered' if self._discovered else 'not_verified'})

    def revoke(self, reason='revoked'):
        self._revoked.set()
        try:
            self._lifecycle_event('auxiliary_revoked', reason=str(reason))
        finally:
            receipt = self.close()
        return receipt

    def cancel(self):
        return self.revoke('cancelled')

    def close(self):
        with self._lifecycle:
            self._closed = True
            receipts = [service.close() for service in self._services.values()]
            receipt = {'verified_stopped': all(item['verified_stopped'] for item in receipts), 'servers': receipts}
            self._lifecycle_event('auxiliary_cleanup', **receipt)
            receipt['audit_status'] = 'failed' if self._audit_failures else 'recorded'
            receipt['audit_failure_evidence'] = copy.deepcopy(self._audit_failures)
            self._cleanup_receipt = copy.deepcopy(receipt)
            return receipt
