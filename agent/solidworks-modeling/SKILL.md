---
name: solidworks-modeling
description: Use when a benchmark worker is assigned SolidWorks native Part modeling through the controlled CAD Gateway with public TASK, DXF, PNG or JPEG inputs.
---

# SolidWorks Part modeling

Build a self-contained native Part using this attempt's Gateway. The contract fixes the CAD backend, workspace, native filename, time and tool budgets. It permits one task document; preserve unrelated user documents. Single-body and multi-body Parts are supported subject to independent geometry checks. Assemblies and external references are outside this contract.

Call `get_contract` and `list_inputs`, then `read_input(name="cad_skill")` and read the task. Read every declared attachment in order using `read_image(id=...)` for PNG/JPEG and `parse_dxf(id=...)` for DXF. Record input IDs/hashes and associate dimensions with their annotation/view. Engineering dimensions and units are authoritative; pixels are not millimetres. Disclose unreadable dimensions, conflicts and assumptions in the report. Unsupported or unreadable required evidence is a failure.

Call `cad_tools` for the actual allowed native schemas. Use `cad_call({"tool":"<discovered name>","arguments":{...}})` for all modeling. The underlying bridge has more tools than this contract allows. Follow returned schemas, including required parameters, exact sketch names and explicit units. `create_new_part` starts the owned Part. No external CAD connection, Python execution, imports, assemblies or global setting changes are available through this route.

## Parameter authorization

Before each planned group of mutating calls, use native `validate_parameter_plan` through `cad_call`. Include `objective`, `parameters`, `assumptions`, `output_requirements`, and ordered `control_operations` with exact `tool` and `arguments`. Each parameter needs `name`, concrete `value`, `unit`, `basis`, `role`, `decision`, and `source_ref`.

| Source | Required mapping |
|---|---|
| Explicit public task or annotation | `role: user_input`, `decision: user_specified`, `source_ref: public:<input-id>#<annotation-location>`; include that exact input's complete SHA256 from `list_inputs` in `basis`, together with the dimension and unit. The annotation location must be nonempty. |
| Delegated implementation choice or disclosed assumption | Only when `get_contract.modeling_authorization.delegated_assumptions` is true: `role: agent_suggestion`, `decision: delegated`, `source_ref: benchmark:delegated`. Explain the choice and include the complete public TASK SHA256 or `get_contract.contract_sha256` in `basis`. |
| Derived value | `role: derived_constraint`, `decision: derived_from_ready`, `depends_on` naming earlier ready parameters, `formula`, and `source_ref: derived:<rule>`. |

For assumptions include `name`, `basis`, `decision`, and the corresponding `source_ref`. Delegation permits necessary implementation choices and clearly disclosed assumptions; it does not override explicit dimensions or turn guessed dimensions into observed facts. Do not claim `user_accepted` without a real acceptance channel. Private reference geometry, scores and other attempts are never parameter sources. If a necessary assumption lacks authorization, record the constraint and stop that construction.

For example, the exact UTF-8 task text `Create one part, radius 3 mm.` without a trailing newline has the SHA256 shown below. These provenance entries cite that example task; replace its values, annotation location and hash with evidence returned for your actual task. An attachment dimension must use that attachment's own hash, not the TASK hash.

```json
{
  "parameters": [{
    "name": "radius", "value": 3, "unit": "mm",
    "role": "user_input", "decision": "user_specified",
    "source_ref": "public:task#line 1, radius 3 mm",
    "basis": "TASK line 1 explicitly specifies radius 3 mm; SHA256=c25384f990eb38b376bf9113840001a1e42717a32a3c1c1f022e6ea3ee2e4191"
  }],
  "assumptions": [{
    "name": "sketch_plane", "value": "Front", "decision": "delegated",
    "source_ref": "benchmark:delegated",
    "basis": "Choose Front as an implementation plane under the disclosed task delegation; TASK SHA256=c25384f990eb38b376bf9113840001a1e42717a32a3c1c1f022e6ea3ee2e4191"
  }]
}
```

Example `control_operations` for a fresh Part (use the dimensions from your own task):

```json
[
  {"tool":"create_new_part","arguments":{}},
  {"tool":"create_sketch","arguments":{"plane":"Front"}},
  {"tool":"draw_circle","arguments":{"x":0,"y":0,"radius":3,"unit":"mm"}}
]
```

Check these argument names against the discovered schema before issuing the plan. Use the returned native `data.parameter_plan_token` as `parameter_plan_token` on each listed operation, in order. Do not include the token itself in the plan. Tokens expire and bind exact tools, values and ordering; only completed successful operations advance the plan. A changed parameter or expired token requires a revised plan. For a known completed failure, inspect its explanation before choosing a corrected plan or resolving the stated precondition. Never retry unknown execution or disable the parameter gate. Read and selection calls can occur between planned mutations; they are not control operations unless their schema requires a token.

## Save and submit

Inspect feature health, bodies and configurations through allowed native tools. Save with `save_document` to the absolute `<workspace>/submission.SLDPRT` path. Include that exact path in the parameter plan. First save must use a new target; subsequent saves are allowed only for the owned saved document and may require `confirm_overwrite: true`. Omitted-path saving is accepted only after the Gateway has recorded the owned saved path. Images use an explicit new `.png` path inside the workspace.

Use `list_configurations` to select the exact configuration to freeze. Save that configuration before closing; the Gateway binds the native `save_document` receipt's Part type, configuration, path, byte count and SHA256 to the actual file. A changed file or a submitted configuration different from the verified save is rejected. Close only the owned document using `close_document(save=false, document_title=<exact title>, expected_path=<absolute saved path>)`. Then `write_report` with input evidence, assumptions, actual configuration, construction/health observations and limitations. Call `submit({"configuration":"<exact saved name>"})`. Submission revokes every worker tool. A filename or file header does not establish native correctness: the maintainer independently reopens the frozen file, activates this configuration, verifies dependencies and health, and exports the trusted STEP. Candidate STEP files and dependency claims are not trusted scoring evidence.

Inspect the native `save_warning_code` before closing. Display-only save warnings 64, 128 and 256 are recorded and permit submission, including combinations; they do not establish geometry health. Known rebuild, dependency or model-data warnings block submission even when the native save completed successfully and advanced the plan. Correct the stated problem while the owned Part remains open, then save again. Unknown warning bits or context-dependent meanings preserve the pending state and require reconciliation; do not retry.

If any call reports unknown execution, timeout, cancellation, partial mutation or instance drift, stop CAD calls. Do not repeat the operation, kill CAD, close all documents or reconnect to bypass the pending state. Preserve the observed failure in `write_report` when still available. The same-account tool boundary does not revoke operating-system file access.
