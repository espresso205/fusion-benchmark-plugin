"""SolidWorks native adapter and independent, fail-closed STEP publication."""
import hashlib
import json
import math
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from .stdio_mcp import StdioMCP, CadCallUnknown
from .solidworks_helper import PRIVATE_TOOLS, bounded_worker_timeout, verify_instance, plain_path

ADAPTER_VERSION = '1'
MAXIMUM_TOOL_TIMEOUT_S = 600
GEOMETRY_TOLERANCES = {'bbox_absolute_mm': 0.01, 'volume_relative': 1e-5, 'volume_absolute_mm3': 1e-5}
# Reviewed vendor branches: named-sketch lookup returns before InsertSketch,
# ClearSelection2 or FeatureExtrusion2; tools.py maps swSelectionError to 104.
# Other selection errors can follow edit/selection changes and are not admitted.
_SKETCH_LOOKUP_SOURCES = {
    '_vendor/solidworks_mcp/automation/features.py': '16fef5556cbcbdb6832c5e44aa5f0faabb155770f648cf5c38d04b65ddab6d78',
    'tools.py': '6f34cfe3763cdca745a41460fa4846a07cace89815e141adf1a2e1a05434c68a'}
# This exact profile validation raises before selection or native feature edits.
# An arbitrary invalid_input result is not evidence that a mutation never began.
_THREAD_PROFILE_SOURCES = {**_SKETCH_LOOKUP_SOURCES,
    '_vendor/solidworks_mcp/automation/thread_feature.py': '9635f52c1712b9d590befad593406f251e94883cab07b0b4d9ce342a92ca3b40'}


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_new(path, value):
    with Path(path).open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n')


def parse_tool_result(result):
    if not isinstance(result, dict):
        raise ValueError('MCP tool result is not an object')
    candidates = []
    if isinstance(result.get('structuredContent'), dict):
        candidates.append(result['structuredContent'])
    content = result.get('content', [])
    if not isinstance(content, list) or any(not isinstance(block, dict) for block in content):
        raise ValueError('MCP content is malformed')
    for block in content:
        if block.get('type') == 'text':
            try:
                item = json.loads(block.get('text', ''))
            except (TypeError, ValueError):
                continue
            if isinstance(item, dict):
                candidates.append(item)
    if not candidates or any(item != candidates[0] for item in candidates):
        raise ValueError('MCP result lacks one unambiguous business result')
    payload = candidates[0]
    if result.get('isError') is True and (payload.get('success') is True or payload.get('ok') is True):
        raise ValueError('MCP and business result disagree')
    return payload


def classify_result(payload):
    try:
        return _classify_result(payload)
    except (ValueError, TypeError, AttributeError):
        return 'unknown'


def _classify_result(payload):
    if not isinstance(payload, dict):
        return 'unknown'
    if 'data' in payload and not isinstance(payload['data'], dict):
        return 'unknown'
    flags = [payload[k] for k in ('ok', 'success') if k in payload]
    if not flags or any(type(flag) is not bool for flag in flags) or len(set(flags)) != 1:
        return 'unknown'
    if payload.get('error_code') in {'com_worker_request_error', 'unhandled_exception',
            'com_worker_protocol_error', 'com_worker_exit_error', 'instance_verification_failed'}:
        if payload.get('data', {}).get('completion_state') not in {'completed', 'not_started'}:
            return 'unknown'
    def unknown(item):
        if isinstance(item, dict):
            if item.get('completion_state') == 'unknown' or item.get('partial_mutation_possible') is True or item.get('partial_change_possible') is True:
                return True
            if item.get('reconciliation_required') is True:
                return True
            return any(unknown(v) for v in item.values() if isinstance(v, (dict, list)))
        return isinstance(item, list) and any(unknown(v) for v in item)
    if unknown(payload):
        return 'unknown'
    if flags[0] and payload.get('status') in {'error', 'partial', 'blocked_by_default'}:
        return 'unknown'
    return 'completed_success' if flags[0] else 'completed_failure'


