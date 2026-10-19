"""Package an unchanged frozen evaluator plus explicitly selected pipeline files.

--check-only validates and plans entirely in memory. It does not create output.
No private inputs, live configuration, model invocation or Fusion access is used.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import uuid
import zipfile


PREFIX = 'fusion_benchmark_evaluator_v2/'
REQUIRED_EVALUATOR_FILES = (
    'evaluate.py', 'worker.py', 'weights.json', 'requirements-evaluator.txt',
    'scheduler.py', 'scheduler/__init__.py', 'scheduler/__main__.py', 'scheduler/core.py',
    'src/__init__.py', 'src/io_utils.py',
)
OVERLAY_FILES = (
    'plugins/fusion-benchmark-guard.mjs',
    'run-fusion-benchmark.ps1',
    'requirements-fusion-pipeline.txt',
    'docs/UNATTENDED_FUSION_PLUGIN.md',
    'docs/MODELING_AGENT_GUIDE.md',
    'docs/DISTRIBUTION_GUIDE.md',
    'docs/EVALUATOR_AGENT_GUIDE.md',
    'tests/test_fusion_runner.py',
    'tests/test_fusion_runner_safety.py',
    'tests/test_fusion_scripts.py',
    'tests/test_fusion_integration.py',
    'tests/fusion_guard.test.mjs',
    'tests/test_package_fusion_pipeline.py',
    'scripts/package_fusion_pipeline.py',
)
REQUIRED_PIPELINE_MODULES = {'__init__.py', '__main__.py', 'runner.py', 'mcp.py', 'fusion_scripts.py', 'integration_smoke.py'}
EXAMPLE_CONFIG = {
    'model': 'lanyi/gpt-6.1-sol',
    'evaluator_zip': 'C:/benchmark/payload/evaluator.zip',
    'opencode': 'C:/benchmark/tools/opencode/opencode.exe',
    'python': 'C:/benchmark/.venv/Scripts/python.exe',
    'questions_root': 'C:/benchmark/questions',
    'private_cases': 'C:/benchmark/private/cases.json',
    'runs_root': 'C:/benchmark/runs',
    'private_runs_root': 'C:/benchmark/private/runs',
    'fusion_skill': 'C:/benchmark/opencode/skills/autodeskfusion/SKILL.md',
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode('utf-8')


def safe_relative(name):
    p = PurePosixPath(name)
    reserved = {'CON', 'PRN', 'AUX', 'NUL'} | {f'{kind}{n}' for kind in ('COM', 'LPT') for n in range(1, 10)}
    if (not isinstance(name, str) or not name or p.is_absolute() or str(p) != name
            or '\\' in name or ':' in name or '\x00' in name or '..' in p.parts
            or any(part.rstrip(' .') != part or part.split('.')[0].upper() in reserved for part in p.parts)):
        raise ValueError('unsafe package path')
    return p


def read_overlay(root, relative):
    safe_relative(relative)
    source = root / relative
    if not source.is_file():
        raise ValueError(f'missing required overlay: {relative}')
    if not source.resolve().is_relative_to(root):
        raise ValueError(f'overlay path escapes source root: {relative}')
    for item in (source, *source.parents):
        if item == root:
            break
        if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
            raise ValueError(f'linked overlay path is not allowed: {relative}')
    return source.read_bytes()


def load_frozen_evaluator(archive, expected_evaluator_sha256=None):
    if (expected_evaluator_sha256 is not None
            and (not isinstance(expected_evaluator_sha256, str)
                 or not re.fullmatch(r'[0-9a-fA-F]{64}', expected_evaluator_sha256))):
        raise ValueError('expected evaluator SHA256 must contain 64 hexadecimal characters')
    raw = Path(archive).read_bytes()
    if expected_evaluator_sha256 is not None and sha(raw) != expected_evaluator_sha256.lower():
        raise ValueError('frozen evaluator archive SHA256 mismatch')
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        names = source.namelist()
        if len(names) != len(set(name.casefold() for name in names)):
            raise ValueError('duplicate archive entries')
        manifest_raw = source.read(PREFIX + 'PACKAGE_MANIFEST.json')
        manifest = json.loads(manifest_raw)
        rows = manifest.get('files')
        if not isinstance(rows, dict):
            raise ValueError('invalid frozen evaluator manifest')
        for name in rows:
            safe_relative(name)
        expected = {PREFIX + name for name in rows} | {PREFIX + 'PACKAGE_MANIFEST.json'}
        if set(names) != expected:
            raise ValueError('archive entries do not match its manifest')
        files = {}
        for name, meta in rows.items():
            info = source.getinfo(PREFIX + name)
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError('unsafe archive symbolic link')
            data = source.read(info)
            if len(data) != meta.get('bytes') or sha(data) != meta.get('sha256'):
                raise ValueError(f'frozen evaluator file hash or size mismatch: {name}')
            files[name] = {'content': data, 'source': 'frozen-evaluator'}
        missing = set(REQUIRED_EVALUATOR_FILES) - files.keys()
        if missing:
            raise ValueError('missing evaluator dependencies: ' + ', '.join(sorted(missing)))
    return raw, manifest_raw, files


README = '''# OpenCode + Fusion 自动流水线整合包

本包根目录可运行 `python -m fusion_pipeline`。原已冻结评测器文件铺在根目录以保留离线测试与评测入口；`payload/evaluator.zip` 保留同一原始 ZIP 的完整字节，真实流水线只使用配置中指定的这份冻结 ZIP。

1. 在自己的 Python 3.12 环境执行 `python -m pip install -r requirements-fusion-pipeline.txt`。
2. 单独安装、登录并配置 Autodesk Fusion、现有 autodeskfusion MCP 与 OpenCode；模型固定 `lanyi/gpt-6.1-sol`。这些软件及凭据不在本包中。
3. 复制 `examples/fusion_pipeline_config.json` 到自己的配置位置，替换全部绝对路径。`evaluator_zip` 应指向本包解压目录的 `payload/evaluator.zip`；示例中的 `C:/benchmark` 只是占位路径。
4. 在本包根目录运行 `python -m fusion_pipeline run --config C:/benchmark/pipeline.json --preflight-only`。该检查会真实调用模型和只读 Fusion 工具。
5. 检查通过后用 `python -m fusion_pipeline run --config C:/benchmark/pipeline.json` 创建新运行；查询使用 `python -m fusion_pipeline status <打印出的run目录>`。也可用 `./run-fusion-benchmark.ps1 -Config <配置路径>`。

详细配置、私有 cases 格式、预加载门、200次/1800秒/一次attempt、停止条件与审计见 `docs/UNATTENDED_FUSION_PLUGIN.md`。没有OS沙箱；未知Fusion请求停止全批并保留锁，必须人工核查，不能自动重发。当前包不附十题数据或执行结果，也不证明十题已经完成；正式排名资格始终为false。

离线检查：`python -m unittest discover -s tests -v`。需要评测依赖；缺历史数据的用例会明确skip。可选 `node --test tests/fusion_guard.test.mjs` 检查guard；Node仅用于这组测试，OpenCode使用自身运行时加载插件。

`PACKAGE_MANIFEST.json` 逐文件记录SHA256、大小和来源（frozen-evaluator、pipeline-overlay、generated、frozen-evaluator-archive）；`SHA256SUMS` 还包含清单文件哈希。原评测器清单留在 `payload/evaluator-manifest.json`。哈希证明字节完整性，不代替维护者对原包来源的确认。不要把私有参考、配置、凭据或运行日志加入本包再转发。

维护者重新打包时应添加 `--expected-evaluator-sha256 <已确认原ZIP的64位SHA256>`；脚本在读取ZIP条目及创建输出前核验，并将预期与实际摘要记入生成清单。示例配置始终由纯占位模板生成，不读取本机编辑后的配置示例。
'''

AGENTS = '''# 整合包 Agent 接手入口

先读 `docs/UNATTENDED_FUSION_PLUGIN.md` 和根目录README。无人值守入口是 `python -m fusion_pipeline run --config <用户自己的JSON>`；`--preflight-only` 会真实调用指定模型与只读Fusion工具。模型固定lanyi/gpt-6.1-sol，每题新run上下文、200次工具请求、1800秒、一次attempt。

根目录保留原冻结评测器及离线scheduler供验证；scheduler自身不会启动OpenCode或Fusion。真实流水线从原样的payload/evaluator.zip解压并运行评测器，不把当前候选生成的新规则作为评分依据。用户必须设置自己的绝对路径并另行提供公开题目和私有参考。

每次新建输出，不覆盖或自动续跑旧attempt。GT/标注/历史答案/分数只留在维护端，建模Agent只获得单题公开输入、工作区及指定技能。未知Fusion请求停止全批保留锁，人工核查前不重发。提示词/工具路径检查不构成OS沙箱。排名资格始终false；十题结果以具体run证据为准。

验证使用 `python -m unittest discover -s tests -v`；Node可选用于 `node --test tests/fusion_guard.test.mjs`。记录实际passed/skipped，不能把模拟API测试当作真实Fusion验收。
'''


def prepare_bundle(source_root, evaluator_zip, expected_evaluator_sha256=None):
    root = Path(source_root).resolve()
    archive_raw, evaluator_manifest, files = load_frozen_evaluator(evaluator_zip, expected_evaluator_sha256)
    original_names = set(files)
    selected = list(OVERLAY_FILES) + ['fusion_pipeline/' + name for name in sorted(REQUIRED_PIPELINE_MODULES)]
    for relative in selected:
        data = read_overlay(root, relative)
        row = {'content': data, 'source': 'pipeline-overlay'}
        if relative in files:
            row['replaces_sha256'] = sha(files[relative]['content'])
        files[relative] = row
    # Never read a locally edited example: it may have become an operational
    # configuration containing real machine paths or credentials.
    example_path = 'examples/fusion_pipeline_config.json'
    previous = files.get(example_path)
    files[example_path] = {'content': json_bytes(EXAMPLE_CONFIG), 'source': 'generated'}
    if previous:
        files[example_path]['replaces_sha256'] = sha(previous['content'])
    for name, text in (('README.md', README), ('AGENTS.md', AGENTS)):
        previous = files.get(name)
        files[name] = {'content': text.encode('utf-8'), 'source': 'generated'}
        if previous:
            files[name]['replaces_sha256'] = sha(previous['content'])
    files['payload/evaluator.zip'] = {'content': archive_raw, 'source': 'frozen-evaluator-archive'}
    files['payload/evaluator-manifest.json'] = {'content': evaluator_manifest, 'source': 'frozen-evaluator'}
    return {'files': files, 'evaluator_sha256': sha(archive_raw), 'frozen_evaluator_file_count': len(original_names),
            'expected_evaluator_sha256': expected_evaluator_sha256.lower() if expected_evaluator_sha256 is not None else None,
            'overlaid_evaluator_paths': sorted(name for name in original_names if files[name]['source'] != 'frozen-evaluator')}


def write_bundle(plan, output_parent):
    output_parent = Path(output_parent).resolve()
    output_parent.mkdir(parents=True, exist_ok=True)
    name = 'fusion_pipeline_plugin_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ_') + uuid.uuid4().hex[:8]
    destination = output_parent / name
    destination.mkdir(exist_ok=False)
    marker = destination / 'INCOMPLETE'
    marker.write_text('Packaging not yet verified. Do not distribute.\n', encoding='utf-8')
    files = dict(plan['files'])
    manifest = {key: value for key, value in plan.items() if key != 'files'}
    manifest.update(schema_version='fusion-pipeline-package/1', created_at_utc=datetime.now(timezone.utc).isoformat(),
                    files={name: {'bytes': len(row['content']), 'sha256': sha(row['content']),
                                  **{key: value for key, value in row.items() if key != 'content'}}
                           for name, row in sorted(files.items())})
    files['PACKAGE_MANIFEST.json'] = {'content': json_bytes(manifest), 'source': 'generated'}
    sums = ''.join(f'{sha(row["content"])}  {name}\n' for name, row in sorted(files.items()))
    files['SHA256SUMS'] = {'content': sums.encode('utf-8'), 'source': 'generated'}
    for relative, row in sorted(files.items()):
        target = destination.joinpath(*safe_relative(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(row['content'])
    archive_path = output_parent / (name + '.zip')
    with zipfile.ZipFile(archive_path, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for relative in sorted(files):
            archive.write(destination / relative, arcname=name + '/' + relative)
    with zipfile.ZipFile(archive_path) as archive:
        if set(archive.namelist()) != {name + '/' + relative for relative in files}:
            raise ValueError('generated archive file set mismatch')
        for relative, row in files.items():
            if sha(archive.read(name + '/' + relative)) != sha(row['content']):
                raise ValueError('generated archive hash mismatch')
    archive_hash = sha(archive_path.read_bytes())
    with archive_path.with_suffix('.zip.sha256').open('x', encoding='utf-8') as stream:
        stream.write(archive_hash + '  ' + archive_path.name + '\n')
    marker.unlink()
    return {'directory': str(destination), 'archive': str(archive_path), 'archive_sha256': archive_hash,
            'evaluator_sha256': plan['evaluator_sha256'], 'expected_evaluator_sha256': plan['expected_evaluator_sha256'],
            'files': len(files)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evaluator-zip', type=Path, required=True)
    parser.add_argument('--expected-evaluator-sha256', help='optional trusted original ZIP SHA256; mismatch aborts before output')
    parser.add_argument('--output-parent', type=Path, required=True)
    parser.add_argument('--source-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--check-only', action='store_true', help='validate in memory; do not create any output')
    args = parser.parse_args(argv)
    plan = prepare_bundle(args.source_root, args.evaluator_zip, args.expected_evaluator_sha256)
    if args.check_only:
        result = {'mode': 'check-only', 'files_before_generated_manifests': len(plan['files']),
                  'frozen_evaluator_file_count': plan['frozen_evaluator_file_count'],
                  'evaluator_sha256': plan['evaluator_sha256'], 'expected_evaluator_sha256': plan['expected_evaluator_sha256'],
                  'overlaid_evaluator_paths': plan['overlaid_evaluator_paths']}
    else:
        result = {'mode': 'packaged', **write_bundle(plan, args.output_parent)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
