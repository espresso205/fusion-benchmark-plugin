---
name: fusion-modeling
description: Build and inspect native Autodesk Fusion models through an existing Fusion MCP bridge, including benchmark worker sessions that receive public TASK/DXF inputs and submit an F3D archive.
---

# Native Fusion modeling

Use the existing Autodesk Fusion desktop and MCP bridge. This skill is guidance, not a bridge installer. Read the question's units, dimensions, permitted tools, output directory and time/call budget before editing. Work only in your own task document; preserve unrelated unsaved user work.

## Benchmark worker route

Call `get_contract`, then `read_input` for `TASK.md` and `fusion_skill`; use `parse_dxf` for the public drawing. `fusion_tools` returns the current schemas for the allowed Fusion tools. All Fusion calls go through `fusion_call` with the discovered tool name and argument object. Do not infer schema fields from a previous machine or hardcode an MCP port.

The currently supported bridge tool names are `fusion_mcp_read`, `fusion_mcp_execute`, `fusion_mcp_update`, and `fusion_mcp_electronics_read`; availability still depends on the installed bridge. A mechanical benchmark normally uses the first three. Read their returned schemas and errors before selecting operations.

For ordinary authorized Fusion work outside a worker gateway, initialize the configured MCP connection and list tools directly. Use the recipient's configured endpoint; there is no fixed machine-independent port. Authentication, licensing and first-run dialogs remain with the user.

## In-process API

Fusion's `adsk` modules run inside Fusion. Never import them in the host Python interpreter. For the execute tool, use the schema's expected script form; a common entry point is `def run(_context: str)`. Keep scripts short enough to inspect their results. An inspection example is:

```python
import adsk.core
import adsk.fusion

def run(_context: str):
    app = adsk.core.Application.get()
    design = adsk.fusion.Design.cast(app.activeProduct)
    return {
        "has_design": design is not None,
        "body_count": design.rootComponent.bRepBodies.count if design else 0,
        "document_name": app.activeDocument.name if app.activeDocument else None,
    }
```

Respect Fusion's internal length units (centimetres) when using numeric geometry APIs. Use explicit unit strings/UnitsManager where supported, and record conversions from the drawing. Construct geometry with Fusion native sketches/features/BRep APIs, inspect solids and feature health incrementally, and record dimensions actually measured. A rendered image alone does not prove solid validity.

## Submission and stopping

Export the task's native archive as `submission.f3d` in the allowed submission directory. Use Fusion's archive export options for the root component, check the export return status and file existence, and close only your own task documents when the contract requires it. Record external references and dependency evidence rather than assuming an archive is self-contained.

In a benchmark worker, write the result with `write_report`, then call `submit`. Submission freezes the attempt and revokes further worker tools. Do not read GT, private annotations, other candidates or scoring feedback. The trusted coordinator independently reopens the frozen archive and exports STEP; a candidate-created STEP is not trusted export evidence.

If a Fusion request times out or its execution status is unknown, stop issuing Fusion operations and report the request evidence. Do not retry the same creation/export or restart Fusion to conceal an unresolved operation. A terminated CLI does not establish that Fusion stopped executing.
