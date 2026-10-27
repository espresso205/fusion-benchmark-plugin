"""Private SolidWorks bridge extension, executed only with the vendor Python.

The installed bundle is imported unchanged. Every isolated native call binds the
COM server in that worker; private checks never enter the candidate tool schema.
"""
import ctypes
import hashlib
import io
import json
import math
import os
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

HELPER_VERSION = '1'
PRIVATE_TOOLS = {'benchmark_workspace', 'benchmark_inspect_submission', 'benchmark_trusted_export'}
_TEMPLATE_EVIDENCE = None


def bounded_worker_timeout(name, configured):
    """Keep the two read-only probes within their shared hard worker budget."""
    budget = float(configured)
    return min(budget, 30.0) if name in {'benchmark_workspace', 'get_solidworks_info'} else budget

# Values read from the installed SW2026 swconst.tlb. This classification is for
# a self-contained Part and complete-solid STEP export, never drawings/assemblies.
_LOAD_WARNING_NAMES = {
    1:'IdMismatch', 2:'ReadOnly', 4:'SharingViolation', 8:'DrawingANSIUpdate',
    16:'SheetScaleUpdate', 32:'NeedsRegen', 64:'BasePartNotLoaded', 128:'AlreadyOpen',
    256:'DrawingsOnlyRapidDraft', 512:'ViewOnlyRestrictions', 1024:'ViewMissingReferencedConfig',
    2048:'DrawingSFSymbolConvert', 4096:'RevolveDimTolerance', 8192:'ModelOutOfDate',
    16384:'DimensionsReferencedIncorrectlyToModels', 32768:'ComponentMissingReferencedConfig',
    65536:'InvisibleDoc_LinkedDesignTableUpdateFail', 131072:'MissingDesignTable',
    262144:'AutomaticRepair', 524288:'CriticalDataRepair', 1048576:'MissingExternalReferences'}
_SAVE_WARNING_NAMES = {
    1:'RebuildError', 2:'NeedsRebuild', 4:'ViewsNeedUpdate', 8:'AnimatorNeedToSolve',
    16:'AnimatorFeatureEdits', 32:'EdrwingsBadSelection', 64:'AnimatorLightEdits',
    128:'AnimatorCameraViews', 256:'AnimatorSectionViews', 512:'MissingOLEObjects',
    1024:'OpenedViewOnly', 2048:'XmlInvalid'}


def classify_native_warnings(kind, code):
    """Classify raw warning bits; an allowed warning never certifies geometry."""
    result = {'policy':'solidworks-part-warnings/1', 'kind':kind, 'raw':code,
              'status':'unknown', 'flags':[], 'unknown_bits':None,
              'geometry_verified_by_warning_policy':False}
    names = {'load':_LOAD_WARNING_NAMES, 'save':_SAVE_WARNING_NAMES}.get(kind)
    if names is None or type(code) is not int or code < 0:
        result['reason']='Warning kind or native nonnegative integer code is unavailable'
        return result
    allowed = {2} if kind=='load' else {64,128,256}
    uncertain = {4,8,16,256,2048} if kind=='load' else {4,8,32,2048}
    unknown_bits = code & ~sum(names)
    result['unknown_bits']=unknown_bits
    for bit,name in names.items():
        if not code & bit:
            continue
        classification = 'allowed' if bit in allowed else 'unknown' if bit in uncertain else 'blocked'
        reason = ('Read-only staged source is never saved; independent native/STEP checks remain required'
                  if kind=='load' and classification=='allowed' else
                  'Presentation-only animation state; native configuration, full solids and STEP geometry still require verification'
                  if classification=='allowed' else
                  'Warning meaning is not established for the selected Part/native-or-STEP context'
                  if classification=='unknown' else
                  'Warning affects identity, ownership, rebuild, configuration, dependencies or model data')
        result['flags'].append({'bit':bit,'name':f'swFile{kind.title()}Warning_'+name,
                                'classification':classification,'reason':reason})
    categories={flag['classification'] for flag in result['flags']}
    result['status'] = ('unknown' if unknown_bits or 'unknown' in categories else
                        'blocked' if 'blocked' in categories else 'allowed' if categories else 'clear')
    return result


def _record_warning_policy(report, field, kind, code):
    policy=classify_native_warnings(kind,code)
    report[field]=policy
    if policy['status']=='unknown':
        report.update(completion_state='unknown',reconciliation_required=True)
    if policy['status'] in {'blocked','unknown'}:
        raise ValueError(f"Native {kind} warning policy: {policy['status']}")


class PrivateVerificationError(ValueError):
    def __init__(self, message, evidence):
        super().__init__(message)
        self.evidence = evidence


