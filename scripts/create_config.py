"""Create recipient-local configuration without reading credentials or running tools."""
import argparse
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlparse


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--provider', choices=('opencode', 'codex', 'claude'), required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--cli-executable', type=Path, required=True)
    parser.add_argument('--questions-root', type=Path, required=True)
    parser.add_argument('--private-cases', type=Path, required=True)
    parser.add_argument('--runs-root', type=Path, required=True)
    parser.add_argument('--private-runs-root', type=Path, required=True)
    parser.add_argument('--fusion-url', required=True, help='Verified recipient-local Fusion MCP endpoint')
    parser.add_argument('--case-ids', nargs='+', default=['Q01'])
    parser.add_argument('--allow-native-tools', action='store_true',
                        help='Explicitly disable strict tool isolation; native tools remain available in the trusted same-account environment')
    args = parser.parse_args(argv)
    if args.provider == 'codex' and not args.allow_native_tools:
        parser.error('the Codex adapter cannot disable all native tools; choose another provider for strict tool isolation, '
                     'or explicitly pass --allow-native-tools only for a trusted same-account environment (not an OS sandbox)')
    if not args.model.strip():
        parser.error('model must be explicit')
    if len(set(args.case_ids)) != len(args.case_ids) or any(not re.fullmatch(r'Q\d{2}', c) for c in args.case_ids):
        parser.error('case IDs must be unique Qxx identifiers')
    endpoint = urlparse(args.fusion_url)
    if endpoint.scheme not in ('http', 'https') or endpoint.hostname not in ('localhost', '127.0.0.1', '::1') or endpoint.username or endpoint.password:
        parser.error('fusion URL must be a loopback HTTP(S) URL without embedded credentials')
    root = Path(__file__).resolve().parents[1]
    config = {
        'provider': args.provider, 'model': args.model,
        'execution_backend': 'unified_gateway',
        'cli_executable': str(args.cli_executable.resolve()),
        'python': str(Path(sys.executable).resolve()), 'evaluator_directory': str(root),
        'questions_root': str(args.questions_root.resolve()), 'private_cases': str(args.private_cases.resolve()),
        'runs_root': str(args.runs_root.resolve()), 'private_runs_root': str(args.private_runs_root.resolve()),
        'fusion_skill': str(root / 'agent/fusion-modeling/SKILL.md'),
        'fusion_connection': {'url': args.fusion_url, 'enabled': True},
        'case_ids': args.case_ids, 'strict_tool_isolation': not args.allow_native_tools,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print('Configuration created. Verify recipient paths and endpoint before running.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
