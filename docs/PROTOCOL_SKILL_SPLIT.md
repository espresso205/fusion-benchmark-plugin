# Protocol / optional knowledge split

`cad-worker/1` and both legacy `agent/*-modeling/SKILL.md` files retain their original bytes and aliases. Explicit `cad-worker/2` uses `agent/common-protocol` and independently registered optional `agent/fusion-guidance` / `agent/solidworks-guidance`. The shared protocol hash frames each filename, byte length and raw content in PROTOCOL.md, SOLIDWORKS.md, FUSION.md order. All capability groups use the same protocol files and hash; only the selected backend text is delivered.

The following paragraph map uses legacy headings and paragraph opening words as stable source locators. Rewritten or new advice is identified explicitly. Code examples are not copied into the common protocol.

| New paragraph | Source and disposition |
|---|---|
| PROTOCOL 1: Call get_contract | SolidWorks opening “Call get_contract”; Fusion Benchmark worker route “Call get_contract” and “Use annotated engineering dimensions”. Input reading, hashes, dimensions, units and unreadable-input failure are required interfaces. Optional skill aliases removed. |
| PROTOCOL 2: Use only | SolidWorks opening “Build a self-contained”; native schema paragraph “Call cad_tools”; Fusion opening “Use the existing” and Submission “In a benchmark worker”. Preserve document ownership and private-input prohibition. |
| PROTOCOL 3: The contract fixes | Both legacy opening budget statements and Submission write_report instructions. Explicitly documents the already shared controlled-call budget, including optional reads/calls. |
| PROTOCOL 4: Unknown CAD | Both legacy final stopping paragraphs; new explicit distinction for read-only auxiliary failures. |
| SOLIDWORKS 1: Use cad_tools | Legacy “Call cad_tools”: exact schema, units, Part-only and ownership requirements. |
| SOLIDWORKS 2: Before each group | Legacy Parameter authorization opening paragraph, source-mapping table and “For assumptions”. Exact required parameter/provenance fields preserved without the optional example plan. |
| SOLIDWORKS 3: Take the native | Legacy “Check these argument names”: exact token, order, expiration, success advancement and stop requirements. |
| SOLIDWORKS 4: Save with | Legacy Save and submit “Inspect feature health” path/overwrite instructions, “Use list_configurations” binding requirements and “Inspect the native save_warning_code”. Health-check advice separated; path/configuration/warning admission rules retained. |
| SOLIDWORKS 5: Close only | Legacy “Use list_configurations” close/report/submit and independent-reopen requirements. |
| FUSION 1: Use cad_tools | Legacy Benchmark worker route schema and maintained tool paragraphs; In-process API “Fusion's adsk” and “Respect Fusion's internal length units”. New generic v2 cad_tools/cad_call names replace old routing names. No inspection example copied. |
| FUSION 2: Export the owned | Legacy Submission “Export the task's native archive”, “In a benchmark worker” and final unknown-status paragraph. Native save, ownership and trusted export requirements preserved. |
| SolidWorks optional 1: Read orthographic | New optional construction strategy; absent from common protocol. |
| SolidWorks optional 2: Inspect feature health | Legacy Save opening health/body/configuration inspection advice; continuity/dimension prioritization is new optional advice. |
| SolidWorks optional 3: When a known | Legacy parameter-token paragraph known-failure advice, expanded with optional geometric simplification. Required known-vs-unknown outcome distinction stays in protocol. |
| Fusion optional 1: Combine all | Legacy input paragraph “Combine all views”, expanded with new optional feature-sequence advice. Required input consumption and provenance stay in protocol. |
| Fusion optional 2: Keep execute | Legacy In-process API “Keep scripts short” and “Construct geometry ... inspect ... incrementally”; rendering limitation preserved as advice. |
| Fusion optional 3: Inspect body | Legacy In-process API measurement advice, expanded with optional check prioritization. |

The no-skill condition has no optional skill metadata/body/resources or mandatory skill-read instruction. Protocol files provide only task/control requirements and required native interface conventions. The optional files contain advice, never executable dependencies. Native capability removal is an explicitly labeled negative control: no CAD tools or backend instructions, and native submit is rejected. Tools/list observations prove the response supplied to the CLI, not that a model backend consumed it.
