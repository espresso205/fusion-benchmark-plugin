"""Static capability selection and immutable snapshot boundary tests."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from fusion_pipeline import capabilities


class CandidateCapabilitiesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.skill_root = self.root / "guidance"
        self.skill_root.mkdir()
        (self.skill_root / "SKILL.md").write_bytes(b"Public modeling guidance.\n")
        (self.skill_root / "notes.txt").write_bytes(b"Check dimensions.\n")
        self.server = self.root / "lookup.py"
        self.server.write_text("print('fixture only')\n", encoding="utf-8")
        self.registry = {
            "schema": "candidate-capability-registry/1",
            "skills": {
                "guidance": {
                    "path": str(self.skill_root / "SKILL.md"), "version": "1",
                    "resources": [{"id": "notes", "path": "notes.txt",
                                   "sha256": hashlib.sha256(b"Check dimensions.\n").hexdigest()}],
                    "cad_backends": ["solidworks"], "requires_mcp": ["cad_native"],
                },
                "checklist": {
                    "path": str(self.skill_root / "SKILL.md"), "version": "2",
                    "resources": [], "cad_backends": ["fusion", "solidworks"], "requires_mcp": [],
                },
            },
            "mcp_servers": {
                "cad_native": {"kind": "builtin_cad_adapter"},
                "lookup": {
                    "kind": "stdio", "command": sys.executable, "args": [str(self.server)],
                    "source_files": [str(self.server)], "env_allowlist": ["PUBLIC_TOKEN"],
                    "allowed_tools": ["lookup_note"], "timeout_s": 30,
                    "max_response_bytes": 1048576, "side_effect_policy": "read_only",
                },
            },
        }

    def config(self, skills=None, mcps=None, **values):
        selection = {
            "schema": "candidate-capabilities/1", "study_kind": "capability_ablation",
            "skill_delivery": "gateway_read", "exposure_mode": "gateway_proxy",
            "skills": [] if skills is None else skills,
            "mcp_servers": ["cad_native"] if mcps is None else mcps,
        }
        selection.update(values)
        return {"candidate_capabilities": selection}

    def normalize(self, config=None, registry=None, backend="solidworks"):
        return capabilities.normalize_selection(
            self.config() if config is None else config,
            self.registry if registry is None else registry, cad_backend=backend)

    def test_explicit_empty_skills_never_falls_back(self):
        result = self.normalize()
        self.assertEqual(result["skills"], [])
        self.assertEqual(result["selection_origin"], "explicit")
        for key in ("cad_skill", "modeling_skill", "fusion_skill"):
            for value in (None, "", str(self.skill_root / "SKILL.md")):
                with self.subTest(key=key, value=value):
                    with self.assertRaisesRegex(ValueError, "legacy_skill_selection_conflict"):
                        self.normalize(dict(self.config(), **{key: value}))

    def test_response_limits_reject_outside_transport_range_before_snapshot(self):
        for limit in (1, 255, 32 * 1024 * 1024 + 1, 64 * 1024 * 1024):
            output = self.root / ('invalid-bound-snapshot-' + str(limit))
            registry = copy.deepcopy(self.registry)
            registry['mcp_servers']['lookup']['max_response_bytes'] = limit
            with self.subTest(limit=limit):
                with self.assertRaisesRegex(ValueError, 'invalid_mcp_response_limit'):
                    selected = self.normalize(self.config(mcps=['cad_native', 'lookup']), registry)
                    capabilities.freeze_capabilities(selected, registry, output)
                self.assertFalse(output.exists())

    def test_transport_response_boundaries_normalize_freeze_and_construct(self):
        from fusion_pipeline.aux_mcp import AuxiliaryMCPManager
        for limit in (256, 32 * 1024 * 1024):
            registry = copy.deepcopy(self.registry)
            registry['mcp_servers']['lookup']['max_response_bytes'] = limit
            selected = self.normalize(self.config(mcps=['cad_native', 'lookup']), registry)
            manifest = capabilities.freeze_capabilities(selected, registry, self.root / ('boundary-' + str(limit)))
            manager = AuxiliaryMCPManager(manifest, self.root / ('audit-' + str(limit)),
                                          time.monotonic() + 10, {})
            self.addCleanup(manager.close)
            self.assertEqual(manager.close()['servers'], [], 'static admission must not start a service')

    def test_missing_selection_preserves_legacy_without_registry_or_file_reads(self):
        config = {"cad_skill": "missing-historical-file.md"}
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("legacy read")):
            result = capabilities.normalize_selection(config, {}, cad_backend="fusion")
        self.assertEqual(result["selection_origin"], "legacy_default")
        self.assertEqual(config, {"cad_skill": "missing-historical-file.md"})

    def test_presence_requires_complete_typed_explicit_selection(self):
        for value in (None, [], "guidance", {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "invalid_capability_selection"):
                self.normalize({"candidate_capabilities": value})
        for field in self.config()["candidate_capabilities"]:
            config = self.config()
            del config["candidate_capabilities"][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.normalize(config)
        for field, value in (("skills", "guidance"), ("mcp_servers", None),
                             ("skills", [True]), ("skills", ["Guidance"]),
                             ("schema", "candidate-capabilities/2"),
                             ("skill_delivery", "prompt_inline"), ("exposure_mode", "direct"),
                             ("study_kind", "unknown"), ("enabled", True)):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.normalize(self.config(**{field: value}))

    def test_duplicate_unknown_and_reserved_ids_rejected(self):
        for field, value, reason in (
            ("skills", ["guidance", "guidance"], "duplicate_capability_id"),
            ("skills", ["missing"], "unknown_skill_id"),
            ("mcp_servers", ["cad_native", "cad_native"], "duplicate_capability_id"),
            ("mcp_servers", ["cad_native", "missing"], "unknown_mcp_id"),
            ("skills", ["benchmark"], "reserved_capability_id"),
        ):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, reason):
                self.normalize(self.config(**{field: value}))

    def test_no_cad_requires_explicit_negative_control(self):
        with self.assertRaisesRegex(ValueError, "cad_capability_required"):
            self.normalize(self.config(mcps=[]))
        result = self.normalize(self.config(mcps=[], study_kind="negative_control_no_cad"))
        self.assertEqual(result["mcp_servers"], [])

    def test_dependencies_and_backend_are_checked_without_auto_enabling(self):
        with self.assertRaisesRegex(ValueError, "skill_mcp_dependency_missing"):
            self.normalize(self.config(skills=["guidance"], mcps=[], study_kind="negative_control_no_cad"))
        with self.assertRaisesRegex(ValueError, "skill_cad_backend_incompatible"):
            self.normalize(self.config(skills=["guidance"]), backend="fusion")

    def test_list_order_is_preserved_and_bound_into_selection_hash(self):
        first = self.normalize(self.config(skills=["guidance", "checklist"], mcps=["lookup", "cad_native"]))
        second = self.normalize(self.config(skills=["checklist", "guidance"], mcps=["lookup", "cad_native"]))
        self.assertEqual(first["skills"], ["guidance", "checklist"])
        self.assertEqual(first["mcp_servers"], ["lookup", "cad_native"])
        self.assertNotEqual(first["selection_sha256"], second["selection_sha256"])
        self.assertEqual(first["selection_sha256"], self.normalize(self.config(
            skills=["guidance", "checklist"], mcps=["lookup", "cad_native"]))["selection_sha256"])

    def test_resource_paths_are_explicit_relative_canonical_files(self):
        for path in ("../secret.txt", "/secret.txt", "C:/secret.txt", "sub/../notes.txt",
                     "./notes.txt", "notes.txt:stream", "sub\\notes.txt", "notes.txt.", "notes.txt "):
            registry = copy.deepcopy(self.registry)
            registry["skills"]["guidance"]["resources"][0]["path"] = path
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "invalid_skill_resource_path"):
                self.normalize(self.config(skills=["guidance"]), registry)

    def test_resource_digest_and_static_file_type_enforced(self):
        (self.skill_root / "run.py").write_text("print('no')", encoding="utf-8")
        for changes, reason in (({"sha256": "0" * 64}, "capability_source_hash_mismatch"),
                                ({"path": "run.py"}, "unsupported_skill_resource_type"),
                                ({"sha256": None}, "invalid_resource_sha256")):
            registry = copy.deepcopy(self.registry)
            registry["skills"]["guidance"]["resources"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, reason):
                self.normalize(self.config(skills=["guidance"]), registry)

    def test_resource_case_aliases_and_duplicate_ids_rejected(self):
        for changes in ({"id": "notes", "path": "SKILL.md"}, {"id": "other", "path": "NOTES.TXT"}):
            registry = copy.deepcopy(self.registry)
            registry["skills"]["guidance"]["resources"].append(dict(
                registry["skills"]["guidance"]["resources"][0], **changes))
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "duplicate_skill_resource"):
                self.normalize(self.config(skills=["guidance"]), registry)

    def test_symlinked_skill_ancestor_rejected(self):
        link = self.root / "alias"
        try:
            link.symlink_to(self.skill_root, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation unavailable; junction checked separately on Windows")
        registry = copy.deepcopy(self.registry)
        registry["skills"]["guidance"]["path"] = str(link / "SKILL.md")
        with self.assertRaisesRegex(ValueError, "capability_reparse_point"):
            self.normalize(self.config(skills=["guidance"]), registry)

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_junctioned_skill_ancestor_rejected(self):
        link = self.root / "junction"
        # mklink is creation only; removal stays within tempfile's normal cleanup.
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(self.skill_root)],
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(lambda: link.rmdir() if link.exists() else None)
        registry = copy.deepcopy(self.registry)
        registry["skills"]["guidance"]["path"] = str(link / "SKILL.md")
        with self.assertRaisesRegex(ValueError, "capability_reparse_point"):
            self.normalize(self.config(skills=["guidance"]), registry)

    def test_registry_validation_rejects_unsupported_and_unsafe_server_definitions(self):
        for changes in ({"kind": "sse"}, {"kind": "builtin_cad_adapter"},
                        {"command": "python"}, {"args": "shell command"},
                        {"env_allowlist": {"SECRET": "value"}}, {"side_effect_policy": "cad_write"},
                        {"allowed_tools": []}, {"timeout_s": 0}, {"max_response_bytes": True},
                        {"source_files": []}, {"env": {"TOKEN": "secret"}}):
            registry = copy.deepcopy(self.registry)
            registry["mcp_servers"]["lookup"].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.normalize(self.config(mcps=["cad_native", "lookup"]), registry)

    def test_load_registry_is_static_and_rejects_unknown_schema_and_duplicate_json_keys(self):
        path = self.root / "registry.json"
        path.write_text(json.dumps(self.registry), encoding="utf-8")
        with mock.patch("subprocess.Popen", side_effect=AssertionError("unexpected process")):
            loaded = capabilities.load_registry(path)
            self.normalize(self.config(skills=["guidance"], mcps=["cad_native", "lookup"]), loaded)
        for raw in ('{"schema":"candidate-capability-registry/2","skills":{},"mcp_servers":{}}',
                    '{"schema":"candidate-capability-registry/1","skills":{},"skills":{},"mcp_servers":{}}'):
            path.write_text(raw, encoding="utf-8")
            with self.assertRaises(ValueError):
                capabilities.load_registry(path)

    def test_snapshot_copies_exact_selected_closure_and_hides_private_data(self):
        (self.skill_root / "unlisted-secret.txt").write_text("PRIVATE", encoding="utf-8")
        selection = self.normalize(self.config(skills=["guidance"], mcps=["cad_native", "lookup"]))
        output = self.root / "new-snapshot"
        manifest = capabilities.freeze_capabilities(selection, self.registry, output)
        self.assertEqual(manifest["ordered_enabled_skill_ids"], ["guidance"])
        self.assertEqual(manifest["ordered_enabled_mcp_ids"], ["cad_native", "lookup"])
        skill = manifest["skills"]["guidance"]
        self.assertEqual(Path(skill["snapshot_path"]).read_bytes(), b"Public modeling guidance.\n")
        self.assertEqual(Path(skill["resources"][0]["snapshot_path"]).read_bytes(), b"Check dimensions.\n")
        self.assertEqual(skill["content_sha256"], hashlib.sha256(b"Public modeling guidance.\n").hexdigest())
        self.assertFalse(any(path.name == "unlisted-secret.txt" for path in output.rglob("*")))
        self.assertTrue((output / "capability-manifest.json").is_file())
        self.assertEqual(json.loads((output / "capability-manifest.json").read_text(encoding="utf-8")), manifest)
        public = capabilities.public_capabilities(manifest)
        self.assertEqual([skill["id"] for skill in public["skills"]], ["guidance"])
        self.assertEqual(public["mcp_servers"], ["cad_native", "lookup"])
        text = json.dumps(public)
        for private in (str(self.root), "checklist", "command", "env_allowlist", "source_files", "PRIVATE"):
            self.assertNotIn(private, text)
        public["skills"][0]["id"] = "changed"
        self.assertEqual(manifest["skills"]["guidance"]["id"], "guidance")

    def test_snapshot_refuses_existing_directory_without_modifying_it(self):
        selection = self.normalize()
        output = self.root / "existing"
        output.mkdir()
        (output / "old.txt").write_text("historic", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "capability_snapshot_exists"):
            capabilities.freeze_capabilities(selection, self.registry, output)
        self.assertEqual([path.name for path in output.iterdir()], ["old.txt"])

    def test_skill_or_auxiliary_source_change_after_selection_rejected_before_snapshot(self):
        for name in ("skill", "resource", "server"):
            with self.subTest(name=name):
                selection = self.normalize(self.config(skills=["guidance"], mcps=["cad_native", "lookup"]))
                path = {"skill": self.skill_root / "SKILL.md", "resource": self.skill_root / "notes.txt",
                        "server": self.server}[name]
                original = path.read_bytes()
                path.write_bytes(original + b"changed")
                output = self.root / ("changed-" + name)
                with self.assertRaisesRegex(ValueError, "capability_source_hash_mismatch"):
                    capabilities.freeze_capabilities(selection, self.registry, output)
                self.assertFalse(output.exists())
                path.write_bytes(original)

    def test_changed_registry_definition_is_not_substituted_during_freeze(self):
        selection = self.normalize(self.config(mcps=["cad_native", "lookup"]))
        registry = copy.deepcopy(self.registry)
        registry["mcp_servers"]["lookup"]["args"].append("--different")
        with self.assertRaisesRegex(ValueError, "capability_registry_changed"):
            capabilities.freeze_capabilities(selection, registry, self.root / "changed-definition")

    def test_empty_snapshot_public_projection_contains_no_disabled_skill_metadata(self):
        manifest = capabilities.freeze_capabilities(self.normalize(), self.registry, self.root / "empty")
        public = capabilities.public_capabilities(manifest)
        self.assertEqual(public["skills"], [])
        self.assertNotIn("guidance", json.dumps(public))
        self.assertEqual(manifest["actual_skill_reads"], [])
        self.assertEqual(manifest["exposure_verification_status"], "not_verified")

    def test_snapshot_keeps_bytes_when_originals_are_later_modified(self):
        selection = self.normalize(self.config(skills=["guidance"], mcps=["cad_native", "lookup"]))
        manifest = capabilities.freeze_capabilities(selection, self.registry, self.root / "frozen")
        (self.skill_root / "SKILL.md").write_bytes(b"replacement")
        self.server.write_bytes(b"replacement")
        self.assertEqual(Path(manifest["skills"]["guidance"]["snapshot_path"]).read_bytes(),
                         b"Public modeling guidance.\n")
        server = manifest["requested_server_definitions"]["lookup"]
        self.assertNotEqual(Path(server["source_file_hashes"][0]["snapshot_path"]).read_bytes(), b"replacement")
        self.assertEqual(server["source_files"], [str(self.server)])
        self.assertEqual(len(server["command_sha256"]), 64)

    def test_resource_file_directory_collisions_rejected_before_snapshot_allocation(self):
        registry = copy.deepcopy(self.registry)
        registry["skills"]["guidance"]["resources"].append({
            "id": "nested", "path": "notes.txt/another.txt", "sha256": "0" * 64})
        with self.assertRaisesRegex(ValueError, "duplicate_skill_resource"):
            self.normalize(self.config(skills=["guidance"]), registry)

    def test_explicit_absolute_dot_segment_aliases_rejected(self):
        for path in (str(self.skill_root) + "/./SKILL.md", str(self.skill_root) + "/../guidance/SKILL.md"):
            registry = copy.deepcopy(self.registry)
            registry["skills"]["guidance"]["path"] = path
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "invalid_capability_source_path"):
                self.normalize(self.config(skills=["guidance"]), registry)

    def test_hardlinked_resource_rejected_as_path_alias(self):
        hardlink = self.skill_root / "alias.txt"
        os.link(self.skill_root / "notes.txt", hardlink)
        with self.assertRaisesRegex(ValueError, "capability_path_alias"):
            self.normalize(self.config(skills=["guidance"]))

    def test_load_registry_binds_source_bytes_before_selection(self):
        path = self.root / "registry.json"
        path.write_text(json.dumps(self.registry), encoding="utf-8")
        registry = capabilities.load_registry(path)
        (self.skill_root / "SKILL.md").write_bytes(b"replacement")
        with self.assertRaisesRegex(ValueError, "capability_source_hash_mismatch"):
            self.normalize(self.config(skills=["guidance"]), registry)

    def test_snapshot_parent_junction_is_rejected(self):
        parent = self.root / "snapshot-parent"
        parent.mkdir()
        alias = self.root / "snapshot-alias"
        try:
            alias.symlink_to(parent, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation unavailable")
        with self.assertRaisesRegex(ValueError, "capability_reparse_point"):
            capabilities.freeze_capabilities(self.normalize(), self.registry, alias / "snapshot")
        self.assertEqual(list(parent.iterdir()), [])

    def test_reserved_core_tool_names_cannot_be_registered(self):
        for name in ("benchmark", "get_contract", "fusion_call", "solidworks_call", "read_input", "cad_tools", "cad_call"):
            registry = copy.deepcopy(self.registry)
            registry["skills"][name] = registry["skills"].pop("checklist")
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "reserved_capability_id"):
                self.normalize(registry=registry)

    def test_verify_frozen_files_detects_modified_skill_resource_and_aux_source(self):
        selection = self.normalize(self.config(skills=["guidance"], mcps=["cad_native", "lookup"]))
        manifest = capabilities.freeze_capabilities(selection, self.registry, self.root / "verified")
        self.assertEqual(capabilities.verify_frozen_capabilities(manifest)["status"], "verified")
        skill = manifest["skills"]["guidance"]
        files = [skill["snapshot_path"], skill["resources"][0]["snapshot_path"],
                 manifest["requested_server_definitions"]["lookup"]["source_file_hashes"][0]["snapshot_path"]]
        for file in files:
            path = Path(file)
            original = path.read_bytes()
            path.write_bytes(b"tampered")
            with self.subTest(file=file), self.assertRaisesRegex(ValueError, "capability_source_hash_mismatch"):
                capabilities.verify_frozen_capabilities(manifest)
            path.write_bytes(original)

    def test_verify_original_sources_and_snapshot_path_boundary(self):
        selection = self.normalize(self.config(skills=["guidance"]))
        manifest = capabilities.freeze_capabilities(selection, self.registry, self.root / "verify-source")
        (self.skill_root / "SKILL.md").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "capability_source_hash_mismatch"):
            capabilities.verify_frozen_capabilities(manifest)
        self.assertEqual(capabilities.verify_frozen_capabilities(manifest, verify_sources=False)["status"], "verified")
        manifest["skills"]["guidance"]["snapshot_path"] = str(self.skill_root / "SKILL.md")
        with self.assertRaisesRegex(ValueError, "capability_snapshot_path_escape"):
            capabilities.verify_frozen_capabilities(manifest, verify_sources=False)

    def test_unselected_content_does_not_change_selection_hash(self):
        first = self.normalize()
        (self.skill_root / "SKILL.md").write_bytes(b"other guidance")
        second = self.normalize()
        self.assertEqual(first["selection_sha256"], second["selection_sha256"])
        self.assertEqual(first["_source_hashes"], second["_source_hashes"])


if __name__ == "__main__":
    unittest.main()