def _process_handle(pid, terminate=False):
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000 | (0x100001 if terminate else 0), False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:
            raise ProcessLookupError(pid)
        raise OSError(ctypes.get_last_error(), 'Cannot verify owned worker process')
    return kernel, handle


def _process_record_handle(kernel, handle, pid):
    from ctypes import wintypes
    times = [wintypes.FILETIME() for _ in range(4)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)]*4)]
    if not kernel.GetProcessTimes(handle, *[ctypes.byref(item) for item in times]):
        raise OSError('Cannot read owned worker creation identity')
    size = wintypes.DWORD(32768)
    text = ctypes.create_unicode_buffer(size.value)
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    if not kernel.QueryFullProcessImageNameW(handle, 0, text, ctypes.byref(size)):
        raise OSError('Cannot read owned worker executable identity')
    return {'pid': pid, 'created': (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime,
            'executable': str(Path(text.value).resolve())}


def process_record(pid):
    kernel, handle = _process_handle(pid)
    try:
        return _process_record_handle(kernel, handle, pid)
    finally:
        kernel.CloseHandle(handle)


def record_worker_before_dispatch(process, record_path):
    """A worker cannot receive a request until ownership evidence is durable."""
    try:
        if not record_path:
            raise ValueError('Owned worker record path missing')
        record = process_record(process.pid)
        with open(record_path, 'a', encoding='utf-8') as stream:
            stream.write(json.dumps(record)+'\n')
            stream.flush()
            os.fsync(stream.fileno())
    except Exception as error:
        # Popen retains the exact process handle; no PID reuse or child traversal.
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
        raise ValueError('Worker ownership could not be recorded; request not dispatched') from error


def terminate_owned_worker(record):
    """Terminate only the exact recorded Python worker, never its child CAD server."""
    evidence = {'pid': record.get('pid'), 'terminated': False, 'scope': 'exact_owned_python_worker'}
    if (type(record.get('pid')) is not int or record['pid'] <= 0
            or type(record.get('created')) is not int or record['created'] <= 0 or
            Path(str(record.get('executable', ''))).name.lower() not in {'python.exe', 'pythonw.exe'}):
        return {**evidence, 'error': 'invalid worker ownership record'}
    try:
        kernel, handle = _process_handle(record['pid'], terminate=True)
    except ProcessLookupError:
        return {**evidence, 'already_exited': True}
    except OSError as error:
        return {**evidence, 'error': str(error)}
    try:
        actual = _process_record_handle(kernel, handle, record['pid'])
        if (actual.get('pid') == record['pid'] and type(actual.get('created')) is int
                and actual['created'] > 0 and actual['created'] != record['created']):
            # The open handle identifies a different process lifetime. The old
            # worker exited before this PID could be reused; never signal the new
            # process. Keep both identities so repeated PIDs remain auditable.
            return {**evidence, 'already_exited': True, 'pid_reused': True,
                    'recorded_identity': dict(record), 'observed_identity': actual}
        if actual != record:
            return {**evidence, 'error': 'worker identity changed; no termination attempted'}
        from ctypes import wintypes
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        if not kernel.TerminateProcess(handle, 1):
            return {**evidence, 'error': 'worker termination failed'}
        waited = kernel.WaitForSingleObject(handle, 5000)
        return {**evidence, 'terminated': waited == 0, 'wait_result': int(waited)}
    finally:
        kernel.CloseHandle(handle)


def value(obj, name, *args):
    member = getattr(obj, name)
    return member(*args) if callable(member) and not hasattr(member, '_oleobj_') else member


def verify_instance(expected, actual):
    if (not isinstance(actual, dict) or type(actual.get('pid')) is not int or actual['pid'] <= 0
            or type(actual.get('session_id')) is not int or actual['session_id'] < 0
            or not isinstance(actual.get('version'), str) or not actual['version']
            or not isinstance(actual.get('executable'), str) or not actual['executable']):
        raise ValueError('SolidWorks instance evidence is incomplete')
    if expected is not None:
        for key in ('pid', 'session_id', 'version', 'executable'):
            left, right = expected.get(key), actual[key]
            if key == 'executable' and isinstance(left, str):
                left, right = os.path.normcase(os.path.realpath(left)), os.path.normcase(os.path.realpath(right))
            if left != right:
                raise ValueError('SolidWorks instance drift: ' + key)
    return actual


def validate_references(evidence):
    if (not isinstance(evidence, dict) or evidence.get('complete') is not True
            or evidence.get('external') != [] or evidence.get('imported_features') != []):
        raise ValueError('Part is not independently verified self-contained')
    return True


def validate_health(evidence):
    if not isinstance(evidence, dict) or evidence.get('complete') is not True:
        raise ValueError('Native health evidence incomplete')
    solids, sheets = evidence.get('solid_body_count'), evidence.get('sheet_body_count')
    volume = evidence.get('volume_mm3')
    if type(solids) is not int or solids <= 0 or sheets != 0:
        raise ValueError('Require nonempty solid Part without sheet bodies')
    if type(volume) not in (int, float) or not math.isfinite(volume) or volume <= 0:
        raise ValueError('Part volume is unknown or nonpositive')
    bodies = evidence.get('bodies')
    if not isinstance(bodies, list) or len(bodies) != solids or any(
            b.get('valid') is not True or type(b.get('fault_count')) is not int or b['fault_count'] != 0 for b in bodies):
        raise ValueError('Native body validity is unknown or failed')
    features = evidence.get('features')
    if not isinstance(features, list) or any(type(f.get('error_code')) is not int or
            f['error_code'] != 0 or f.get('warning') is not False for f in features):
        raise ValueError('Native feature health is unknown or failed')
    return True


def _sha(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def plain_path(path, *, kind='file', allow_missing=False):
    """Inspect each lexical ancestor before resolution can erase an alias."""
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError('Parent traversal is not permitted in maintenance paths')
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if allow_missing:
                continue
            raise
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Maintenance paths cannot use symlink/reparse aliases')
        if part != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError('Maintenance path ancestor is not a directory')
        if part == path:
            if kind == 'file' and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size <= 0):
                raise ValueError('Maintenance file must be nonempty, regular and unaliased')
            if kind == 'directory' and not stat.S_ISDIR(info.st_mode):
                raise ValueError('Maintenance directory expected')
            if kind == 'new':
                raise ValueError('Maintenance output already exists')
    return path


def select_part_template(existing, instance, *, program_data=None):
    """Use the bundle choice or one current-version installation template."""
    source = 'vendor_template_discovery'
    if existing:
        selected = Path(existing)
    else:
        major = int(instance['version'].split('.', 1)[0])
        if not 20 <= major <= 100:
            raise ValueError('SolidWorks product year is unavailable for template discovery')
        root = program_data if program_data is not None else os.environ.get('ProgramData')
        if not root:
            raise ValueError('Windows ProgramData is unavailable')
        try:
            directory = plain_path(Path(root)/'SOLIDWORKS'/f'SOLIDWORKS {major+1992}'/'templates', kind='directory')
        except FileNotFoundError as error:
            raise ValueError('Current-version standard template directory is missing') from error
        candidates = [item for item in directory.iterdir() if item.suffix.lower() == '.prtdot']
        if len(candidates) != 1:
            raise ValueError('Current-version Part template is missing or ambiguous')
        selected = candidates[0]
        source = 'verified_instance_version_standard_directory'
    selected = plain_path(selected)
    if selected.suffix.lower() != '.prtdot':
        raise ValueError('Part template format is invalid')
    return {'path': str(selected), 'sha256': _sha(selected), 'bytes': selected.stat().st_size,
            'source': source, 'instance_version': instance['version']}


def _install_template_selection():
    from sim_plugin_solidworks._vendor.solidworks_mcp.automation import documents
    original = documents.find_template
    def find_template(kind):
        global _TEMPLATE_EVIDENCE
        existing = original(kind)
        if kind != 'part':
            return existing
        from sim_plugin_solidworks.tools import _sw_automation
        evidence = select_part_template(existing, _instance(_sw_automation()._sw_app))
        _TEMPLATE_EVIDENCE = evidence
        return evidence['path']
    documents.find_template = find_template


def _instance(app):
    pid = value(app, 'GetProcessID')
    sid = ctypes.c_ulong()
    if type(pid) is not int or not ctypes.windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(sid)):
        raise ValueError('Cannot resolve CAD Windows session')
    version = value(app, 'RevisionNumber')
    executable = value(app, 'GetExecutablePath')
    return verify_instance(None, {'pid': pid, 'session_id': sid.value,
                                'version': str(version or ''), 'executable': str(executable or '')})


