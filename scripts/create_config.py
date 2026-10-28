"""Create recipient-local configuration without reading credentials or running tools."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlparse


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--provider', choices=('opencode', 'codex', 'claude'))
    parser.add_argument('--model')
    parser.add_argument('--cli-executable', type=Path)
    parser.add_argument('--candidate-profiles-file', '--candidate-profiles', dest='candidate_profiles', type=Path)
    parser.add_argument('--candidate-profile')
    reasoning_options = parser.add_mutually_exclusive_group()
    reasoning_options.add_argument('--reasoning-effort')
    reasoning_options.add_argument('--reasoning-variant')
    reasoning_options.add_argument('--reasoning-default', action='store_true')
    parser.add_argument('--reasoning-support', type=Path, help='Maintained support JSON; profile-ID mapping in profile mode')
    parser.add_argument('--capability-registry', type=Path)
    skills = parser.add_mutually_exclusive_group()
    skills.add_argument('--skills', nargs='+')
    skills.add_argument('--no-skills', action='store_true')
    mcps = parser.add_mutually_exclusive_group()
    mcps.add_argument('--mcps', nargs='+')
    mcps.add_argument('--no-task-mcps', action='store_true')
    parser.add_argument('--study-kind', choices=('capability_ablation', 'negative_control_no_cad'))
    parser.add_argument('--questions-root', type=Path, required=True)
    parser.add_argument('--private-cases', type=Path, required=True)
    parser.add_argument('--runs-root', type=Path, required=True)
    parser.add_argument('--private-runs-root', type=Path, required=True)
    parser.add_argument('--cad-backend', choices=('fusion', 'solidworks'), default='fusion')
    parser.add_argument('--fusion-url', help='Verified recipient-local Fusion MCP endpoint')
    parser.add_argument('--solidworks-bundle', type=Path, help='Recipient-installed SolidWorks stdio bundle; never copied')
    parser.add_argument('--launch-cad', action='store_true', help='Explicitly allow starting SolidWorks when no instance is running')
    parser.add_argument('--case-ids', nargs='+', default=['Q01'])
    parser.add_argument('--protocol', choices=('fusion-step-adapted-v3.0','fusion-step-adapted-v4.0'), default='fusion-step-adapted-v4.0')
    parser.add_argument('--coordinate-mode', choices=('geometry','strict'))
    parser.add_argument('--allow-native-tools', action='store_true',
                        help='Explicitly disable strict tool isolation; native tools remain available in the trusted same-account environment')
    args = parser.parse_args(argv)
    profile_mode = args.candidate_profile is not None or args.candidate_profiles is not None
    if profile_mode:
        if not args.candidate_profile or not args.candidate_profiles:
            parser.error('profile path requires --candidate-profiles-file and --candidate-profile')
        if any(value is not None for value in (args.provider, args.model, args.cli_executable)) or args.allow_native_tools:
            parser.error('profile selection is mutually exclusive with --provider/--model/--cli-executable/--allow-native-tools')
    elif any(value is None for value in (args.provider, args.model, args.cli_executable)):
        parser.error('legacy path requires --provider, --model and --cli-executable')
    explicit_capabilities = any((args.capability_registry, args.skills, args.no_skills,
                                 args.mcps, args.no_task_mcps, args.study_kind))
    if explicit_capabilities and (not args.capability_registry or not (args.skills or args.no_skills)
                                  or not (args.mcps or args.no_task_mcps)):
        parser.error('explicit capabilities require --capability-registry, skills/--no-skills and mcps/--no-task-mcps')
    reasoning = None
    if args.reasoning_default:
        reasoning = {'mode': 'cli_default'}
    elif args.reasoning_effort is not None or args.reasoning_variant is not None:
        reasoning = dict(mode='explicit', control='effort' if args.reasoning_effort is not None else 'variant',
                         value=args.reasoning_effort if args.reasoning_effort is not None else args.reasoning_variant)
    coordinate_mode = args.coordinate_mode or ('geometry' if args.protocol=='fusion-step-adapted-v4.0' else 'strict')
    if args.protocol=='fusion-step-adapted-v3.0' and coordinate_mode!='strict':
        parser.error('v3 requires strict coordinate mode')
    if args.provider == 'codex' and not args.allow_native_tools:
        parser.error('the Codex adapter cannot disable all native tools; choose another provider for strict tool isolation, '
                     'or explicitly pass --allow-native-tools only for a trusted same-account environment (not an OS sandbox)')
    if not profile_mode and not args.model.strip():
        parser.error('model must be explicit')
    if len(set(args.case_ids)) != len(args.case_ids) or any(not re.fullmatch(r'Q\d{2}', c) for c in args.case_ids):
        parser.error('case IDs must be unique Qxx identifiers')
    if args.cad_backend == 'fusion':
        if not args.fusion_url or args.solidworks_bundle or args.launch_cad:
            parser.error('fusion requires --fusion-url and rejects SolidWorks connection/launch options')
        endpoint = urlparse(args.fusion_url)
        if endpoint.scheme != 'http' or endpoint.hostname not in ('localhost', '127.0.0.1', '::1') or endpoint.username or endpoint.password:
            parser.error('fusion URL must be a loopback HTTP URL without embedded credentials')
    elif args.fusion_url:
        parser.error('solidworks rejects conflicting --fusion-url')
    root = Path(__file__).resolve().parents[1]
    config = {
        'cad_backend': args.cad_backend,
        'protocol': args.protocol, 'coordinate_mode': coordinate_mode,
        'execution_backend': 'unified_gateway',
        'python': str(Path(sys.executable).resolve()), 'evaluator_directory': str(root),
        'questions_root': str(args.questions_root.resolve()), 'private_cases': str(args.private_cases.resolve()),
        'runs_root': str(args.runs_root.resolve()), 'private_runs_root': str(args.private_runs_root.resolve()),
        'case_ids': args.case_ids, 'strict_tool_isolation': not args.allow_native_tools,
    }
    if profile_mode:
        # Direct script invocation keeps the source root importable without installation.
        sys.path.insert(0, str(root))
        from fusion_pipeline.candidate_profiles import load_candidate_profiles
        try:
            config.update(load_candidate_profiles(args.candidate_profiles))
            config.pop('schema')
            config['candidate_profiles_source'] = str(args.candidate_profiles.resolve())
            config['candidate_profiles_source_sha256'] = hashlib.sha256(args.candidate_profiles.read_bytes()).hexdigest()
        except (ValueError, OSError) as error:
            parser.error(str(error))
        config['candidate_profile'] = args.candidate_profile
        config.pop('strict_tool_isolation')
    else:
        config.update(provider=args.provider, model=args.model,
                      cli_executable=str(args.cli_executable.resolve()))
    if args.reasoning_support:
        try:
            config['candidate_reasoning_support' if profile_mode else 'reasoning_support'] = json.loads(
                args.reasoning_support.read_text(encoding='utf-8-sig'))
        except (ValueError, OSError) as error:
            parser.error(str(error))
    if args.cad_backend == 'fusion':
        config.update(fusion_connection={'url': args.fusion_url, 'enabled': True})
        if not explicit_capabilities:
            config['fusion_skill'] = str(root / 'agent/fusion-modeling/SKILL.md')
    else:
        config['solidworks'] = {'launch_if_needed': args.launch_cad}
        if args.solidworks_bundle:
            config['solidworks']['bundle'] = str(args.solidworks_bundle.resolve())
        if not explicit_capabilities:
            config['modeling_skill'] = str(root / 'agent/solidworks-modeling/SKILL.md')
    if args.allow_native_tools:
        config['native_tools_authorization'] = {'source': 'explicit --allow-native-tools',
                                              'scope': 'trusted same-account execution; not an OS sandbox'}
    if explicit_capabilities:
        config['capability_registry'] = str(args.capability_registry.resolve())
        config['candidate_capabilities'] = dict(schema='candidate-capabilities/1',
            study_kind=args.study_kind or 'capability_ablation', skill_delivery='gateway_read',
            exposure_mode='gateway_proxy', skills=args.skills if args.skills is not None else [],
            mcp_servers=args.mcps if args.mcps is not None else [])
    sys.path.insert(0, str(root))
    from fusion_pipeline.candidate_config import resolve_candidate_configuration
    try:
        config = resolve_candidate_configuration(config, {'reasoning': reasoning} if reasoning else None)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print('Configuration created. Verify recipient paths and endpoint before running.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
