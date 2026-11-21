# V5 assembly validity and isolated measurements

Protocol `fusion-step-adapted-v5.0`, schema `5.0`, opt-in. Historical v4 remains the default and retains its original code and semantics. `ranking_eligible=false` in every v5 result.

```powershell
python -B evaluate_v5.py --cases C:/benchmark/cases.json --output C:/benchmark/new-v5 --coordinate-mode geometry --timeout 600 --stage-timeout 90 --workers 4
python -B evaluate.py --protocol fusion-step-adapted-v5.0 --cases C:/benchmark/cases.json --output C:/benchmark/new-v5
```

Every output must be a new directory. The explicit evaluator path, Python executable, dependency versions, source hashes, immutable input SHA256, annotation hash, acceptance hash and weights hash are recorded. Bundled embedded Python can prefer installed packages: both v5 entrypoints explicitly import their own checkout. Tests with that runtime must prepend the checkout to `sys.path` before discovery.

## Contract and gates

Stages use `pass`, `fail`, `error`, `timeout`, `partial`, `not_run`. Results separately expose `candidate_basic`, `candidate_assembly`, `reference_preflight` and `measurements`. Basic validation checks transferable STEP, solids, residual geometry, BRep validity, shell closure, finite coordinates and positive volume. Self-intersection and positive-volume interferences are separate checks on the original candidate. A strict failure cannot erase successful geometry measurements.

The three geometric measurements retain weights shape 6/13, dimension 5/13, mass 2/13. Shape and dimension call unchanged v4 algorithms. Geometry mode requires the single proper whole-model rigid transform and verified derived STEP hash. There is no per-part ICP, scale or reflection. Strict mode measures in the original world frame. A failed or incomplete metric has `measured_score=null`; a true measured zero remains zero.

`final_score` exists only when all three measurements are valid, candidate basic passes, frozen reference acceptance passes, reference basic passes, actual GT dimension self-check passes, and input/source/derived identities agree. The value is explicitly `complete_geometric_similarity`; it does not certify assembly. `partial_lower_bound` is a separately named diagnostic missing-credit floor. It never enters `batch_mean`. The exact complete case IDs, candidate failure rate, basic inconclusive rate, assembly pass rate and measurement completion rate remain visible, including failed cases. Integrity failure revokes both total and lower bound.

## Frozen reference acceptance

Manifest rows retain candidate/gt/annotations and their three expected hashes. Add optional absolute `reference_acceptance` and its `reference_acceptance_sha256`. An omitted file yields `not_run`, never reference pass. Acceptance JSON:

```json
{"status":"pass","gt_sha256":"<sha256>","annotations_sha256":"<sha256>",
 "checks":{"inventory":{"status":"pass"},"configuration":{"status":"pass"},
 "dimensions":{"status":"pass"},"self_intersection":{"status":"pass"},"interference":{"status":"pass"}},
 "unresolved_issues":[]}
```

All five checks and the top level must pass, with an explicitly empty unresolved list. A known actual basic or dimension-reference failure is exposed as fail even if frozen acceptance is absent, with acceptance evidence preserved separately; partial/error/timeout/not_run is not promoted. The CLI also performs reference basic checking and the dimension module's actual GT self-check. Frozen acceptance is a maintainer attestation bound to data, not a substitute for drawing review. Diagnostics may run against a pending reference; no formal score is released.

## Isolation, timing and mass

Each stage is a fresh process. The supervisor kills the full process tree on Windows with `taskkill /T /F` and uses process-group kill on POSIX. Per-stage and total budgets are independent; unstarted stages after the total budget are `not_run`. Core order is basic, reference basic, pose, shape, dimension, mass; matching, self-intersection, interference and fit follow. A mass/self/Common timeout does not block later independent stages while total budget remains. Each stage result is atomically published. Strict progress snapshots atomically replace previous progress, preserving checked counts, findings and unchecked entity/pair lists. Events record import, entity integration, self, exact Common and fit boundaries. Logs and progress survive crashes and timeouts.

V5 mass intentionally changes the numerical backend: integrate each physical instance at adaptive Gauss epsilon 1e-6, 1e-8, 1e-10, 1e-12, then 1e-14, combine `GProp_GProps.Add` with parallel-axis translation, and require the same v3/v4 relative convergence budget 1e-6 for volume/inertia and the same centroid criterion. Distinct coincident physical parts contribute separately. No full-shape Gauss–Kronrod is called: a stalled full-shape integration must not prevent the independent measurements from running. Unconverged v5 mass remains null with diagnostic estimate and per-instance evidence. Convergence differences are empirical, not rigorous error bounds. Historical `robust_metrics.py` and its mass semantics are byte-preserved.