def _features(doc):
    rows, objects, seen = [], [], set()
    def walk(first, parent, sub=False):
        item = first
        while item is not None:
            name = str(value(item, 'Name'))
            kind = str(value(item, 'GetTypeName2'))
            key = (parent, name, kind)
            if key in seen or len(seen) >= 10000:
                raise ValueError('Feature traversal is cyclic or ambiguous')
            seen.add(key)
            rows.append({'name': name, 'type': kind, 'parent': parent})
            objects.append(item)
            walk(value(item, 'GetFirstSubFeature'), parent+'/'+name, True)
            item = value(item, 'GetNextSubFeature' if sub else 'GetNextFeature')
    walk(value(doc, 'FirstFeature'), '')
    return rows, objects


def _references(doc, app):
    from sim_plugin_solidworks._vendor.solidworks_mcp.automation.document_saving import reference_snapshot
    paths = reference_snapshot(doc, app=app)
    rows, objects = _features(doc)
    counts = {'model': value(doc, 'ListExternalFileReferencesCount2'),
              'extension': value(doc.Extension, 'ListExternalFileReferencesCount'),
              'auxiliary': value(doc, 'ListAuxiliaryExternalFileReferencesCount')}
    for row, feature in zip(rows, objects):
        row['external_reference_count'] = value(feature, 'ListExternalFileReferencesCount')
        counts[row['parent']+'/'+row['name']] = row['external_reference_count']
    if any(type(count) is not int or count < 0 for count in counts.values()):
        raise ValueError('External reference count is unavailable')
    external_name = value(doc, 'GetExternalReferenceName')
    if external_name is not None and not isinstance(external_name, str):
        raise ValueError('External reference name readback unknown')
    imported = [r for r in rows if any(word in r['type'].lower() for word in ('import', 'interconnect'))]
    external = list(paths.values())
    external.extend({'scope': scope, 'count': count} for scope, count in counts.items() if count)
    if external_name:
        external.append({'native_external_reference': external_name})
    result = {'complete': True, 'external': external, 'imported_features': imported,
              'native_counts': counts, 'features_including_suppressed': rows,
              'method': 'native dependencies searched/unsearched plus model/extension/auxiliary/every-feature counts'}
    return result


