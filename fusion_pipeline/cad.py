"""CAD choice is independent of CLI provider and evaluation protocol."""
import hashlib
import json
import os
from pathlib import Path


def _solidworks_connection(raw):
    raw = dict(raw or {})
    allowed = {'bundle_path', 'executor_timeout_s', 'launch_if_needed'}
    if set(raw)-allowed:
        raise ValueError('Unknown SolidWorks connection fields: ' + ', '.join(sorted(set(raw)-allowed)))
    bundle = raw.get('bundle_path') or str(Path(os.environ.get('APPDATA', str(Path.home()/'AppData/Roaming'))) /
        'com.interx.desktop/skills/bundled/solidworks')
    if not isinstance(bundle, str) or not bundle.strip():
        raise ValueError('SolidWorks bundle_path must be a nonempty path')
    timeout = raw.get('executor_timeout_s', 180)
    if type(timeout) not in (int, float) or not 10 <= timeout <= 600:
        raise ValueError('SolidWorks executor_timeout_s must be between 10 and 600')
    launch = raw.get('launch_if_needed', False)
    if type(launch) is not bool:
        raise ValueError('SolidWorks launch_if_needed must be boolean')
    return {'bundle_path': str(Path(bundle).expanduser().resolve()),
            'executor_timeout_s': float(timeout), 'launch_if_needed': launch}


def normalize_cad_config(config):
    backend = config.get('cad_backend', 'fusion')
    if backend not in {'fusion', 'solidworks'}:
        raise ValueError('Unknown cad_backend: ' + str(backend))
    explicit = config.get('cad_config')
    if explicit is not None and not isinstance(explicit, dict):
        raise ValueError('cad_config must be an object')
    if backend == 'fusion':
        if config.get('solidworks_connection') or config.get('solidworks'):
            raise ValueError('SolidWorks connection conflicts with selected Fusion backend')
        connection = config.get('fusion_connection') or config.get('mcp', {}).get('autodeskfusion', {})
        if explicit is not None and connection and explicit != connection:
            raise ValueError('Conflicting Fusion connection fields')
        normalized = dict(explicit if explicit is not None else connection)
    else:
        if config.get('execution_backend', 'unified_gateway') != 'unified_gateway':
            raise ValueError('SolidWorks requires execution_backend=unified_gateway')
        if config.get('fusion_connection') or config.get('fusion_url') or config.get('mcp', {}).get('autodeskfusion'):
            raise ValueError('Fusion connection conflicts with selected SolidWorks backend')
        connection = config.get('solidworks_connection')
        alias = config.get('solidworks')
        if alias is not None:
            if not isinstance(alias, dict) or set(alias)-{'bundle', 'launch_if_needed', 'executor_timeout_s'}:
                raise ValueError('Invalid solidworks connection object')
            alias = {('bundle_path' if k == 'bundle' else k): v for k, v in alias.items()}
            if connection is not None and _solidworks_connection(connection) != _solidworks_connection(alias):
                raise ValueError('Conflicting SolidWorks connection fields')
            connection = alias
        if connection is not None and not isinstance(connection, dict):
            raise ValueError('solidworks_connection must be an object')
        if explicit is not None and connection is not None and _solidworks_connection(explicit) != _solidworks_connection(connection):
            raise ValueError('Conflicting SolidWorks connection fields')
        normalized = _solidworks_connection(explicit if explicit is not None else connection)
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    return {'cad_backend': backend, 'cad_config': normalized, 'cad_config_sha256': digest}


def cad_capabilities(config):
    frozen = normalize_cad_config(config)
    missing = []
    if frozen['cad_backend'] == 'solidworks':
        root = Path(frozen['cad_config']['bundle_path'])
        for relative in ('runtime/python-embed/python.exe', 'scripts/launch_mcp_server.py',
                         'runtime/solidworks-plugin/src/sim_plugin_solidworks/mcp_server.py'):
            if not (root/relative).is_file():
                missing.append(relative)
    else:
        if not frozen['cad_config'].get('url'):
            missing.append('fusion_connection.url')
    return {'backend': frozen['cad_backend'], 'declared_support': True,
            'dependency_discovered': not missing, 'missing_dependencies': missing,
            'mcp_handshake': {'executed': False, 'status': 'not_tested'},
            'cad_connection': {'executed': False, 'status': 'not_tested'},
            'trusted_export': {'executed': False, 'status': 'not_tested'},
            'available': False, 'cad_config_sha256': frozen['cad_config_sha256']}


