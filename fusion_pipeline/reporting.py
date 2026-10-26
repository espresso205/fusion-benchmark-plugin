"""Maintainer-only HTML reporting, isolated from scoring and candidate output."""
import json
from pathlib import Path


V4_PROTOCOL = 'fusion-step-adapted-v4.0'
GENERATION_FILE = 'html-report-generation.json'
SAFE_FAILURE = 'HTML report generation failed; completed scoring results are unchanged.'


def _inside_file(root, relative):
    root = Path(root).resolve()
    path = root / relative
    if not path.is_file() or not path.resolve().is_relative_to(root):
        return None
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink() or (hasattr(part, 'is_junction') and part.is_junction()):
            return None
    return path


def report_references(private_root):
    """Return only existing private artifacts, never paths from saved JSON."""
    root = Path(private_root)
    files = {}
    index = _inside_file(root, 'html-report/index.html')
    status_path = _inside_file(root, GENERATION_FILE)
    if index:
        files['html'] = str(index)
    generation = None
    if status_path:
        files['html_report_status'] = str(status_path)
        try:
            value = json.loads(status_path.read_text(encoding='utf-8'))
            if isinstance(value, dict) and value.get('status') in ('completed', 'partial', 'failed'):
                generation = {'status': value['status']}
                if type(value.get('case_count')) is int and value['case_count'] >= 0:
                    generation['case_count'] = value['case_count']
                if value['status'] == 'failed':
                    generation['error'] = SAFE_FAILURE
        except (OSError, ValueError):
            generation = {'status': 'failed', 'error': SAFE_FAILURE}
    result = {'files': files} if files else {}
    if generation is not None:
        result['html_report_generation'] = generation
    return result


def build_private_report(private_root, pipeline, *, config=None, timeout=10.0, inputs=None, interfaces=None):
    """Generate from all requested cases; ordinary failures never escape."""
    config = config or {}
    protocol = pipeline.get('scoring_protocol') or config.get('protocol', V4_PROTOCOL)
    if protocol != V4_PROTOCOL:
        return {'status': 'not_applicable'}
    root = Path(private_root).resolve()
    # A misconfigured maintainer root must not publish GT in a candidate folder.
    for row in pipeline.get('cases', []):
        if row.get('workspace'):
            workspace = Path(row['workspace']).resolve()
            if root.is_relative_to(workspace) or workspace.is_relative_to(root):
                return {'status': 'failed', 'error': SAFE_FAILURE}
    result = {'status': 'failed', 'error': SAFE_FAILURE}
    try:
        from src.html_reporting import try_build_report
        merged = dict(pipeline, scoring_protocol=protocol,
                      coordinate_mode=pipeline.get('coordinate_mode') or config.get('coordinate_mode', 'geometry'))
        source = root / 'evaluation' if (root / 'evaluation/summary.json').is_file() else None
        inputs = inputs if inputs is not None else config.get('report_inputs')
        interfaces = interfaces if interfaces is not None else config.get('report_interfaces')
        options = {'inputs': inputs} if inputs is not None else {}
        if interfaces is not None:
            options['interfaces'] = interfaces
        result = try_build_report(source, root / 'html-report', pipeline=merged,
                                  timeout=timeout, run_name=pipeline.get('run_id', root.name), **options)
        if not isinstance(result, dict) or result.get('status') not in ('completed', 'partial', 'failed'):
            result = {'status': 'failed', 'error': SAFE_FAILURE}
        elif result['status'] == 'failed':
            result = {'status': 'failed', 'error': SAFE_FAILURE}
    except Exception:
        result = {'status': 'failed', 'error': SAFE_FAILURE}
    # The shared renderer normally writes this file. Also cover import errors
    # and its dependency failures without writing into the public pipeline run.
    try:
        status_path = root / GENERATION_FILE
        with status_path.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
    except (OSError, ValueError, TypeError):
        pass
    return result