def _body_extreme(body, direction):
    import pythoncom
    inputs = ((pythoncom.VT_R8, pythoncom.PARAMFLAG_FIN),)*3
    outputs = ((pythoncom.VT_BYREF | pythoncom.VT_R8, pythoncom.PARAMFLAG_FOUT),)*3
    result = body._oleobj_.InvokeTypes(body._oleobj_.GetIDsOfNames('GetExtremePoint'), 0,
        pythoncom.DISPATCH_METHOD, (pythoncom.VT_BOOL, 0), inputs+outputs, *direction)
    if not isinstance(result, tuple) or len(result) != 4 or result[0] not in (True, -1, 1):
        raise ValueError('Native extreme point unavailable')
    coords = [float(n)*1000 for n in result[1:]]
    if not all(map(math.isfinite, coords)):
        raise ValueError('Invalid native extreme point')
    return coords


def _health(doc):
    from sim_plugin_solidworks._vendor.solidworks_mcp.automation.feature_execution import feature_error_code
    if value(doc, 'GetType') != 1:
        raise ValueError('Only SolidWorks Part documents are supported')
    if value(doc, 'ForceRebuild3', False) not in (True, 1, -1):
        raise ValueError('Native full rebuild failed')
    features, objects = _features(doc)
    for row, feature in zip(features, objects):
        error, warning = feature_error_code(feature)
        row.update(error_code=error, warning=warning)
    solids = value(doc, 'GetBodies2', 0, False)
    sheets = value(doc, 'GetBodies2', 1, False)
    if solids is None:
        solids = []
    if sheets is None:
        sheets = []
    if not isinstance(solids, (list, tuple)) or not isinstance(sheets, (list, tuple)):
        raise ValueError('Native body enumeration unavailable')
    volume, rows, bounds = 0.0, [], []
    for body in solids:
        fault = value(body, 'Check3')
        count = 0 if fault is None else value(fault, 'Count')
        mass = value(body, 'GetMassProperties', 1.0)
        if not isinstance(mass, (list, tuple)) or len(mass) < 4:
            raise ValueError('Native body mass properties unavailable')
        amount = float(mass[3])*1e9
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError('Native body volume invalid')
        volume += amount
        bbox = []
        for axis in range(3):
            direction = [0., 0., 0.]
            direction[axis] = -1.
            low = _body_extreme(body, direction)[axis]
            direction[axis] = 1.
            high = _body_extreme(body, direction)[axis]
            bbox.append((low, high))
        bounds.append(bbox)
        rows.append({'valid': type(count) is int and count == 0, 'fault_count': count,
                     'volume_mm3': amount})
    bbox = ([min(b[a][0] for b in bounds) for a in range(3)] +
            [max(b[a][1] for b in bounds) for a in range(3)]) if bounds else None
    result = {'complete': True, 'features': features, 'solid_body_count': len(solids),
              'sheet_body_count': len(sheets), 'bodies': rows, 'volume_mm3': volume,
              'bbox_mm': bbox, 'bbox_method': 'IBody2.GetExtremePoint', 'working_unit': 'mm'}
    return result