def create_adapter(config, *, workspace, audit_dir, audit=None, deadline=None):
    frozen = normalize_cad_config(config)
    if frozen['cad_backend'] == 'solidworks':
        from .solidworks import SolidWorksAdapter
        return SolidWorksAdapter(config, workspace=workspace, audit_dir=audit_dir, audit=audit, deadline=deadline)
    return FusionAdapter(config, workspace=workspace, audit_dir=audit_dir, audit=audit, deadline=deadline)


class FusionAdapter:
    backend = 'fusion'

    def __init__(self, config, *, workspace, audit_dir, audit=None, deadline=None):
        from .mcp import FusionMCP
        from .solidworks import file_sha256
        from .solidworks_helper import plain_path
        frozen = normalize_cad_config(config)
        self.workspace = plain_path(workspace, kind='directory', allow_missing=True)
        self.client = FusionMCP({'mcp': {'autodeskfusion': frozen['cad_config']}}, audit=audit, timeout=180)
        self.tools = self.client.tools
        self.identity = {'backend': self.backend, 'server': self.client.server,
            'adapter_version':'1', 'adapter_sha256':file_sha256(__file__),
            'cad_config_sha256':frozen['cad_config_sha256']}

    def connect(self, launch_if_needed=False):
        return self.probe_workspace()

    def list_tools(self):
        return self.tools

    def call_tool(self, name, arguments):
        if name not in self.tools:
            raise ValueError('Unknown Fusion tool')
        return self.client.rpc('tools/call', {'name': name, 'arguments': arguments})

    def execute(self, script, *, readonly):
        return self.client.execute(script, readonly=readonly)

    def probe_workspace(self):
        from .fusion_scripts import workspace_probe_script
        from .mcp import extract_result
        report = extract_result(self.client.execute(workspace_probe_script(), readonly=True))
        if isinstance(report.get('fusion_version'), str) and report['fusion_version']:
            self.identity['fusion_version'] = report['fusion_version']
        return report

    def submission_spec(self):
        return {'filename': 'submission.f3d', 'format': 'f3d', 'document_type': 'Design'}

    def inspect_submission(self, path, configuration=None):
        if configuration is not None:
            raise ValueError('Fusion configuration is not applicable')
        return {'path': str(Path(path).resolve()), 'configuration': None,
                'verified': False, 'inspection_method': 'independent Fusion reopen required'}

    def trusted_reopen_export(self, native, folder, identity):
        from .fusion_scripts import trusted_reopen_script
        from .mcp import extract_result
        from .solidworks import file_sha256, write_json_new
        from .native_contract import validate_native_descriptor
        from .solidworks_helper import plain_path
        source = self.workspace / native['path']
        validate_native_descriptor(native, identity=identity, path=source)
        folder = plain_path(folder, kind='new', allow_missing=True)
        folder.mkdir(parents=True, exist_ok=False)
        step, report_path = folder/'trusted_export.step', folder/'reopen_report.json'
        native_report_path = folder/'fusion-native-report.json'
        result = self.client.execute(trusted_reopen_script(str(source), str(step), str(native_report_path), native['sha256']), readonly=False)
        native_report = extract_result(result)
        if native_report.get('ok') is not True or native_report.get('status') != 'passed':
            raise ValueError('Fusion trusted export failed')
        if json.loads(native_report_path.read_text(encoding='utf-8')) != native_report or file_sha256(source) != native['sha256']:
            raise ValueError('Fusion trusted export identity mismatch')
        if native_report.get('step_export', {}).get('sha256') != file_sha256(step):
            raise ValueError('Fusion trusted STEP hash mismatch')
        if isinstance(native_report.get('fusion_version'), str) and native_report['fusion_version']:
            self.identity['fusion_version'] = native_report['fusion_version']
        report = {'schema':'cad-trusted-export/1', 'ok':True, 'status':'passed', **identity,
            'native_document':native, 'native_reopen':native_report, 'adapter_identity':self.identity,
            'native_report_sha256':file_sha256(native_report_path), 'step_export':native_report['step_export']}
        write_json_new(report_path, report)
        return {'status': 'passed', 'native_document': native, 'trusted_step': str(step),
                'trusted_step_sha256': file_sha256(step), 'trusted_report': str(report_path),
                'trusted_report_sha256': file_sha256(report_path), 'report': report}

    def close(self):
        return self.client.close()