Cross-run caches are disabled. This avoids stale acceptance, coordinate and instance-transform reuse. GT import/meshing cache optimization is deferred until a key covers input/annotation/source/protocol/OCP hashes and all numerical, mesh and fit parameters. No mesh broad-phase collision approximation is introduced: exact bbox rejection plus STEP Common retains complete containment cases. An exact sweep-and-prune index rejects separated/touching boxes while retaining complete containment; candidate Common pairs are indexed and unchecked pairs remain explicit. Worst-case overlapping models still require quadratic exact Common work.

## Physical instances and matching

XCAF leaf instances retain definition IDs, full instance paths and composed transforms. Explicit multi-body physical leaves remain one matched part with separately identified constituent solids for strict checks. Flat products with no physical assembly structure expose each solid as an explicit fallback component. XCAF can create pseudo-components for representation aliases: aliases collapse only within a source PRODUCT that has no outgoing STEP NAUO physical child relation and contains exactly one identical BRep. The collapse evidence is recorded; separate product occurrences, including coincident duplicates, remain distinct.

Matching uses volume, area and centroid principal moments, followed by globally optimal one-to-one assignment with explicit unmatched dummies. Relative descriptor tolerance 0.15 is a matching heuristic, not an engineering fit tolerance. Centroid displacement only breaks ties; it is always reported after the one whole-model alignment. No surface or volume weighting hides missing small parts. Missing, extra, duplicate candidates, count coverage and single-solid incompleteness are reported. Descriptor collisions and symmetric parts can remain ambiguous; this matcher does not prove semantic BOM identity.

## Frozen fit rules

Rules live in hashed annotations under `assembly_v5`. They use exact `reference_ids` emitted by the reference inventory. Candidate identities are obtained through matching. No rules means `fit=not_run` and prevents complete assembly pass. Progress is saved before each expensive operation; position and fit checks share one counted unit and unfinished rule IDs remain explicit. A supported nonempty rule set certifies only its explicit scope, not unlisted connections.

- Optional `position_tolerance_mm`: tests each matched centroid after the single whole-model alignment.
- `fits`: each has `id`, `reference_ids:[A,B]`, and one of:
  - `type:surface_gap`, `minimum_mm`, `maximum_mm` (exact BRep distance).
  - `type:expected_connection` or `forbidden_connection`, `contact_tolerance_mm`.
  - `type:axis_alignment`, `cylinder_radii_mm:[rA,rB]`, `selector_tolerance_mm`, `maximum_axis_distance_mm`, `maximum_angle_degrees`. Ambiguous cylinder selectors error. Axis direction sign is equivalent for cylindrical symmetry.
- `allowed_interferences`: each has `id`, `reference_ids:[A,B]`, `maximum_volume_mm3`, and a finite `allowed_bbox_mm:[xmin,ymin,zmin,xmax,ymax,zmax]` or `allowed_annulus` as below. All bounds must come from frozen drawing/fit evidence.

```json
{"id":"specified-fit","reference_ids":["instance-A","instance-B"],"maximum_volume_mm3":0.2,
 "allowed_annulus":{"axis_origin_mm":[0,0,0],"axis_direction":[0,0,1],"length_mm":10,
                    "inner_radius_mm":4.9,"outer_radius_mm":5.0}}
```

The exact intersection is transformed once into the reference frame. Bbox allowances require complete bbox containment. Annular allowances construct outer cylinder minus inner cylinder and subtract the allowed region from the intersection; residual volume must be at most max(1e-7 mm3, 1e-9 times intersection volume). This is the numerical check tolerance, not a drawing tolerance. For a physical pair with multiple constituent intersections, the exact union is formed before applying the volume cap once. Constituent evidence, cumulative union volume and the applicable cap are recorded; the full union must also satisfy the allowed region. Pending physical pairs remain explicit and cannot be granted an allowance on timeout or error. Pair, cap and region must all match. No spring/thread/seal class exemption exists. Unsupported regions (e.g. cones), ambiguous features or failed Boolean checks remain errors/unresolved, never implicit permission.

## Outputs and recovery

`summary.json`, each `aggregate.json`, CSV, Markdown and static HTML display the same separated statuses and null/lower-bound/complete values. The static v5 HTML is intentionally separate from historical v4 mesh reports. CLI success means the supervised evaluation completed; a reference-pending result can exit zero while still having null formal score. Consumers must inspect the versioned contract rather than equating successful process exit with a valid score.

Explicit `--protocol fusion-step-adapted-v4.0` (or previous source snapshot) restores historical behavior. In this plugin repository, `SCORING_SOURCE_MANIFEST_V4.json` preserves the pre-v5 source record and `SCORING_SOURCE_MANIFEST.json` records the release files. The standalone evaluator maintains its own source manifests separately.

Publication validation regenerates the producer’s `report.html` and checks every reconstructed aggregate field, frozen acceptance bytes and content, and summary rates. The plugin runner source identity includes the complete local import closure used by the verifier (`evaluate_v5.py`, `src/contract_v5.py`, `src/contract.py`, `src/stages_v5.py`, `src/io_utils.py`, and package initialization).