def _workspace(app, expected):
    from sim_plugin_solidworks._vendor.solidworks_mcp.automation.document_identity import open_documents
    instance = verify_instance(expected, _instance(app))
    if value(app, 'StartupProcessCompleted') is not True or value(app, 'CommandInProgress') is not False:
        raise ValueError('CAD startup/busy state is not known safe')
    docs, identities = open_documents(app)
    for doc, row in zip(docs, identities):
        dirty = value(doc, 'GetSaveFlag')
        if type(dirty) is not bool:
            raise ValueError('Document dirty status unavailable')
        row.update(document_type=row['type'], dirty=dirty)
    active = value(app, 'ActiveDoc')
    current = None
    if active is not None:
        current = {'path': value(active, 'GetPathName'), 'title': value(active, 'GetTitle'),
                   'type': {1:'Part', 2:'Assembly', 3:'Drawing'}.get(value(active, 'GetType'))}
    return {'benchmark_probe': True, 'safe': True, 'safe_to_model': True,
            'identity': instance, 'instance': instance, 'documents': identities,
            'documents_count': len(docs), 'active_document': current, 'active_command': None}


def activate_configuration(doc, requested):
    evidence = {'requested':requested, 'before':None, 'native_return':None,
                'after':None, 'status':'failed', 'method':'ShowConfiguration2 with native before/after readback'}
    try:
        evidence['before'] = str(value(value(doc.ConfigurationManager,'ActiveConfiguration'),'Name'))
        evidence['native_return'] = value(doc,'ShowConfiguration2',requested)
        evidence['after'] = str(value(value(doc.ConfigurationManager,'ActiveConfiguration'),'Name'))
        switched = evidence['native_return'] is True
        unchanged = evidence['native_return'] is False and evidence['before'] == requested
        if evidence['after'] == requested and (switched or unchanged):
            evidence['status']='passed'
            evidence['already_active_noop']=unchanged
    except Exception as error:
        evidence['error_type']=type(error).__name__
        evidence['error']=str(error)
    return evidence


