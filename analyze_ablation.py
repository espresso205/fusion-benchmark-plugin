"""Analyze existing evaluation JSON without importing a geometry backend."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

from analysis.ablation import analyze_dataset
from analysis.reporting import csv_text, markdown_text
from analysis.result_reader import read_source
from analysis.system_comparison import compare_systems

ANALYSIS_VERSION = '1.0.0'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON key: {key}')
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError(f'non-finite JSON value: {value}')


def source_specs(args):
    if args.input:
        return [{'path': path} for path in args.input], []
    path = args.manifest.resolve()
    raw = path.read_bytes()
    document = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique_object, parse_constant=_nonfinite)
    if not isinstance(document, dict) or set(document) != {'sources'}:
        raise ValueError('analysis manifest must contain only sources')
    sources = document['sources']
    if not isinstance(sources, list) or not sources:
        raise ValueError('sources must be a nonempty list')
    specs = []
    for row in sources:
        if not isinstance(row, dict) or set(row) - {'path', 'id', 'system'} or not isinstance(row.get('path'), str) or not row['path']:
            raise ValueError('each source needs a path and optional id/system')
        for label in ('id', 'system'):
            if label in row and (not isinstance(row[label], str) or not row[label].strip()):
                raise ValueError(f'{label} must be a nonempty string')
        source = Path(row['path'])
        specs.append({**row, 'path': source if source.is_absolute() else path.parent / source})
    return specs, [{'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}]


def run_analysis(args):
    output = args.output.resolve()
    if output.exists():
        raise ValueError('output must be a new directory')
    specs, manifest_files = source_specs(args)
    datasets = []
    ids, paths = set(), set()
    for spec in specs:
        path = Path(spec['path']).resolve()
        if not path.exists():
            raise ValueError(f'input does not exist: {path}')
        source_root = path if path.is_dir() else path.parent
        # Match the reader's legacy root even when optional weights.json is absent.
        if source_root.name == 'results' and (source_root / 'final_scores.json').exists():
            source_root = source_root.parent
        if output.is_relative_to(source_root) or source_root.is_relative_to(output):
            raise ValueError('output must be separate from historical input directories')
        data = read_source(path, source_id=spec.get('id'), system=spec.get('system'))
        if data['source_id'] in ids:
            raise ValueError('duplicate source_id; provide unique ids in an analysis manifest')
        identity = tuple(sorted(item['path'] for item in data['source_files']))
        if identity in paths:
            raise ValueError('the same result source cannot be included twice')
        ids.add(data['source_id'])
        paths.add(identity)
        datasets.append(data)
    analyses = [analyze_dataset(data) for data in datasets]
    code_root = Path(__file__).resolve().parent
    code_files = [Path(__file__), *sorted((code_root / 'analysis').glob('*.py'))]
    source_files = {item['path']: item['sha256'] for data in datasets for item in data['source_files']}
    source_files.update({item['path']: item['sha256'] for item in manifest_files})
    document = {'schema_version': 'fusion-ablation/1', 'analysis_version': ANALYSIS_VERSION,
        'ranking_eligible': False, 'generated_at': datetime.now(timezone.utc).isoformat(),
        'analysis_code_hashes': {str(path.relative_to(code_root)): digest(path) for path in code_files},
        'sources': [{key: data[key] for key in ('source_id', 'system', 'source_protocol_id', 'weights', 'source_files', 'warnings')} for data in datasets],
        'source_files': [{'path': path, 'sha256': value} for path, value in sorted(source_files.items())],
        'analyses': analyses, 'system_comparisons': compare_systems(datasets, analyses)}
    contents = {'ablation.json': json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + '\n',
                'ablation.csv': csv_text(document), 'ablation.md': markdown_text(document)}
    for path, expected in source_files.items():
        if digest(path) != expected:
            raise ValueError(f'source changed during analysis: {path}')
    output.mkdir(parents=True, exist_ok=False)
    marker = output / 'INCOMPLETE'
    marker.write_text('Analysis report generation is incomplete.\n', encoding='utf-8')
    for name, content in contents.items():
        with (output / name).open('x', encoding='utf-8-sig' if name.endswith('.csv') else 'utf-8', newline='') as stream:
            stream.write(content)
    marker.unlink()
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description='Offline CAD metric ablation (no Fusion or geometry calls)')
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--input', nargs='+', type=Path, help='Completed v3 or legacy result directories / summary JSON')
    inputs.add_argument('--manifest', type=Path, help='Analysis manifest with source paths and optional system identities')
    parser.add_argument('--output', type=Path, required=True, help='New output directory outside historical results')
    args = parser.parse_args(argv)
    try:
        output = run_analysis(args)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 2
    print(json.dumps({'output': str(output), 'report': str(output / 'ablation.md'), 'ranking_eligible': False}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    raise SystemExit(main())