def classify_tool_result(result):
    try:
        return classify_result(parse_tool_result(result))
    except ValueError:
        return 'unknown'


def effective_tool_timeout(name, args, base=180):
    budget = float(base)
    if name in {'list_dimensions', 'list_editable_parameters', 'list_features_with_dimensions', 'find_dimension_by_hint'}:
        budget = max(budget, 240)
    if name == 'run_benchmark_suite':
        budget = max(budget, 600)
    if name in {'loft', 'inspect_loft_profiles'}:
        names = args.get('profile_names')
        if isinstance(names, list) and len(names) > 19:
            budget = max(budget, min(600, 180+4*(len(names)-19)))
    return bounded_worker_timeout(name, budget)


def verify_step_geometry(path, native_health):
    # Existing maintained importer performs declared-unit conversion to mm.
    # This check has no private reference model or score-dependent tolerance.
    from src.metrics import read_step, geometry_info
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.TopTools import TopTools_IndexedMapOfShape
    from OCP.TopExp import TopExp
    with open(path, 'rb') as stream:
        header = stream.read(65536)
        if not header.lstrip().startswith(b'ISO-10303-21;'):
            raise ValueError('Export is not a STEP exchange file')
    section = header.split(b'ENDSEC;', 1)[0]
    schema = re.search(rb'FILE_SCHEMA\s*\(\s*\((.*?)\)\s*\)', section, re.S | re.I)
    if schema is None or not re.fullmatch(rb"\s*'AUTOMOTIVE_DESIGN[^']*'\s*", schema.group(1), re.I):
        raise ValueError('STEP file schema does not verify requested AP214')
    shape, units = read_step(path)
    info = geometry_info(shape, units)
    if not info['healthy'] or info['solid_count'] != native_health['solid_body_count']:
        raise ValueError('STEP validity/body count differs from native Part')
    # Every face must belong to an exported solid; loose sheet faces cannot hide.
    all_faces, solid_faces = TopTools_IndexedMapOfShape(), TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_FACE, all_faces)
    solids = TopExp_Explorer(shape, TopAbs_SOLID)
    while solids.More():
        TopExp.MapShapes_s(solids.Current(), TopAbs_FACE, solid_faces)
        solids.Next()
    if all_faces.Extent() != solid_faces.Extent():
        raise ValueError('STEP contains loose faces or sheet bodies')
    before, after = native_health['volume_mm3'], info['signed_volume_mm3']
    tolerance = max(GEOMETRY_TOLERANCES['volume_absolute_mm3'], abs(before)*GEOMETRY_TOLERANCES['volume_relative'])
    if abs(after-before) > tolerance:
        raise ValueError('STEP volume differs from native Part')
    if len(native_health.get('bbox_mm', [])) != 6 or any(abs(a-b) > GEOMETRY_TOLERANCES['bbox_absolute_mm']
            for a, b in zip(info['bbox_mm'], native_health['bbox_mm'])):
        raise ValueError('STEP bounding box differs from native Part')
    return {'status': 'passed', 'method': 'independent OCCT STEP import', 'geometry': info,
            'sheet_body_count': 0, 'step_schema': schema.group(1).decode('ascii'),
            'application_protocol':214, 'tolerances': GEOMETRY_TOLERANCES}