def _private_operation(name, args, expected):
    from sim_plugin_solidworks.tools import _sw_automation
    sw = _sw_automation()
    connection = sw.connect(launch_if_needed=False)
    if connection.get('success') is not True:
        raise ValueError('SolidWorks is not connected')
    app = sw.app
    before = _workspace(app, expected)
    if name == 'benchmark_workspace':
        return before
    if name == 'benchmark_inspect_submission':
        doc, error = sw.get_active_doc()
        if error:
            raise ValueError('Submitted native document must still be active for configuration capture')
        path = plain_path(args['path'])
        if Path(value(doc, 'GetPathName')).resolve() != path or value(doc, 'GetType') != 1:
            raise ValueError('Submitted native document identity does not match active Part')
        configuration = str(value(value(doc.ConfigurationManager, 'ActiveConfiguration'), 'Name'))
        if not configuration or (args.get('configuration') and configuration != args['configuration']):
            raise ValueError('Submission configuration mismatch')
        return {'path': str(path), 'configuration': configuration, 'document_type': 'Part',
                'sha256': _sha(path), 'instance': before['instance'], 'verified': False,
                'inspection_method': 'active-document identity; independent reopen remains required'}
    if name != 'benchmark_trusted_export':
        raise ValueError('Unknown private operation')
    if before['documents']:
        raise ValueError('Trusted reopen requires empty session; user documents are preserved')
    source = plain_path(args['source'])
    output = plain_path(args['output'], kind='new', allow_missing=True)
    if source.parent != output.parent or output.exists() or source.suffix.lower() != '.sldprt' or output.suffix.lower() != '.step':
        raise ValueError('Invalid private export paths')
    if _sha(source) != args['sha256']:
        raise ValueError('Staged native hash mismatch')
    report = {'schema': 'solidworks-native-reopen/1', 'instance': before['instance'],
              'source_sha256': args['sha256'], 'configuration': args['configuration'],
              'opened': False, 'closed': False, 'preferences_restored': False}
    doc = None
    previous = None
    failure = None
    cleanup_errors = []
    try:
        opened = sw.open_document(str(source), silent_open=True)
        report['open_result'] = opened
        report['open_warnings']=classify_native_warnings('load',opened.get('data',{}).get('load_warning_code'))
        if report['open_warnings']['status']=='unknown':
            report.update(completion_state='unknown',reconciliation_required=True)
        if opened.get('success') is not True:
            raise ValueError('Native reopen failed or is unknown')
        doc, error = sw.get_active_doc()
        if error or Path(value(doc, 'GetPathName')).resolve() != source or value(doc, 'GetType') != 1:
            raise ValueError('Reopened document is not the requested Part')
        report['opened'] = True
        if report['open_warnings']['status'] in {'blocked','unknown'}:
            raise ValueError('Native load warning policy: '+report['open_warnings']['status'])
        config = args['configuration']
        names = value(doc, 'GetConfigurationNames')
        if not isinstance(names, (tuple, list)) or config not in names:
            raise ValueError('Frozen configuration is absent')
        activation = activate_configuration(doc,config)
        report['configuration_activation'] = activation
        if activation['status'] != 'passed':
            raise ValueError('Frozen configuration activation/readback failed')
        active = activation['after']
        report['active_configuration'] = active
        report['references'] = _references(doc, app)
        validate_references(report['references'])
        report['health'] = _health(doc)
        validate_health(report['health'])
        # Official swUserPreferenceIntegerValue_e.swStepAP = 75 (installed API typelib).
        previous = value(app, 'GetUserPreferenceIntegerValue', 75)
        if type(previous) is not int:
            raise ValueError('STEP AP preference unavailable')
        if value(app, 'SetUserPreferenceIntegerValue', 75, 214) is not True or value(app, 'GetUserPreferenceIntegerValue', 75) != 214:
            raise ValueError('STEP AP214 could not be applied and read back')
        report['export_policy'] = {'ap': 214, 'previous_ap': previous, 'body_scope': 'solid',
            'native_length_unit': 'm', 'measurement_unit': 'mm', 'document_units': list(value(doc, 'GetUnits')),
            'step_unit_source': 'STEP file declaration independently parsed after export'}
        exported = sw.export_document(str(output), export_format='step', body_scope='solid')
        report['export_result'] = exported
        _record_warning_policy(report,'export_warnings','save',exported.get('data',{}).get('solidworks_warning_code'))
        if exported.get('success') is not True or exported.get('data', {}).get('solidworks_error_code') != 0:
            raise ValueError('Native STEP export failed')
        if str(value(value(doc.ConfigurationManager, 'ActiveConfiguration'), 'Name')) != config:
            raise ValueError('Configuration changed during export')
        verify_instance(before['instance'], _instance(app))
        report['step_sha256'] = _sha(output)
    except Exception as error:
        failure = str(error)
        report['failure_type'] = type(error).__name__
    finally:
        if previous is not None:
            try:
                restored = value(app, 'SetUserPreferenceIntegerValue', 75, previous)
                report['preferences_restored'] = restored is True and value(app, 'GetUserPreferenceIntegerValue', 75) == previous
            except Exception as error:
                cleanup_errors.append('preference restoration: '+str(error))
        else:
            report['preferences_restored'] = True
        if doc is not None:
            # Captured path and exact title checked by native close; never save rebuilt source.
            try:
                closed = sw.close_document(save=False, document_title=value(doc, 'GetTitle'), expected_path=str(source))
                report['close_result'] = closed
                report['closed'] = closed.get('success') is True
            except Exception as error:
                cleanup_errors.append('document close: '+str(error))
        try:
            report['source_unchanged'] = _sha(source) == args['sha256']
            if not report['source_unchanged']:
                failure = failure or 'Staged source changed during trusted reopen'
        except Exception as error:
            cleanup_errors.append('source hash: '+str(error))
        try:
            after = _workspace(app, before['instance'])
            report['cleanup'] = after
            report['closed'] = not after['documents']
        except Exception as error:
            cleanup_errors.append('final workspace: '+str(error))
        report['cleanup_errors'] = cleanup_errors
        report['cleanup_confirmed'] = not cleanup_errors and report['closed'] and report['preferences_restored']
        report['status'] = 'passed' if not failure and report['cleanup_confirmed'] else 'failed'
        report['error'] = failure
        # Failure evidence is deliberately retained even though it cannot be published as trusted.
        evidence_path = output.parent/'native-helper-report.json'
        with evidence_path.open('x', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, default=str)
    if failure or not report['cleanup_confirmed']:
        raise PrivateVerificationError(failure or 'Trusted cleanup incomplete; reconciliation required', report)
    return report


