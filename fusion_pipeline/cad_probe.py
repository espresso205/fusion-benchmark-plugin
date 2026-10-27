"""Explicit CAD discovery/connect diagnostics; management preflight stays static."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from src.io_utils import strict_json
from .cad import create_adapter, normalize_cad_config, cad_capabilities
from .stdio_mcp import CadCallUnknown


def desktop_lease_path():
    return Path(tempfile.gettempdir()) / 'fusion-benchmark-desktop.lock'


def run_probe(config, output, *, stage='discovery', launch_cad=False):
    if stage not in {'discovery', 'connect'} or type(launch_cad) is not bool:
        raise ValueError('invalid explicit CAD probe stage or launch policy')
    if stage == 'discovery' and launch_cad:
        raise ValueError('discovery cannot launch CAD')
    config = strict_json(Path(config)) if not isinstance(config, dict) else dict(config)
    frozen = normalize_cad_config(config)
    output = Path(output).absolute()
    output.mkdir(parents=True, exist_ok=False)
    workspace = output / 'workspace'
    workspace.mkdir()
    lease = desktop_lease_path()
    owner = {'pid': os.getpid(), 'run': str(output), 'attempt_id': 'probe-' + uuid.uuid4().hex,
             'cad_backend': frozen['cad_backend'], 'started_at': datetime.now(timezone.utc).isoformat()}
    report = {'schema': 'cad-probe/1', 'stage': stage, 'status': 'running',
              'cad_backend': frozen['cad_backend'], 'cad_config_sha256': frozen['cad_config_sha256'],
              'launch_if_needed': launch_cad, 'capabilities': cad_capabilities(config),
              'mcp_handshake': {'executed': False, 'status': 'not_tested'},
              'cad_connection': {'executed': False, 'status': 'not_tested'},
              'trusted_export': {'executed': False, 'status': 'not_tested'},
              'reconciliation_required': False, 'model_called': False}
    acquired, adapter, connect_started, interrupted = False, None, False, None
    def audit(event):
        with (output/'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'at': time.time(), **event}, ensure_ascii=False) + '\n')
    try:
        if stage == 'connect':
            with lease.open('x', encoding='utf-8') as stream:
                acquired = True
                json.dump(owner, stream)
                stream.flush()
                os.fsync(stream.fileno())
            report['lease_owner'] = owner
        report['mcp_handshake'] = {'executed': True, 'status': 'running'}
        adapter = create_adapter(config, workspace=workspace, audit_dir=output/'cad', audit=audit)
        catalog = adapter.list_tools()
        with (output/'tools.json').open('x', encoding='utf-8') as stream:
            json.dump(catalog, stream, ensure_ascii=False, indent=2)
        report['mcp_handshake'] = {'executed': True, 'status': 'passed', 'tool_count': len(catalog)}
        if stage == 'connect':
            connect_started = True
            report['cad_connection'] = {'executed': True, 'status': 'running'}
            probe = adapter.connect(launch_if_needed=launch_cad)
            report['cad_connection'] = {'executed': True, 'status': 'passed', 'workspace': probe}
            report['modeling_session_empty'] = probe.get('documents') == []
        report.update(status='passed', adapter_identity=adapter.identity)
    except BaseException as error:
        if not isinstance(error, Exception):
            interrupted = error
        unknown = connect_started and (interrupted is not None or isinstance(error, CadCallUnknown) or
                  getattr(getattr(adapter, 'client', None), 'unknown', False))
        report.update(status='failed', error_type=type(error).__name__, error=str(error),
                      reconciliation_required=bool(unknown))
        if connect_started:
            report['cad_connection'].update(status='unknown' if unknown else 'failed')
        elif report['mcp_handshake']['executed']:
            report['mcp_handshake'].update(status='failed')
    finally:
        if adapter is not None:
            try:
                adapter.close()
                client = getattr(adapter, 'client', None)
                report['transport_cleanup'] = getattr(client, 'cleanup_evidence', [])
                if getattr(client, 'unknown', False):
                    report.update(status='failed', reconciliation_required=bool(connect_started),
                                  cleanup_error='Owned CAD transport or worker completion is unknown')
            except Exception as error:
                report.update(status='failed', reconciliation_required=bool(connect_started),
                              cleanup_error=type(error).__name__ + ': ' + str(error))
        # Persist known CAD outcome before releasing its owned desktop lease.
        report['ended_at_utc'] = datetime.now(timezone.utc).isoformat()
        encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
        with (output/'probe.json').open('x', encoding='utf-8') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if acquired and not report['reconciliation_required']:
            try:
                if strict_json(lease) != owner:
                    raise ValueError('desktop lease owner changed; preserve lock')
                lease.unlink()
            except Exception as error:
                report.update(status='failed', reconciliation_required=True,
                              lease_error=type(error).__name__ + ': ' + str(error))
                with (output/'lease-release-failure.json').open('x', encoding='utf-8') as stream:
                    json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    if interrupted is not None:
        raise interrupted
    return report