class SolidWorksAdapter:
    backend = 'solidworks'
    maximum_tool_timeout_s = MAXIMUM_TOOL_TIMEOUT_S

    def __init__(self, config, *, workspace, audit_dir, audit=None, deadline=None):
        from .cad import normalize_cad_config, cad_capabilities
        normalized = normalize_cad_config(config)
        capabilities = cad_capabilities(config)
        if capabilities['missing_dependencies']:
            raise ValueError('SolidWorks runtime dependencies missing: '+', '.join(capabilities['missing_dependencies']))
        self.config = normalized['cad_config']
        self.workspace = plain_path(workspace, kind='directory', allow_missing=True)
        self.audit_dir = plain_path(audit_dir, kind='directory', allow_missing=True)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.audit, self.deadline = audit, deadline
        self.instance = config.get('expected_instance')
        if self.instance is not None:
            verify_instance(None, self.instance)
        root = Path(self.config['bundle_path'])
        helper = Path(__file__).with_name('solidworks_helper.py').resolve()
        environment = {key: val for key, val in os.environ.items() if not key.startswith(
            ('SIM_SOLIDWORKS_', 'CAD_BENCHMARK_', 'FUSION_PIPELINE_', 'FUSION_BENCHMARK_'))}
        environment.update({'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1',
            'PYTHONNOUSERSITE': '1', 'SIM_SOLIDWORKS_SKILL_DIR': str(root),
            'SIM_SOLIDWORKS_EXPECTED_PYTHON': str(root/'runtime/python-embed/python.exe'),
            'SIM_SOLIDWORKS_EXPECTED_SOURCE': str(root/'runtime/solidworks-plugin/src'),
            'SIM_SOLIDWORKS_ENABLE_UNSAFE_TOOLS': '0', 'SIM_SOLIDWORKS_PARAMETER_GATE': '1',
            'SIM_SOLIDWORKS_PROCESS_ISOLATION': '1', 'SIM_SOLIDWORKS_WORKSPACE_ROOT': str(self.workspace),
            'SIM_SOLIDWORKS_LOG_DIR': str(self.audit_dir),
            'SIM_SOLIDWORKS_EXECUTOR_TIMEOUT_S': str(self.config['executor_timeout_s'])})
        if self.instance:
            environment['CAD_BENCHMARK_EXPECTED_INSTANCE'] = json.dumps(self.instance)
        self.client = StdioMCP(root/'runtime/python-embed/python.exe', ['-B', '-s', str(helper), 'mcp', str(root)],
            cwd=self.workspace, env=environment, audit_dir=self.audit_dir, audit=audit,
            timeout=max(30, self.config['executor_timeout_s']+15), deadline=deadline)
        self.tools = {name: value for name, value in self.client.tools.items() if name not in PRIVATE_TOOLS}
        if 'execute_python' in self.tools:
            self.close()
            raise ValueError('Unsafe Python unexpectedly exposed by SolidWorks runtime')
        required = {'connect_solidworks', 'create_new_part', 'save_document', 'export_step', 'validate_parameter_plan', 'list_configurations'}
        if not required.issubset(self.tools):
            self.close()
            raise ValueError('SolidWorks runtime lacks required native tools')
        self.identity = {'backend': self.backend, 'adapter_version': ADAPTER_VERSION,
            'adapter_sha256': file_sha256(__file__), 'helper_sha256': file_sha256(helper),
            'bridge_server': self.client.server, 'cad_config_sha256': normalized['cad_config_sha256'],
            'instance': self.instance, 'tools_sha256': hashlib.sha256(json.dumps(self.tools, sort_keys=True,
                ensure_ascii=False).encode('utf-8')).hexdigest()}
        source_files = [root/'scripts/runtime_contract.py',
                        *sorted((root/'runtime/solidworks-plugin/src').rglob('*.py'))]
        source_manifest = [{'path': str(path.relative_to(root)), 'sha256': file_sha256(path)}
                           for path in source_files]
        manifest_path = self.audit_dir/('vendor-source-'+uuid.uuid4().hex+'.json')
        write_json_new(manifest_path, {'bundle': str(root), 'files': source_manifest})
        self.identity['vendor_source_manifest'] = {'path':str(manifest_path), 'sha256':file_sha256(manifest_path)}
        write_json_new(self.audit_dir/('cad-discovery-'+uuid.uuid4().hex+'.json'), self.identity)

    def list_tools(self):
        return self.tools

    def require_call_budget(self, name, arguments, *, post_probe=True):
        """Reject before dispatch unless the native call and verification fit."""
        base = self.config['executor_timeout_s']
        native_budget = effective_tool_timeout(name, arguments, base) + 15
        probe_budget = effective_tool_timeout('benchmark_workspace', {}, base) + 15 if post_probe else 0
        overhead = 5 if post_probe else 0
        required = native_budget + probe_budget + overhead
        remaining = None if self.deadline is None else self.deadline - time.time()
        if remaining is not None and remaining < required:
            raise TimeoutError(f'CAD call requires {required:.3f}s including post-probe; only {remaining:.3f}s remain')
        return {'required_s': required, 'remaining_s': remaining,
                'native_budget_s': native_budget, 'post_probe_budget_s': probe_budget,
                'overhead_s': overhead}

    def _verified_preflight_failure(self, name, arguments, payload):
        """Recognize only reviewed no-write branches, bound to their vendor code."""
        try:
            def mutation_claim(item):
                if isinstance(item, dict):
                    return (any(key in item and item[key] is not False for key in
                                ('mutation_started', 'operation_started', 'write_entered'))
                            or any(mutation_claim(value) for value in item.values()))
                return isinstance(item, list) and any(mutation_claim(value) for value in item)
            if mutation_claim(payload):
                return False
            if name == 'extrude_sketch':
                message, code, number, vendor = ('Requested sketch was not found',
                    'selection_operation_failed', 104, 'swSelectionError')
                argument, sources = 'sketch_name', _SKETCH_LOOKUP_SOURCES
            elif name == 'create_thread':
                message, code, number, vendor = ('Profile must be an existing local absolute .sldlfp file',
                    'invalid_input', 107, 'swInvalidInput')
                argument, sources = 'profile_file', _THREAD_PROFILE_SOURCES
            else:
                return False
            if (payload.get('tool') != name
                    or payload.get('ok') is not False or payload.get('success') is not False
                    or payload.get('status') != 'error' or payload.get('message') != message
                    or payload.get('error') != message or payload.get('error_code') != code
                    or type(payload.get('vendor_error_code')) is not int or payload['vendor_error_code'] != number
                    or payload.get('vendor_error_name') != vendor
                    or payload.get('data') != {'mutation_started': False, 'automatic_retry_allowed': False}
                    or any(type(value) is not bool for value in payload['data'].values())
                    or not isinstance(arguments.get(argument), str) or not arguments[argument].strip()
                    or payload.get('arguments') != {key: value for key, value in arguments.items() if key != 'parameter_plan_token'}):
                return False
            isolation, executor = payload.get('process_isolation', {}), payload.get('executor', {})
            if (isolation.get('enabled') is not True or isolation.get('same_worker_instance_guard') is not True
                    or isolation.get('helper_version') != '1' or type(isolation.get('worker_pid')) is not int
                    or isolation['worker_pid'] <= 0 or type(isolation.get('returncode')) is not int
                    or isolation['returncode'] != 0 or executor.get('timeout_enforced') is not True
                    or executor.get('timeout_policy') != 'hard_process_termination'
                    or executor.get('timeout_exceeded') is not False or executor.get('soft_timeout_exceeded') is not False
                    or getattr(self, 'instance', None) is None):
                return False
            verify_instance(self.instance, payload.get('benchmark_instance'))
            source = Path(self.config['bundle_path'])/'runtime/solidworks-plugin/src/sim_plugin_solidworks'
            return all(file_sha256(source/relative) == digest for relative, digest in sources.items())
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            return False

    def _verified_health_contract_failure(self, name, arguments, payload):
        """A reviewed health workflow finished, but rejected requested geometry.

        This workflow rebuilds the Part. Require successful completion of every
        native check, including that rebuild; a failed/unknown check is not a
        geometry-contract rejection and cannot gain this completion evidence.
        """
        try:
            if (name != 'review_model_health' or payload.get('tool') != name
                    or payload.get('ok') is not False or payload.get('success') is not False
                    or payload.get('status') != 'error' or payload.get('error_code') != 'geometry_contract_failed'
                    or payload.get('arguments') != arguments):
                return False
            data = payload['data']
            checks = data['checks']
            names = {'document', 'rebuild', 'features', 'feature_tree', 'sketch', 'mass',
                     'bounding_box', 'properties', 'configurations'}
            if (set(checks) != names or data.get('failed_checks') != []
                    or not isinstance(data.get('successful_checks'), list)
                    or len(data['successful_checks']) != len(names) or set(data['successful_checks']) != names
                    or any(type(data.get(key)) is not int or data[key] != expected for key, expected in
                           [('check_count', 9), ('successful_check_count', 9), ('failed_check_count', 0)])
                    or any(check.get('ok') is not True or check.get('success') is not True
                           or check.get('status') != 'success' or check.get('error_code') is not None
                           for check in checks.values())):
                return False
            document, rebuild = checks['document']['data'], checks['rebuild']
            if (document.get('type') != 'Part' or type(document.get('type_code')) is not int
                    or document['type_code'] != 1 or rebuild.get('tool') != 'rebuild_active_document'
                    or rebuild['data'].get('raw_result') is not True or rebuild['data'].get('type') != 'Part'
                    or rebuild['data'].get('scope') != 'active_configuration'
                    or rebuild['data'].get('top_only') is not False):
                return False
            contracts = data['geometry_contracts']
            if (not isinstance(contracts, list) or not contracts
                    or any(not isinstance(item, dict) or type(item.get('success')) is not bool for item in contracts)):
                return False
            failed = [item for item in contracts if not item['success']]
            message = f'Model health checks ran, but {len(failed)} geometry/material contract(s) failed'
            if (not failed or data.get('failed_geometry_contracts') != failed
                    or type(data.get('geometry_contract_count')) is not int
                    or data['geometry_contract_count'] != len(contracts)
                    or type(data.get('geometry_contracts_passed')) is not int
                    or data['geometry_contracts_passed'] != len(contracts)-len(failed)
                    or payload.get('message') != message or payload.get('error') != message):
                return False
            isolation, executor = payload.get('process_isolation', {}), payload.get('executor', {})
            if (isolation.get('enabled') is not True or isolation.get('same_worker_instance_guard') is not True
                    or isolation.get('helper_version') != '1' or type(isolation.get('worker_pid')) is not int
                    or isolation['worker_pid'] <= 0 or type(isolation.get('returncode')) is not int
                    or isolation['returncode'] != 0 or executor.get('timeout_enforced') is not True
                    or executor.get('timeout_policy') != 'hard_process_termination'
                    or executor.get('timeout_exceeded') is not False or executor.get('soft_timeout_exceeded') is not False
                    or getattr(self, 'instance', None) is None):
                return False
            verify_instance(self.instance, payload.get('benchmark_instance'))
            source = Path(self.config['bundle_path'])/'runtime/solidworks-plugin/src/sim_plugin_solidworks/tools.py'
            # _review_model_health returns this exact error only after the
            # checks and geometry comparisons above; review changed vendors anew.
            return file_sha256(source) == _SKETCH_LOOKUP_SOURCES['tools.py']
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            return False

    def _call(self, name, arguments):
        budget = effective_tool_timeout(name, arguments, self.config['executor_timeout_s'])+15
        if self.deadline is not None and time.time()+budget > float(self.deadline):
            raise TimeoutError('Insufficient remaining deadline for native worker and cleanup; no call sent')
        result = self.client.call_tool(name, arguments, timeout=budget)
        raw_id = uuid.uuid4().hex
        raw_path = self.audit_dir/('cad-raw-'+raw_id+'.json')
        try:
            write_json_new(raw_path, {'tool':name, 'result':result})
        except Exception as error:
            self.client.unknown = True
            raise CadCallUnknown('CAD response evidence could not be persisted; reconciliation required') from error
        try:
            payload = parse_tool_result(result)
        except ValueError:
            self.client.unknown = True
            raise CadCallUnknown('Malformed CAD result; reconciliation required') from None
        outcome = classify_result(payload)
        schema = self.tools.get(name, {}).get('inputSchema', {})
        mutating = 'parameter_plan_token' in schema.get('required', []) or name in {
            'open_document', 'close_document', 'review_model_health', 'benchmark_trusted_export'}
        error_code = payload.get('error_code')
        predispatch = isinstance(error_code, str) and error_code in {'invalid_arguments', 'parameter_plan_required',
            'invalid_parameter_plan', 'parameter_operation_mismatch', 'parameter_unit_context_mismatch',
            'parameter_plan_not_ready', 'instance_not_bound', 'unknown_tool', 'unknown_or_disabled_tool'}
        if (outcome == 'completed_failure' and mutating and not predispatch
                and payload.get('data', {}).get('completion_state') not in {'completed', 'not_started'}):
            if not (self._verified_preflight_failure(name, arguments, payload)
                    or self._verified_health_contract_failure(name, arguments, payload)):
                outcome = 'unknown'
        try:
            write_json_new(self.audit_dir/('cad-call-'+raw_id+'.json'),
                {'tool': name, 'arguments_sha256': hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest(),
                 'outcome': outcome, 'result': result, 'raw_sha256': file_sha256(raw_path)})
        except Exception as error:
            self.client.unknown = True
            raise CadCallUnknown('CAD normalized evidence could not be persisted; reconciliation required') from error
        if outcome == 'unknown':
            self.client.unknown = True
            raise CadCallUnknown('CAD postconditions are unknown; reconciliation required')
        if outcome == 'completed_success' and name not in {'validate_parameter_plan', 'validate_engineering_report', 'get_solidworks_info'}:
            isolation = payload.get('process_isolation')
            executor = payload.get('executor')
            if (not isinstance(isolation, dict) or isolation.get('same_worker_instance_guard') is not True
                    or isolation.get('enabled') is not True or not isinstance(executor, dict)
                    or executor.get('timeout_enforced') is not True):
                self.client.unknown = True
                raise CadCallUnknown('CAD result lacks enforced same-worker instance guard')
        return result, payload

    def call_tool(self, name, arguments):
        if name not in self.tools or name in PRIVATE_TOOLS:
            raise ValueError('Unknown or private SolidWorks tool')
        result, payload = self._call(name, arguments)
        if payload.get('success') is True and payload.get('benchmark_instance'):
            self.instance = verify_instance(self.instance, payload['benchmark_instance'])
            self.identity['instance'] = self.instance
        if name == 'connect_solidworks' and payload.get('success') is True:
            self.identity['runtime'] = payload.get('data', {}).get('plugin_runtime')
        return result

    def connect(self, launch_if_needed=False):
        if type(launch_if_needed) is not bool:
            raise ValueError('launch_if_needed must be explicit boolean')
        result = self.call_tool('connect_solidworks', {'launch_if_needed': launch_if_needed})
        payload = parse_tool_result(result)
        if payload.get('success') is not True:
            raise ValueError('SolidWorks connection failed: '+str(payload.get('message', 'unavailable')))
        if self.instance is None:
            raise CadCallUnknown('Connection lacks verified instance identity')
        return self.probe_workspace()

    def probe_workspace(self):
        _, payload = self._call('benchmark_workspace', {})
        if payload.get('success') is not True:
            raise ValueError('SolidWorks workspace inspection failed: '+str(payload.get('message', 'unknown')))
        data = payload['data']
        self.instance = verify_instance(self.instance, data['instance'])
        self.identity['instance'] = self.instance
        return data

    def submission_spec(self):
        return {'filename': 'submission.SLDPRT', 'format': 'sldprt', 'document_type': 'Part'}

    def inspect_submission(self, path, configuration=None):
        _, payload = self._call('benchmark_inspect_submission', {'path': str(plain_path(path)), 'configuration': configuration})
        if payload.get('success') is not True:
            raise ValueError('Submission configuration/identity unverified: '+str(payload.get('message', 'unknown')))
        return payload['data']

    def trusted_reopen_export(self, native, folder, identity):
        from .native_contract import validate_native_descriptor
        source = self.workspace/native['path']
        validate_native_descriptor(native, identity=identity, path=source)
        if native.get('format') != 'sldprt' or native.get('document_type') != 'Part':
            raise ValueError('SolidWorks trusted export requires a Part descriptor')
        folder = plain_path(folder, kind='new', allow_missing=True)
        folder.mkdir(parents=True, exist_ok=False)
        staging = folder/'staging'
        staging.mkdir()
        copied, temporary = staging/'source.SLDPRT', staging/'export.step'
        shutil.copyfile(source, copied)
        plain_path(copied)
        if file_sha256(copied) != native['sha256'] or file_sha256(source) != native['sha256']:
            raise ValueError('Native bytes changed during staging')
        try:
            _, payload = self._call('benchmark_trusted_export', {'source': str(copied), 'output': str(temporary),
                'sha256': native['sha256'], 'configuration': native['configuration']})
            if payload.get('success') is not True:
                raise ValueError('Trusted native reopen/export failed: '+str(payload.get('message', 'unknown')))
            evidence = payload['data']
            if evidence.get('status') != 'passed' or evidence.get('active_configuration') != native['configuration']:
                raise ValueError('Trusted native evidence mismatch')
            verification = verify_step_geometry(temporary, evidence['health'])
            plain_path(temporary)
            if file_sha256(temporary) != evidence['step_sha256']:
                raise ValueError('STEP changed after native export')
            if file_sha256(source) != native['sha256'] or file_sha256(copied) != native['sha256']:
                raise ValueError('Native bytes changed during trusted export')
            step = folder/'trusted_export.step'
            report_path = folder/'reopen_report.json'
            report = {'schema': 'cad-trusted-export/1', 'status': 'passed', 'ok': True,
                **identity, 'native_document': native, 'adapter_identity': self.identity,
                'native_reopen': evidence, 'independent_verification': verification,
                'step_export': {'path': str(step), 'sha256': file_sha256(temporary),
                                'bytes': temporary.stat().st_size}, 'source_unchanged': True}
            report['template_selection_evidence'] = [json.loads(path.read_text(encoding='utf-8'))
                for path in sorted(self.audit_dir.glob('template-selection-*.json'))]
            temporary_report = staging/'report.json'
            write_json_new(temporary_report, report)
            # Exclusive same-volume publication; no overwrite and no unverified bytes.
            os.link(temporary, step)
            try:
                os.link(temporary_report, report_path)
            except BaseException:
                step.unlink()
                raise
            temporary.unlink()
            temporary_report.unlink()
            if json.loads(report_path.read_text(encoding='utf-8')) != report:
                raise ValueError('Trusted report disk/return mismatch')
            return {'status': 'passed', 'native_document': native, 'trusted_step': str(step),
                    'trusted_step_sha256': file_sha256(step), 'trusted_report': str(report_path),
                    'trusted_report_sha256': file_sha256(report_path), 'report': report}
        except BaseException as error:
            write_json_new(folder/'failure.json', {'stage': 'trusted_reopen_export', 'status': 'failed',
                'error_type': type(error).__name__, 'error': str(error), **identity, 'native_document': native})
            raise

    def close(self):
        self.client.close()
        if self.client.unknown:
            raise CadCallUnknown('CAD transport or owned worker cleanup requires reconciliation')
        return {'cleanup': self.client.cleanup_evidence, 'reconciliation_required': False}
