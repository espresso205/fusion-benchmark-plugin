# Candidate skills and MCP selection

Use the maintainer-owned registry template
[candidate-capabilities.template.json](../examples/candidate-capabilities.template.json)
and select registered IDs, separately from the CLI profile. The base benchmark
control protocol, budget, audit, submission and revocation remain enabled.

```text
--capability-registry <file> --no-skills --mcps cad_native
--capability-registry <file> --skills <id> [<id> ...] --mcps cad_native [<id> ...]
--capability-registry <file> --no-skills --no-task-mcps --study-kind negative_control_no_cad
```

Entering explicit selection requires the registry, a skills choice and an MCP
choice together. `--skills` conflicts with `--no-skills`; `--mcps` conflicts with
`--no-task-mcps`. There is no ambiguous `--no-mcp` flag. Empty skills remain empty.
Ordinary modeling requires `cad_native`; an empty MCP list is accepted only for
the explicitly labeled negative control. List order is preserved and hashed.

The full management `start_run.candidate_capabilities` object contains exactly:

```json
{
  "schema": "candidate-capabilities/1",
  "study_kind": "capability_ablation",
  "skill_delivery": "gateway_read",
  "exposure_mode": "gateway_proxy",
  "skills": [],
  "mcp_servers": ["cad_native"]
}
```

All six fields are required. Management accepts registered IDs, never arbitrary
commands, URLs, filesystem skill paths or credentials. IDs must be unique and
known; dependencies are validated rather than automatically enabled. Sources and
static resources are validated and later frozen privately with hashes. The
registry is never exposed to candidates. Current modes are `gateway_read` and
`gateway_proxy`; unsupported modes fail instead of silently changing behavior.

Omitting the entire object preserves legacy single-skill behavior and records
`selection_origin: legacy_default`. Direct configuration combining the object
with any `cad_skill`, `modeling_skill` or `fusion_skill` field is rejected, even
for empty values. An explicit management override first removes those fields from
a new derived config and records `legacy_skill_fields_removed_by_explicit_override`.
The base file is unchanged; resolving the derived config again does not restore
the removed fields. The generator never emits legacy fields in explicit mode.

Optional auxiliary MCP tools are exposed through the controlled benchmark Gateway
under a namespace. This compares availability through Gateway, not the CLI's native
direct registration behavior. Disabled skills have no candidate read path;
disabled auxiliary services are not started or connected. Static selection records
intent only. Actual tools/list, skill reads, auxiliary calls and cleanup evidence
are recorded separately; enabled but unused is distinct from disabled.

The registry template intentionally contains only builtin `cad_native` and no
optional skills or auxiliary servers. Add reviewed public skill closures and
registered read-only stdio services locally. Credentials and private references,
ground truth, other tasks' answers and historical results must remain outside
candidate resources. Ordinary preflight validates statically and probes CLI
help/version only; it does not connect auxiliary services or run CAD/model smoke.
