"""Private, GT-bound interface display input; never a source of scores.

Only explicitly requested cases are read. The returned paths are worker-only
and must not be projected into report JSON or exposed to candidate tools.
"""
import hashlib
import json
from pathlib import Path
import re

from .report_inputs import _no_links, _source_path

SCHEMA_VERSION = 'fusion-report-interfaces/1'
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_REGION_BYTES = 64 * 1024 * 1024
MAX_REGIONS = 256


def _digest(value):
    return isinstance(value, str) and re.fullmatch('[a-fA-F0-9]{64}', value) is not None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('接口清单不能包含重复字段。')
        result[key] = value
    return result


def load_interfaces(manifest, model_sources):
    """Validate private jig identity; isolate ordinary per-case file failures."""
    try:
        path = _no_links(Path(manifest))
        with path.open('rb') as stream:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError('接口清单超过大小限制。')
        document = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError('无法读取接口清单。') from None
    if (not isinstance(document, dict) or document.get('schema_version') != SCHEMA_VERSION or
            not isinstance(document.get('cases'), list)):
        raise ValueError('不支持此接口清单结构。')
    entries, count = {}, 0
    for entry in document['cases']:
        if not isinstance(entry, dict):
            raise ValueError('接口案例必须是对象。')
        case, regions = entry.get('case'), entry.get('regions')
        if not isinstance(case, str) or not case or len(case) > 160 or case in entries:
            raise ValueError('接口案例标识缺失或重复。')
        if not _digest(entry.get('gt_sha256')) or not isinstance(regions, list) or not regions:
            raise ValueError('接口案例需要标准答案哈希和非空区域列表。')
        for region in regions:
            if (not isinstance(region, dict) or not isinstance(region.get('path'), str) or
                    not region['path'] or len(region['path']) > 2048 or
                    not _digest(region.get('sha256')) or region.get('fit_type') not in ('KOR', 'KIR')):
                raise ValueError('接口区域需要路径、SHA256 和 KOR/KIR 类型。')
            count += 1
            if count > MAX_REGIONS:
                raise ValueError('接口区域数量超过限制。')
        entries[case] = entry
    result = {}
    for case, sources in model_sources.items():
        if case not in entries:
            continue
        entry = entries[case]
        verified = {'gt_sha256': entry['gt_sha256'].lower(), 'regions': []}
        result[case] = verified
        reference = sources.get('world', {}).get('reference') or sources.get('scoring', {}).get('reference') or {}
        if reference.get('sha256') != verified['gt_sha256']:
            verified['error'] = 'interface_reference_mismatch'
            continue
        try:
            for region in entry['regions']:
                source = _source_path(path.parent, region['path'])
                if source.suffix.lower() not in ('.step', '.stp') or not source.is_file():
                    raise ValueError('region unavailable')
                if source.stat().st_size > MAX_REGION_BYTES:
                    raise ValueError('region too large')
                with source.open('rb') as stream:
                    digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                if digest != region['sha256'].lower():
                    raise ValueError('region changed')
                verified['regions'].append({'path': str(source.resolve()), 'sha256': digest,
                                             'fit_type': region['fit_type']})
        except (OSError, ValueError):
            verified.update(regions=[], error='interface_input_unavailable')
    return result
