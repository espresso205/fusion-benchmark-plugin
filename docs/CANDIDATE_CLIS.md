# Candidate CLI profiles

`provider` selects the CLI (`claude`, `codex`, `opencode`), not the model API vendor.
Register complete launch profiles in a maintainer-owned `candidate-profiles/1` JSON
file and select one with `candidate_profile`. The non-sensitive starting template is
[candidate-profiles.template.json](../examples/candidate-profiles.template.json).
Replace its executable/model placeholders and independently establish any native
tool authorization before use. Template paths intentionally do not exist.

The generator has two mutually exclusive paths:

```text
--candidate-profiles-file <file> --candidate-profile <id>
--provider <cli> --model <model> --cli-executable <absolute-path>
```

Both paths also require `--output`, public/private input and run paths, and the
selected CAD backend's connection options. The profile path rejects legacy launch
flags and `--allow-native-tools`; isolation and authorization come from the profile.
The legacy Codex path requires `--allow-native-tools`. No path is an OS sandbox.
Selecting a profile replaces executable, model, authentication reference, isolation,
expected model and reasoning together. A missing optional field never inherits from
the previous profile. An invalid explicit executable cannot fall back to PATH.
Legacy cross-provider overrides are rejected; register a complete profile instead.

Every new profile requires `reasoning: {"mode": "cli_default"}` or an explicit
`{"mode":"explicit","control":"effort","value":"high"}` request. OpenCode uses
`control: "variant"`. Explicit requests require maintained evidence for the exact
CLI/model/version and allowed values. Offline generation and static inspection may
save an unverified request as a draft: requested reasoning is retained,
`support_status` is `not_verified`, `supported_values` and resolved reasoning are
null, and native parameters remain empty. Unsupported or invalid requests still
fail. Formal starts reject unverified requests before a coordinator process starts.
Same-name levels across CLIs do not imply
equal computation.

| CLI | Native control | Strict native tool exclusion |
| --- | --- | --- |
| Claude Code | `--effort <value>` | supported by adapter |
| Codex | `-c model_reasoning_effort="<value>"` | unsupported; explicit authorization required |
| OpenCode | `--variant <value>` and reviewed variant options | supported by adapter |

Maintain `candidate_reasoning_support` as a mapping keyed by profile ID. Each value
contains `provider`, `model`, `cli_version`, `status`, `controls`,
`evidence_sha256`, `resolved_executable`, and `executable_sha256`. The executable
path and hash are private maintained evidence, bound by the runner to the actual
resolved binary before a candidate starts. OpenCode also needs reviewed
`variant_options` and their hashes.
Only genuinely verified support may use `status: "verified"`. Legacy unprofiled
configuration uses `reasoning_support` instead. Evidence from another profile is
never inherited. `--reasoning-support <file>` loads the profile-ID mapping in profile
mode and a single support object in legacy mode. No verified support fixture is
shipped as a production template.

A temporary reasoning override uses `start_run.reasoning` or generator
`--reasoning-effort <value>` or `--reasoning-variant <id>`. These switches and
`--reasoning-default` are mutually exclusive. Use `--reasoning-default` to remove an override. Overrides change the
profile digest and retain their origin across a saved config reload. A profile ID
and provider/model overrides cannot be combined. Formal experiments must freeze a
separate final profile configuration for each setting.

`candidate_config.resolve_candidate_configuration(config, overrides=None, *, require_reasoning_verified=False) -> dict`
is the shared static entry point. It returns a deep copy containing normalized
top-level launch fields, `candidate_capability_selection`, `resolved_reasoning`,
and public `candidate_selection`. Reapplying it is idempotent. It reads public
capability sources but never credentials or subprocesses. Execution callers must
set `require_reasoning_verified=True`; management `start_run` and runner do so.
The selected support is
also copied to `reasoning_support` for the adapter. Only explicit management
capability overrides remove legacy skill fields.

Management `preflight` validates statically, then runs only selected executable
version/help probes. It does not start model, CAD or auxiliary MCP sessions. Public
selection/status/results omit executable, registry and authentication paths;
actual model/reasoning observations remain null until separately observed. Stale
support detected by the CLI-version probe preserves the selected draft request and
marks it `not_verified`. A help
flag, successful process exit or generated parameter is not model verification.

The installed CLI help/version evidence recorded on 2026-10-28 found Claude Code
2.1.218, Codex CLI 0.162.1 and OpenCode 1.19.3 with required adapter/reasoning flags.
Authentication was not checked; model-specific reasoning support and live model/CAD
behavior were not verified. These observations are not a support table for any
production model.

Generated experiment cell configurations run through the direct CLI entry point
in this release. Managed `start_run` does not accept arbitrary frozen-plan fields
or replace a frozen cell configuration; plan admission remains strict.
