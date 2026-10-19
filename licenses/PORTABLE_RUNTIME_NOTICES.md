# Portable runtime notices

The portable release contains CPython, the dependency distributions pinned in `requirements-portable-plugin.lock.txt`, native libraries owned by those distributions, and selected CADGenBench evaluator source. `runtime.lock.json` records the assembled dependency graph. `PACKAGE_MANIFEST.json` and `SHA256SUMS` bind the distributed file bytes; they do not establish independent upstream authenticity.

The builder copies owned license/NOTICE/COPYING/COPYRIGHT files into `licenses/distributions/` and retains them in the original package locations. `licenses/inventory.json` maps distributions to those texts and their declared license metadata. Metadata may not identify every embedded native component. Read the complete notices shipped inside native packages, especially Open3D, VTK, NumPy and SciPy.

CPython's license is copied to `licenses/Python.txt`. The CADGenBench license remains at `vendor/cadgenbench_LICENSE`. The cadquery-ocp wrapper's Apache license does not replace Open CASCADE Technology's LGPL 2.1 license and additional exception; those texts are included separately as `occt-LICENSE_LGPL_21.txt` and `occt-OCCT_LGPL_EXCEPTION.txt`, obtained from the OCCT V7_8_1 source release. OCCT source: https://github.com/Open-Cascade-SAS/OCCT/tree/V7_8_1 . Wrapper source: https://github.com/CadQuery/OCP .

Native library files remain as separate DLL/PYD files and are not sealed inside a single executable. Applicable upstream license terms, source availability and replacement/relinking rights remain in force. The runtime inventory is a technical bill of materials, not a claim that a legal review has been performed.

Autodesk Fusion, its add-in/bridge, CLI agents, model-provider software and credentials are not included. No Autodesk login or model account is redistributed.
