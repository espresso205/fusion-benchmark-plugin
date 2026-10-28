"""Versioned public protocol, independent of optional modeling knowledge."""
import hashlib
import json
from pathlib import Path

PROTOCOL_ROOT = Path(__file__).resolve().parent.parent / 'agent' / 'common-protocol'
PROTOCOL_FILES = ('PROTOCOL.md', 'SOLIDWORKS.md', 'FUSION.md')


def common_protocol_sha256():
    digest = hashlib.sha256()
    for name in PROTOCOL_FILES:
        data = (PROTOCOL_ROOT / name).read_bytes()
        digest.update(name.encode('utf-8') + b'\x00' + str(len(data)).encode('ascii') + b'\x00' + data)
    return digest.hexdigest()


def build_gateway_instructions(contract, public):
    text = (PROTOCOL_ROOT / 'PROTOCOL.md').read_text(encoding='utf-8')
    if 'cad_native' in public.get('mcp_servers', []):
        name = 'SOLIDWORKS.md' if contract.get('cad_backend') == 'solidworks' else 'FUSION.md'
        text += '\n' + (PROTOCOL_ROOT / name).read_text(encoding='utf-8')
    else:
        text += ('\nThis attempt is negative_control_no_cad. Native modeling capability is disabled. '
                 'Report the unavailable capability with write_report; native submission is forbidden.\n')
    skills = public.get('skills', [])
    if skills:
        text += ('\nSelected optional skills, in order: ' + ', '.join(item['id'] for item in skills) +
                 '. Call list_skills, then read_skill with each selected id in that order. '
                 'Only declared static resource IDs may be read with resource_id.\n')
    return text


def build_candidate_prompt(case, contract, public):
    header = {'schema': 'candidate-prompt/2', 'case_id': case.get('case_id', contract.get('case_id')),
              'workspace': contract.get('workspace'), 'deadline': contract.get('deadline'),
              'max_tool_calls': contract.get('max_tool_calls'), 'public_capabilities': public}
    return json.dumps(header, ensure_ascii=False, sort_keys=True) + '\n\n' + build_gateway_instructions(contract, public)