def _load_vendor(bundle):
    root = Path(bundle).resolve()
    python = root/'runtime/python-embed/python.exe'
    if Path(sys.executable).resolve() != python.resolve():
        raise ValueError('Private helper requires the configured vendor Python')
    source = root/'runtime/solidworks-plugin/src'
    sys.path.insert(0, str(source))
    sys.path.insert(0, str(root/'scripts'))
    from runtime_contract import verify_bundled_runtime
    verify_bundled_runtime(root)
    from sim_plugin_solidworks.runtime_identity import collect_runtime_identity
    identity = collect_runtime_identity()
    if identity.get('self_contained_verified') is not True:
        raise ValueError('Vendor runtime identity not verified')
    return identity


def _install_instance_guard(expected, *, allow_launch=False):
    from sim_plugin_solidworks._vendor.solidworks_mcp.automation.base import SolidWorksAutomation
    original = SolidWorksAutomation._try_connect_com
    original_connect = SolidWorksAutomation.connect
    def guarded(self):
        result = original(self)
        if result:
            verify_instance(expected, _instance(self._sw_app))
            processes = self._solidworks_process_snapshot()
            if len(processes) != 1:
                raise ValueError('Multiple or unenumerated SolidWorks instances')
        return result
    SolidWorksAutomation._try_connect_com = guarded
    def connect(self, launch_if_needed=True):
        # Native create/open methods internally call connect() with its unsafe
        # launch-by-default policy. Only the initial explicit host connect may
        # create a server. A frozen instance can never be replaced automatically.
        permitted = bool(launch_if_needed and allow_launch and expected is None)
        return original_connect(self, launch_if_needed=permitted)
    SolidWorksAutomation.connect = connect


def read_worker_instance(expected):
    """COM proxies stay on the vendor executor apartment that acquired them."""
    from sim_plugin_solidworks.tools import _COM_EXECUTOR, _sw_automation
    def inspect():
        sw = _sw_automation()
        return verify_instance(expected, _instance(sw._sw_app)) if sw._sw_app is not None else None
    return _COM_EXECUTOR.submit(inspect).result()


def _worker(bundle):
    request_bytes = sys.stdin.buffer.read()
    request = json.loads(request_bytes.decode('utf-8'))
    _load_vendor(bundle)
    expected = request.pop('benchmark_expected_instance', None)
    _install_instance_guard(expected, allow_launch=request.get('tool') == 'connect_solidworks'
                            and request.get('arguments', {}).get('launch_if_needed') is True)
    _install_template_selection()
    os.environ['SIM_SOLIDWORKS_ISOLATED_WORKER'] = '1'
    os.environ['SIM_SOLIDWORKS_PARAMETER_GATE'] = '0'  # Outer vendor MCP already validates token and exact operation.
    name = request.get('tool')
    if name in PRIVATE_TOOLS:
        try:
            data = _private_operation(name, request.get('arguments') or {}, expected)
            result = {'ok': True, 'success': True, 'status': 'success', 'tool': name, 'data': data}
        except Exception as error:
            evidence = getattr(error, 'evidence', {})
            uncertain = name == 'benchmark_trusted_export' and evidence.get('cleanup_confirmed') is not True
            result = {'ok': False, 'success': False, 'status': 'error', 'tool': name,
                'error_code': 'private_verification_failed', 'message': str(error),
                'data': {'completion_state': 'unknown' if uncertain else 'completed',
                         'automatic_retry_allowed': False, 'evidence': evidence}}
        sys.stdout.buffer.write(json.dumps(result, ensure_ascii=True).encode('ascii'))
        return
    # Keep vendor document guards and its native workflow semantics intact.
    from sim_plugin_solidworks import com_worker
    original_stdout = sys.stdout
    captured = io.BytesIO()
    sys.stdin = io.TextIOWrapper(io.BytesIO(json.dumps(request).encode('utf-8')), encoding='utf-8')
    sys.stdout = io.TextIOWrapper(captured, encoding='utf-8', write_through=True)
    try:
        com_worker.main()
        result = json.loads(captured.getvalue())
        if _TEMPLATE_EVIDENCE is not None:
            result['benchmark_private_template'] = _TEMPLATE_EVIDENCE
        instance = read_worker_instance(expected)
        if instance is not None:
            result['benchmark_instance'] = instance
        elif name != 'get_solidworks_info' and result.get('success') is True:
            raise ValueError('Native result lacks bound COM instance')
    except Exception as error:
        result = {'ok': False, 'success': False, 'status': 'error', 'error_code': 'instance_verification_failed',
                  'message': str(error), 'data': {'completion_state': 'unknown', 'automatic_retry_allowed': False}}
    finally:
        sys.stdout = original_stdout
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=True).encode('ascii'))


