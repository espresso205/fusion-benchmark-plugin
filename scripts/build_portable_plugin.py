"""Build a relocatable Windows x64 bundle from an explicitly selected Python.

The builder never copies a virtualenv. It reads installed distribution RECORDs,
checks their hashes and copies only the pinned dependency closure. The receiver
does not need Python, pip or build tools. No model or Fusion request is made.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import zipfile


REQUIRED_SOURCE_FILES = (
    "README.md", "AGENTS.md", "VERSION", "SCORING_SOURCE_MANIFEST.json",
    "docs/ARCHITECTURE.md", "docs/BUILD.md", "docs/DEPENDENCIES.md",
    "agent/fusion-modeling/SKILL.md", "examples/fusion_pipeline_config.json",
    "scripts/create_config.py",
    "analyze_ablation.py", "analysis/__init__.py", "analysis/result_reader.py",
    "analysis/statistics.py", "analysis/ablation.py", "analysis/reporting.py",
    "analysis/system_comparison.py", "docs/ABLATION.md",
    "evaluate.py", "worker.py", "weights.json", "weights_v3.json",
    "evaluate_v4.py", "worker_v4.py", "weights_v4.json", "docs/SCORING_V4.md",
    "src/contract_v4.py", "src/geometry_v4.py", "src/reporting_v4.py",
    "SCORING_SOURCE_MANIFEST_V3.json",
    "scheduler.py", "scheduler/__init__.py", "scheduler/__main__.py", "scheduler/core.py",
    "src/__init__.py", "src/io_utils.py", "src/metrics.py", "src/section.py",
    "src/face_match.py", "src/dimension_probes.py", "src/reporting.py", "src/contract.py",
    "src/contract_v3.py", "src/robust_metrics.py",
    "fusion_pipeline/__init__.py", "fusion_pipeline/__main__.py", "fusion_pipeline/runner.py",
    "fusion_pipeline/mcp.py", "fusion_pipeline/fusion_scripts.py", "fusion_pipeline/integration_smoke.py",
    "fusion_pipeline/cli.py", "fusion_pipeline/service.py", "fusion_pipeline/adapters.py",
    "fusion_pipeline/discovery.py", "fusion_pipeline/worker_gateway.py", "plugins/fusion-benchmark-guard.mjs",
    "fusion-benchmark.cmd", "fusion-benchmark-bootstrap.py", "requirements-evaluator.txt", "requirements-fusion-pipeline.txt",
    "requirements-portable-plugin.txt", "requirements-portable-plugin.lock.txt",
    "agent/fusion-benchmark/SKILL.md", "docs/PORTABLE_PLUGIN_USAGE.md",
    "docs/SCORING_V3.md", "licenses/PORTABLE_RUNTIME_NOTICES.md",
    "agent/templates/opencode.md", "agent/templates/codex.md", "agent/templates/claude.md",
    "scripts/build_portable_plugin.py",
    "vendor/cadgenbench_LICENSE", "vendor/cadgenbench_pyproject.toml",
    "vendor/cadgenbench/__init__.py", "vendor/cadgenbench/common/__init__.py",
    "vendor/cadgenbench/common/artifacts.py", "vendor/cadgenbench/common/mesh.py",
    "vendor/cadgenbench/common/measurements.py", "vendor/cadgenbench/common/validity.py",
    "vendor/cadgenbench/common/profiling.py", "vendor/cadgenbench/eval/__init__.py",
    "vendor/cadgenbench/eval/shape_similarity.py", "vendor/cadgenbench/eval/topo_match.py",
    "vendor/cadgenbench/eval/sampling.py", "vendor/cadgenbench/eval/booleans.py",
    "vendor/cadgenbench/eval/alignment.py",
)
OPTIONAL_SOURCE_FILES = ("src/fallback_metrics.py", "fusion_pipeline/provenance.py")
LICENSE_FILES = ("licenses/occt-LICENSE_LGPL_21.txt", "licenses/occt-OCCT_LGPL_EXCEPTION.txt", "licenses/Apache-2.0.txt", "licenses/UPSTREAM_SOURCES.json")
ROOT_RUNTIME_FILES = ("python.exe", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt")
NATIVE_STDLIB_FILES = {
    "libcrypto-3-x64.dll", "libssl-3-x64.dll", "libffi-8.dll", "sqlite3.dll",
}


def sha_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def safe_path(name):
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError("unsafe package path")
    path = PurePosixPath(name)
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}
    if path.is_absolute() or str(path) != name or ".." in path.parts or any(
        p.rstrip(" .") != p or p.split(".")[0].upper() in reserved or any(ord(c) < 32 for c in p)
        for p in path.parts
    ):
        raise ValueError("unsafe package path")
    return path


def checked_source(root, relative):
    root = Path(root).resolve()
    path = root.joinpath(*safe_path(relative).parts)
    if not path.is_file():
        raise ValueError(f"missing required source: {relative}")
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"source escapes root: {relative}")
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise ValueError(f"linked source rejected: {relative}")
    return path


def source_files(root):
    result = {name: checked_source(root, name) for name in REQUIRED_SOURCE_FILES}
    for name in OPTIONAL_SOURCE_FILES:
        if (Path(root) / name).exists():
            result[name] = checked_source(root, name)
    # Native dependency notices are generated separately from the source code.
    for name in LICENSE_FILES:
        if (Path(root) / name).exists():
            result[name] = checked_source(root, name)
    return result


INSPECT_SCRIPT = r'''
import importlib.metadata as m, json, platform, sys, sysconfig
from pathlib import Path
from packaging.requirements import Requirement
from packaging.markers import default_environment
from packaging.utils import canonicalize_name
requirements = json.loads(sys.argv[1])
pending = [Requirement(x) for x in requirements]
selected = {}
env = default_environment(); env['extra'] = ''
while pending:
    req = pending.pop(0)
    if req.marker and not req.marker.evaluate(env):
        continue
    key = canonicalize_name(req.name)
    dist = m.distribution(req.name)
    if req.specifier and not req.specifier.contains(dist.version, prereleases=True):
        raise ValueError('installed version does not match requirement: ' + str(req))
    if key in selected:
        continue
    if not dist.files:
        raise ValueError('missing RECORD for ' + req.name)
    selected[key] = {'name': dist.metadata['Name'], 'version': dist.version,
      'license': dist.metadata.get('License-Expression') or dist.metadata.get('License', ''),
      'requires': dist.requires or [],
      'files': [{'path': str(p).replace('\\','/'), 'size': p.size,
        'hash_mode': p.hash.mode if p.hash else None,
        'hash_value': p.hash.value if p.hash else None} for p in dist.files]}
    pending.extend(Requirement(x) for x in (dist.requires or []))
print(json.dumps({'version': platform.python_version(), 'implementation': platform.python_implementation(),
 'machine': platform.machine(), 'platform': sys.platform,
 'base': sys.base_prefix, 'site': sysconfig.get_path('purelib'),
 'stdlib_modules': sorted(sys.stdlib_module_names), 'distributions': [selected[x] for x in sorted(selected)]}))
'''


def load_requirements(path, seen=None):
    path = Path(path).resolve()
    seen = set() if seen is None else seen
    if path in seen:
        raise ValueError("requirements include cycle")
    seen.add(path)
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            rows.extend(load_requirements(path.parent / line[3:].strip(), seen))
        elif re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9.+!-]+", line):
            rows.append(line)
        else:
            raise ValueError(f"only exact pinned requirements are accepted: {line}")
    seen.remove(path)
    return rows


def inspect_environment(python, requirements):
    result = subprocess.run([str(python), "-I", "-c", INSPECT_SCRIPT, json.dumps(requirements)],
                            capture_output=True, text=True, encoding="utf-8", timeout=120)
    if result.returncode:
        raise ValueError("dependency inspection failed: " + result.stderr[-4000:])
    info = json.loads(result.stdout)
    if info["platform"] != "win32" or info["machine"].upper() not in {"AMD64", "X86_64"}:
        raise ValueError("portable target must be Windows x64")
    if info["implementation"] != "CPython" or not info["version"].startswith("3.12."):
        raise ValueError("portable target requires CPython 3.12")
    return info


def dependency_files(site, distributions):
    result = {}
    for dist in distributions:
        for row in dist["files"]:
            name = row["path"]
            # Installed wheel scripts contain interpreter paths; package data
            # outside site-packages is unnecessary for the evaluator runtime.
            if name.startswith("../"):
                continue
            path = safe_path(name)
            if "__pycache__" in path.parts or path.suffix == ".pyc" or path.name in {
                "direct_url.json", "INSTALLER", "REQUESTED", "RECORD"
            }:
                continue
            # Arbitrary .pth executable imports are never activated in this
            # isolated runtime. Only the explicit runtime search paths are used.
            if path.suffix == ".pth":
                continue
            source = checked_source(site, name)
            if row["size"] is not None and source.stat().st_size != row["size"]:
                raise ValueError(f"RECORD size mismatch: {dist['name']}/{name}")
            if row["hash_mode"]:
                digest = hashlib.new(row["hash_mode"], source.read_bytes()).digest()
                actual = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
                if actual != row["hash_value"]:
                    raise ValueError(f"RECORD hash mismatch: {dist['name']}/{name}")
            if name in result and result[name] != source:
                raise ValueError(f"duplicate dependency path: {name}")
            result[name] = source
    return result


def copy_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def copy_runtime(base, target, stdlib_modules, version_tag):
    base, target = Path(base), Path(target)
    target.mkdir(parents=True, exist_ok=False)
    required = ("python.exe", f"python{version_tag}.dll", "python3.dll", "vcruntime140.dll", "LICENSE.txt")
    for name in required:
        copy_file(checked_source(base, name), target / name)
    for name in ROOT_RUNTIME_FILES:
        if name not in required and (base / name).is_file():
            copy_file(checked_source(base, name), target / name)
    for name in sorted(set(stdlib_modules) | {"_sysconfigdata", "encodings"}):
        if name in {"sitecustomize", "usercustomize", "ensurepip", "idlelib", "tkinter", "turtledemo", "test", "venv"}:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_]+", name):
            continue
        file = base / "Lib" / (name + ".py")
        package = base / "Lib" / name
        if file.is_file():
            copy_file(checked_source(base, f"Lib/{name}.py"), target / "Lib" / file.name)
        elif package.is_dir():
            for source in sorted(package.rglob("*.py")):
                if "__pycache__" not in source.parts:
                    relative = source.relative_to(base).as_posix()
                    copy_file(checked_source(base, relative), target / relative)
        native = base / "DLLs" / (name + ".pyd")
        if native.is_file():
            copy_file(checked_source(base, f"DLLs/{name}.pyd"), target / "DLLs" / native.name)
    for name in NATIVE_STDLIB_FILES:
        if (base / "DLLs" / name).is_file():
            copy_file(checked_source(base, "DLLs/" + name), target / "DLLs" / name)
    # Windows _pth mode ignores PYTHONPATH, registry and user-site injection.
    # The root entry allows module execution from any caller working directory.
    # CPython 3.12 on Windows normalizes forward-slash ../.. without a
    # trailing separator incorrectly; native separators work after relocation.
    (target / f"python{version_tag}._pth").write_text(".\nLib\nDLLs\nLib/site-packages\n..\\..\n", encoding="ascii")


def write_licenses(bundle, info, site_files):
    license_dir = bundle / "licenses"
    license_dir.mkdir(exist_ok=True)
    copy_file(bundle / "runtime/python/LICENSE.txt", license_dir / "Python.txt")
    rows = []
    for dist in info["distributions"]:
        matching = []
        for row in dist["files"]:
            name = row["path"]
            leaf = PurePosixPath(name).name.lower()
            if name in site_files and any(word in leaf for word in ("license", "licence", "notice", "copying", "copyright")):
                dest = "licenses/distributions/" + re.sub(r"[^a-z0-9.-]", "-", dist["name"].lower()) + "/" + name
                copy_file(site_files[name], bundle / dest)
                matching.append(dest)
        source = "installed distribution metadata and owned files"
        if not matching and dist["license"].strip() == "Apache-2.0":
            matching.append("licenses/Apache-2.0.txt")
            source = "declared Apache-2.0 metadata; canonical license text supplied separately"
        if not matching:
            raise ValueError("missing license text for distribution: " + dist["name"])
        rows.append({"name": dist["name"], "version": dist["version"], "license_metadata": dist["license"],
                     "license_files": matching, "license_source": source})
    write_json(license_dir / "inventory.json", {"distributions": rows, "native_notice": "PORTABLE_RUNTIME_NOTICES.md"})


def write_manifest(bundle, runtime):
    bundle = Path(bundle)
    ignored = {"PACKAGE_MANIFEST.json", "SHA256SUMS", "INCOMPLETE"}
    files = {}
    for path in sorted(bundle.rglob("*")):
        if path.is_file():
            relative = path.relative_to(bundle).as_posix()
            if relative not in ignored:
                checked_source(bundle, relative)
                files[relative] = {"bytes": path.stat().st_size, "sha256": sha_file(path)}
    write_json(bundle / "PACKAGE_MANIFEST.json", {"schema_version": "fusion-portable-package/1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(), "runtime": runtime, "files": files})
    sums = {name: row["sha256"] for name, row in files.items()}
    sums["PACKAGE_MANIFEST.json"] = sha_file(bundle / "PACKAGE_MANIFEST.json")
    (bundle / "SHA256SUMS").write_text("".join(f"{value}  {key}\n" for key, value in sorted(sums.items())), encoding="utf-8")


def verify_bundle(bundle, *, allow_incomplete=False):
    bundle = Path(bundle)
    if (bundle / "INCOMPLETE").exists() and not allow_incomplete:
        raise ValueError("bundle build is incomplete")
    manifest = json.loads((bundle / "PACKAGE_MANIFEST.json").read_text(encoding="utf-8"))
    expected = set(manifest["files"]) | {"PACKAGE_MANIFEST.json", "SHA256SUMS"}
    actual = {p.relative_to(bundle).as_posix() for p in bundle.rglob("*")
              if p.is_file() and p.relative_to(bundle).as_posix() != "INCOMPLETE"}
    if expected != actual:
        raise ValueError("bundle file set differs from manifest")
    for name, row in manifest["files"].items():
        path = checked_source(bundle, name)
        if path.stat().st_size != row["bytes"] or sha_file(path) != row["sha256"]:
            raise ValueError(f"bundle hash or size mismatch: {name}")
    sums = (bundle / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    expected_sums = {name: row["sha256"] for name, row in manifest["files"].items()}
    expected_sums["PACKAGE_MANIFEST.json"] = sha_file(bundle / "PACKAGE_MANIFEST.json")
    if sums != [f"{digest}  {name}" for name, digest in sorted(expected_sums.items())]:
        raise ValueError("SHA256SUMS mismatch")
    return {"verified_files": len(manifest["files"]), "bytes": sum(r["bytes"] for r in manifest["files"].values())}


def build(source, python, output, archive=False):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError("output already exists; use a new destination")
    sources = source_files(source)
    for name in LICENSE_FILES:
        if name not in sources:
            raise ValueError("missing native license notice: " + name)
    info = inspect_environment(python, load_requirements(source / "requirements-portable-plugin.lock.txt"))
    site_files = dependency_files(info["site"], info["distributions"])
    output.mkdir(parents=True, exist_ok=False)
    (output / "INCOMPLETE").write_text("Build has not passed verification.\n", encoding="utf-8")
    for relative, path in sources.items():
        copy_file(path, output / relative)
    copy_runtime(info["base"], output / "runtime/python", info["stdlib_modules"], "312")
    for relative, path in site_files.items():
        copy_file(path, output / "runtime/python/Lib/site-packages" / relative)
    runtime = {"python_version": info["version"], "architecture": "windows-x64", "source": "RECORD-verified installed distribution closure",
               "distributions": [{k: d[k] for k in ("name", "version", "requires")} for d in info["distributions"]]}
    write_json(output / "runtime.lock.json", runtime)
    write_licenses(output, info, site_files)
    (output / "AGENTS.md").write_text("# Fusion Benchmark\n\nFor benchmark requests read `agent/fusion-benchmark/SKILL.md`. Use `fusion-benchmark.cmd`; no Python installation is needed. Private references belong only to the trusted coordinator.\n", encoding="utf-8")
    # Run from an unrelated cwd with deliberately invalid Python environment
    # variables, proving the interpreter uses only the package _pth entries.
    env = dict(os.environ, PYTHONHOME="Z:/not-an-installed-python", PYTHONPATH="Z:/not-a-site-packages", PYTHONDONTWRITEBYTECODE="1")
    check = subprocess.run([str(output / "runtime/python/python.exe"), "-B", "-c",
        "import sys, pathlib, numpy, scipy, OCP, vtk, build123d, trimesh, manifold3d, open3d, ezdxf; "
        "import fusion_pipeline.cli, src.metrics; "
        "root=pathlib.Path(sys.executable).resolve().parents[2]; "
        "assert all(pathlib.Path(p).resolve().is_relative_to(root) for p in sys.path); print('portable imports OK')"],
        cwd=output.parent, env=env, capture_output=True, text=True, encoding="utf-8", timeout=180)
    if check.returncode:
        raise ValueError("portable import check failed: " + check.stdout[-2000:] + check.stderr[-6000:])
    write_json(output / "BUILD_VERIFICATION.json", {"dependency_imports": "passed", "python_environment_ignored": True,
        "fusion_called": False, "model_called": False, "clean_machine_test": "not performed", "stdout": check.stdout.strip()})
    write_manifest(output, runtime)
    result = verify_bundle(output, allow_incomplete=True)
    (output / "INCOMPLETE").unlink()
    result["directory"] = str(output)
    if archive:
        path = output.parent / (output.name + ".zip")
        with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zipout:
            for file in sorted(output.rglob("*")):
                if file.is_file():
                    zipout.write(file, arcname=output.name + "/" + file.relative_to(output).as_posix())
        digest = sha_file(path)
        path.with_suffix(".zip.sha256").write_text(digest + "  " + path.name + "\n", encoding="utf-8")
        result.update(archive=str(path), archive_sha256=digest, archive_bytes=path.stat().st_size)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--python", type=Path, help="builder's approved CPython 3.12 x64 with pinned dependencies")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--zip", action="store_true")
    parser.add_argument("--verify", type=Path, help="verify existing bundle without modifying it")
    args = parser.parse_args(argv)
    if args.verify:
        result = verify_bundle(args.verify)
    elif args.python and args.output:
        result = build(args.source_root, args.python, args.output, args.zip)
    else:
        parser.error("provide --python and --output, or --verify")
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
