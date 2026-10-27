"""Explicit, billable CLI visual check through the production Worker Gateway."""
import hashlib
import json
import os
from pathlib import Path
import random
import time
import uuid


def assess_answer(answer, expected):
    try:
        text = answer.strip()
        if text.startswith('```') and text.endswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0]
        return json.loads(text).get('observations') == expected
    except (ValueError, AttributeError, TypeError):
        return False


def assess_reads(reads, expected):
    keys = ('id', 'index', 'source_sha256', 'returned_sha256')
    return (len(reads) == len(expected)
            and all(all(read.get('image', {}).get(key) == image.get(key) for key in keys)
                    for read, image in zip(reads, expected)))


def run_visual_check(config_path, output):
    from PIL import Image, ImageDraw
    from .adapters import executable, launch_fresh, probe
    from .input_assets import SCHEMA, iter_assets, image_result, verify_manifest
    from .runner import BASE, atomic, stop_tree, source_hashes
    from src.io_utils import strict_json

    config = strict_json(config_path)
    source_before = source_hashes()
    provider = config.get('provider', 'codex')
    model = config['model']
    output = Path(output).absolute()
    output.mkdir(parents=True, exist_ok=False)
    workspace, audit = output / 'public', output / 'audit'
    workspace.mkdir()
    audit.mkdir()
    doctor = probe(provider, config.get('cli_executable'))
    atomic(output / 'cli-probe.json', doctor)
    atomic(output / 'source-hashes.json', source_before)
    if not doctor['available']:
        raise ValueError('CLI lacks required flags; visual check not started')
    task = ('Read every image in listed order using read_image. Count the circles and triangles in each image. '
            'Return only JSON with observations, an ordered array of objects containing id, circles and triangles. '
            'Use only the benchmark MCP input tools. Do not use filesystem, shell, Fusion or other tools.')
    (workspace / 'TASK.md').write_text(task, encoding='utf-8')
    def descriptor(identifier, name, mime, role):
        data = (workspace / name).read_bytes()
        return dict(id=identifier, role=role, path=name, mime=mime, bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(), display_name=name)
    expected, attachments = [], []
    rng = random.SystemRandom()
    for index in range(2):
        identifier = 'asset-' + chr(97 + index)
        circles, triangles = rng.randint(1, 4), rng.randint(1, 4)
        if expected and (circles, triangles) == (expected[0]['circles'], expected[0]['triangles']):
            triangles = triangles % 4 + 1
        picture = Image.new('RGB', (960, 560), 'white')
        draw = ImageDraw.Draw(picture)
        kinds = ['circle'] * circles + ['triangle'] * triangles
        rng.shuffle(kinds)
        for position, kind in enumerate(kinds):
            x, y = 50 + (position % 4) * 230, 45 + (position // 4) * 270
            if kind == 'circle':
                draw.ellipse((x, y, x + 130, y + 130), fill=(32, 90, 190))
            else:
                draw.polygon(((x + 65, y), (x + 130, y + 130), (x, y + 130)), fill=(190, 55, 35))
        filename = f'input-{index + 1}.png'
        picture.save(workspace / filename)
        attachments.append(descriptor(identifier, filename, 'image/png', 'view'))
        expected.append(dict(id=identifier, circles=circles, triangles=triangles))
    manifest = dict(schema=SCHEMA, case_id='Q00',
        task=descriptor('task', 'TASK.md', 'text/markdown', 'task'), attachments=attachments)
    verify_manifest(manifest, workspace)
    atomic(output / 'expected-private.json', dict(observations=expected))
    atomic(output / 'input-manifest.json', manifest)
    returned = []
    for asset in attachments:
        result = image_result(manifest, workspace, asset['id'])
        returned.append(json.loads(result['content'][0]['text']))
    atomic(output / 'image-transmission.json', returned)
    contract = dict(schema='fusion-worker/1', run_id=output.name, case_id='Q00', attempt_id=uuid.uuid4().hex,
        workspace=str(workspace), audit_dir=str(audit), model=model, max_tool_calls=12,
        deadline=time.time() + 300, budget_scope='mcp_tools_scope', vision_only=True,
        public_files=[a['path'] for a in iter_assets(manifest)], input_manifest=manifest)
    atomic(audit / 'worker-contract.json', contract)
    command = [config['python'], '-B', str(BASE / 'fusion-benchmark-bootstrap.py'),
               'worker', '--contract', str(audit / 'worker-contract.json')]
    spec = dict(provider=provider, executable=executable(provider, config.get('cli_executable')),
        workspace=str(workspace), audit_dir=str(audit), prompt='Call get_contract, list_inputs, read_input(name=task), then follow that task. Read both images.',
        model=model, worker_command=command, env={k: v for k, v in os.environ.items()
            if not k.startswith(('FUSION_PIPELINE_', 'FUSION_BENCHMARK_'))}, timeout=300,
        credential_settings=config.get('credential_settings'),
        strict_tool_isolation=config.get('strict_tool_isolation', False), existing_mcp_names=[])
    processes = []
    try:
        execution = launch_fresh(spec, processes)
    finally:
        for process in processes:
            stop_tree(process)
    answer = ''
    for line in (audit / 'session.jsonl').read_text(encoding='utf-8').splitlines():
        event = json.loads(line)
        if provider == 'codex' and event.get('type') == 'item.completed' and event.get('item', {}).get('type') == 'agent_message':
            answer = event['item'].get('text', '')
        elif provider == 'claude' and event.get('type') == 'result':
            answer = event.get('result', '')
        elif provider == 'opencode' and event.get('type') == 'text':
            answer += event.get('part', {}).get('text', '')
    journal = audit / 'gateway-events.jsonl'
    events = [json.loads(line) for line in journal.read_text(encoding='utf-8').splitlines()] if journal.exists() else []
    reads = [e for e in events if e.get('type') == 'completed' and e.get('tool') == 'read_image']
    # Recognition and production gateway completion are separately disclosed.
    recognized = assess_answer(answer, expected)
    verified_reads = assess_reads(reads, returned)
    passed = (execution['returncode'] == 0 and not execution['timed_out']
              and execution['session_verification']['verified'] and recognized and verified_reads
              and source_hashes() == source_before)
    result = dict(status='passed' if passed else 'failed', provider=provider, requested_model=model,
        cli_version=doctor.get('version'), execution=execution, image_read_completions=len(reads),
        recognized=recognized, gateway_images_verified=verified_reads, answer=answer, images=returned,
        model_identity=execution['session_verification']['model_identity_source'],
        scope='This CLI/configuration/model request only; actual Codex model identity is not attested.')
    atomic(output / 'visual-check-result.json', result)
    return result