def _serve(bundle):
    _load_vendor(bundle)
    from sim_plugin_solidworks import tools
    expected = json.loads(os.environ['CAD_BENCHMARK_EXPECTED_INSTANCE']) if os.environ.get('CAD_BENCHMARK_EXPECTED_INSTANCE') else None
    def isolated(request, *, tool, timeout_s):
        nonlocal expected
        timeout_s = bounded_worker_timeout(tool, timeout_s)
        if expected is None and tool not in {'get_solidworks_info', 'connect_solidworks'}:
            return tools._result(False, tool, 'Explicit verified connection required before CAD operations', error_code='instance_not_bound')
        request = dict(request, benchmark_expected_instance=expected)
        environment = dict(os.environ, SIM_SOLIDWORKS_ISOLATED_WORKER='1')
        started = time.monotonic()
        process = subprocess.Popen([sys.executable, '-B', '-s', str(Path(__file__).resolve()), 'worker', str(bundle)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        record_path = os.environ.get('CAD_BENCHMARK_WORKER_RECORD')
        try:
            record_worker_before_dispatch(process, record_path)
        except Exception as error:
            return tools._result(False, tool, 'Worker ownership recording failed',
                {'completion_state':'not_started' if process.poll() is not None else 'unknown',
                 'automatic_retry_allowed':False, 'error_type':type(error).__name__},
                error_code='owned_worker_record_failed')
        try:
            output, stderr = process.communicate(json.dumps(request).encode('utf-8'), timeout=timeout_s)
            result = json.loads(output.decode('utf-8'))
            if not isinstance(result, dict) or type(result.get('success')) is not bool or process.returncode != 0:
                raise ValueError('Private worker protocol mismatch')
        except (subprocess.TimeoutExpired, ValueError, UnicodeError) as error:
            process.kill()
            try:
                _, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stderr = b'worker pipes did not close'
            result = tools._result(False, tool, 'Private worker completion unknown',
                {'completion_state': 'unknown', 'partial_mutation_possible': True, 'automatic_retry_allowed': False,
                 'error_type': type(error).__name__, 'stderr': stderr.decode('utf-8', errors='replace')[-4000:]},
                error_code='guarded_worker_unknown')
        result['process_isolation'] = {'enabled': True, 'worker_pid': process.pid,
            'hard_timeout_s': timeout_s, 'duration_s': time.monotonic()-started, 'returncode': process.returncode,
            'helper_version': HELPER_VERSION, 'same_worker_instance_guard': True}
        result['executor'] = {'timeout_enforced': True, 'timeout_policy': 'hard_process_termination',
                              'timeout_s': timeout_s, 'serial': True}
        # Template paths are maintenance evidence and never candidate output.
        template = result.pop('benchmark_private_template', None)
        if template is not None:
            template_path = Path(os.environ['SIM_SOLIDWORKS_LOG_DIR'])/('template-selection-'+uuid.uuid4().hex+'.json')
            with template_path.open('x', encoding='utf-8') as stream:
                json.dump({'tool': tool, 'worker_pid': process.pid, 'template': template}, stream,
                          ensure_ascii=False, indent=2)
        instance = result.get('benchmark_instance')
        if tool == 'connect_solidworks' and result.get('success') is True:
            expected = verify_instance(expected, instance)
        return result
    tools._run_isolated_request = isolated
    original_specs, original_run = tools.list_tool_specs, tools.run_tool
    def specs(*args, **kwargs):
        return original_specs(*args, **kwargs) + [tools.ToolSpec(name, 'Private maintenance operation',
            {'type': 'object'}, 'benchmark-private') for name in sorted(PRIVATE_TOOLS)]
    def run(name, arguments=None, **kwargs):
        if name in PRIVATE_TOOLS:
            return isolated({'tool': name, 'arguments': arguments or {},
                             'workspace_root': os.environ['SIM_SOLIDWORKS_WORKSPACE_ROOT']},
                            tool=name, timeout_s=float(os.environ.get('SIM_SOLIDWORKS_EXECUTOR_TIMEOUT_S', '180')))
        return original_run(name, arguments, **kwargs)
    tools.list_tool_specs, tools.run_tool = specs, run
    from sim_plugin_solidworks import mcp_server
    # Runtime identity verification imports this module before extension wiring.
    # Rebind its imported references as well as the registry module itself.
    mcp_server.list_tool_specs, mcp_server.run_tool = specs, run
    mcp_server.run()


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] not in {'mcp', 'worker'}:
        raise SystemExit('Usage: solidworks_helper.py mcp|worker <bundle>')
    (_serve if sys.argv[1] == 'mcp' else _worker)(sys.argv[2])
