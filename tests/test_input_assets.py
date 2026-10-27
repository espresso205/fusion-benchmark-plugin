"""Public input admission and identity tests; no model or CAD calls."""
import base64
from copy import deepcopy
import hashlib
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image


DXF = b"0\nSECTION\n2\nHEADER\n0\nENDSEC\n0\nEOF\n"


def picture(format="PNG", size=(8, 5), orientation=None):
    output = io.BytesIO()
    image = Image.new("RGB", size, (200, 30, 40))
    options = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[274] = orientation
        options["exif"] = exif
    image.save(output, format=format, **options)
    return output.getvalue()


def asset(root, name, raw, identifier, role, mime):
    path = Path(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return {"id": identifier, "role": role, "path": name, "mime": mime,
            "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "display_name": identifier}


def manifest_fixture(root, case_id="Q01"):
    return {"schema": "fusion-public-inputs/1", "case_id": case_id,
            "task": asset(root, "TASK.md", b"Model dimensions from annotations.", "task", "task", "text/markdown"),
            "attachments": [asset(root, "views/front.png", picture(), "front", "view", "image/png"),
                            asset(root, "drawing.dxf", DXF, "drawing", "drawing", "application/dxf")]}


class InputAssetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = manifest_fixture(self.root)

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec("fusion_pipeline.input_assets"), "public input API is missing")
        return importlib.import_module("fusion_pipeline.input_assets")

    def save_manifest(self, value=None):
        path = self.root / "inputs.json"
        path.write_text(json.dumps(value or self.manifest), encoding="utf-8")
        return path

    def test_order_identity_and_load_survive_json_roundtrip(self):
        api = self.api()
        loaded = api.load_manifest(self.save_manifest(), "Q01")
        self.assertEqual([a["id"] for a in api.iter_assets(loaded)], ["task", "front", "drawing"])
        self.assertTrue(api.verify_manifest(loaded, self.root))
        descriptor, raw = api.read_asset(loaded, self.root, "front")
        self.assertEqual(descriptor["path"], "views/front.png")
        self.assertEqual(raw, (self.root / "views/front.png").read_bytes())

    def test_structure_validation_does_not_read_files(self):
        api = self.api()
        (self.root / "views/front.png").unlink()
        api.validate_manifest(self.manifest, "Q01")
        with self.assertRaises((ValueError, OSError)):
            api.verify_manifest(self.manifest, self.root)

    def test_single_image_case_does_not_require_dxf(self):
        self.manifest["attachments"] = self.manifest["attachments"][:1]
        (self.root / "drawing.dxf").unlink()
        self.assertTrue(self.api().verify_manifest(self.manifest, self.root))
        self.assertEqual(self.api().load_manifest(self.save_manifest())["attachments"][0]["id"], "front")

    def test_example_generates_valid_ordered_mixed_inputs_without_overwrite(self):
        example = Path(__file__).resolve().parents[1] / "examples/create_public_input_example.py"
        self.assertTrue(example.is_file(), "runnable input example is missing")
        output = self.root / "example/Q01"
        command = [sys.executable, "-B", str(example), str(output)]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        loaded = self.api().load_manifest(output / "inputs.json", "Q01")
        self.assertEqual([a["id"] for a in loaded["attachments"]], ["front", "side", "outline"])
        before = (output / "inputs.json").read_bytes()
        again = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(again.returncode, 0)
        self.assertEqual((output / "inputs.json").read_bytes(), before)

    def test_legacy_mapping_keeps_fixed_names_and_byte_identity(self):
        manifest = self.api().legacy_manifest("Q01", self.root)
        self.assertEqual([a["id"] for a in self.api().iter_assets(manifest)], ["task", "drawing"])
        self.assertEqual(manifest["attachments"][0]["sha256"], hashlib.sha256(DXF).hexdigest())

    def test_valid_dxf_leading_comments_preserve_legacy_and_manifest_admission(self):
        import ezdxf
        raw = b"999\nCreated by public CAD exporter\n999\nSecond comment\n" + DXF
        self.manifest["attachments"][1] = asset(self.root, "drawing.dxf", raw, "drawing", "drawing", "application/dxf")
        document = ezdxf.readfile(self.root / "drawing.dxf")
        self.assertEqual(len(document.modelspace()), 0)
        api = self.api()
        self.assertTrue(api.verify_manifest(self.manifest, self.root))
        legacy = api.legacy_manifest("Q01", self.root)
        self.assertEqual(legacy["attachments"][0]["sha256"], hashlib.sha256(raw).hexdigest())

    def test_dxf_garbage_prefix_and_comment_only_are_not_admitted(self):
        for raw in (b"arbitrary garbage\n" + DXF, b"999\ncomment only\n", b"999\n", b"not a DXF"):
            self.manifest["attachments"][1] = asset(self.root, "drawing.dxf", raw, "drawing", "drawing", "application/dxf")
            with self.assertRaisesRegex(ValueError, "DXF"):
                self.api().verify_manifest(self.manifest, self.root)

    def test_each_read_rejects_changed_size_or_hash(self):
        api = self.api()
        path = self.root / "views/front.png"
        raw = path.read_bytes()
        for changed in (raw + b"x", raw[:-1] + bytes([raw[-1] ^ 1])):
            path.write_bytes(changed)
            with self.assertRaisesRegex(ValueError, "(?:hash|size|bytes|identity)"):
                api.read_asset(self.manifest, self.root, "front")

    def test_unknown_id_and_case_mismatch_are_rejected(self):
        api = self.api()
        with self.assertRaises(ValueError):
            api.read_asset(self.manifest, self.root, "Q02/front")
        with self.assertRaises(ValueError):
            api.load_manifest(self.save_manifest(), "Q02")

    def test_strict_schema_and_descriptor_fields(self):
        mutations = [lambda m: m.update(schema="fusion-public-inputs/2"),
                     lambda m: m.update(private_gt="answer.step"),
                     lambda m: m["task"].update(id="different"),
                     lambda m: m["task"].update(path="prompt.md"),
                     lambda m: m["attachments"][0].update(bytes=True),
                     lambda m: m["attachments"][0].update(bytes=0),
                     lambda m: m["attachments"][0].update(role="ground_truth"),
                     lambda m: m["attachments"][0].update(display_name="bad\nname"),
                     lambda m: m["attachments"][0].update(required=False),
                     lambda m: m["attachments"][0].update(sha256="x" * 64)]
        for mutation in mutations:
            value = deepcopy(self.manifest)
            mutation(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.api().validate_manifest(value)

    def test_unsafe_paths_and_windows_aliases_are_rejected(self):
        paths = ["../secret.png", "/secret.png", "C:/secret.png", "//host/share.png", "a\\b.png",
                 "a//b.png", "./b.png", "x/../b.png", "a:stream.png", "CON.png", "x/LPT1.png",
                 "a./b.png", "a /b.png", "a\x01.png"]
        for path in paths:
            value = deepcopy(self.manifest)
            value["attachments"][0]["path"] = path
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.api().validate_manifest(value)

    def test_casefold_ids_paths_and_file_directory_collisions_are_rejected(self):
        for field, value in (("id", "TASK"), ("id", "FRONT"), ("path", "VIEWS/FRONT.PNG"),
                             ("path", "views/front.png/child.png")):
            manifest = deepcopy(self.manifest)
            extra = deepcopy(manifest["attachments"][0])
            extra.update(id="third", path="third.png")
            extra[field] = value
            manifest["attachments"].append(extra)
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.api().validate_manifest(manifest)

    def test_unsupported_formats_and_mime_mismatch_fail_admission(self):
        for extension, mime in (("pdf", "application/pdf"), ("webp", "image/webp"), ("step", "application/step"),
                                ("f3d", "application/octet-stream"), ("png", "image/jpeg")):
            value = deepcopy(self.manifest)
            value["attachments"][0].update(path="input." + extension, mime=mime)
            with self.subTest(extension=extension), self.assertRaises(ValueError):
                self.api().validate_manifest(value)

    def test_wrong_signature_and_corrupt_image_are_rejected(self):
        for raw in (picture("JPEG"), b"\x89PNG\r\n\x1a\nnot-a-decodable-image"):
            value = deepcopy(self.manifest)
            value["attachments"][0] = asset(self.root, "bad.png", raw, "front", "view", "image/png")
            with self.assertRaises(ValueError):
                self.api().verify_manifest(value, self.root)

    def test_animated_png_is_rejected_instead_of_silently_reading_first_frame(self):
        output = io.BytesIO()
        Image.new("RGB", (8, 5), "red").save(output, format="PNG", save_all=True,
                                            append_images=[Image.new("RGB", (8, 5), "blue")], duration=100, loop=0)
        self.manifest["attachments"] = [asset(self.root, "animation.png", output.getvalue(), "animation", "view", "image/png")]
        with self.assertRaisesRegex(ValueError, "frames"):
            self.api().verify_manifest(self.manifest, self.root)

    def test_jpg_jpeg_uppercase_extensions_and_native_payload(self):
        api = self.api()
        for extension in ("png", "jpg", "JPEG"):
            raw = picture("PNG" if extension == "png" else "JPEG")
            mime = "image/png" if extension == "png" else "image/jpeg"
            value = deepcopy(self.manifest)
            value["attachments"] = [asset(self.root, "image." + extension, raw, "image", "drawing", mime)]
            result = api.image_result(value, self.root, "image")
            self.assertFalse(result["isError"])
            content = result["content"]
            self.assertEqual([item["type"] for item in content], ["text", "image"])
            self.assertEqual(content[1]["mimeType"], mime)
            returned = base64.b64decode(content[1]["data"], validate=True)
            self.assertEqual(returned, raw)
            metadata = result["structuredContent"]
            self.assertEqual((metadata["width"], metadata["height"]), (8, 5))
            self.assertEqual(metadata["source_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(metadata["returned_sha256"], hashlib.sha256(returned).hexdigest())
            self.assertEqual(metadata["transform"], "none")
            self.assertEqual(json.loads(content[0]["text"]), metadata)

    def test_exif_transpose_changes_returned_identity_without_source_mutation(self):
        raw = picture("JPEG", size=(8, 5), orientation=6)
        self.manifest["attachments"] = [asset(self.root, "rotated.jpg", raw, "rotated", "view", "image/jpeg")]
        result = self.api().image_result(self.manifest, self.root, "rotated")
        metadata = result["structuredContent"]
        returned = base64.b64decode(result["content"][1]["data"])
        self.assertEqual((metadata["width"], metadata["height"]), (5, 8))
        self.assertEqual(metadata["transform"], "exif_transpose:6")
        self.assertNotEqual(metadata["source_sha256"], metadata["returned_sha256"])
        self.assertEqual((self.root / "rotated.jpg").read_bytes(), raw)
        with Image.open(io.BytesIO(returned)) as image:
            self.assertEqual(image.size, (5, 8))
            self.assertNotIn(image.getexif().get(274), (2, 3, 4, 5, 6, 7, 8))

    def test_non_image_cannot_be_returned_as_image(self):
        for identifier in ("task", "drawing"):
            with self.assertRaises(ValueError):
                self.api().image_result(self.manifest, self.root, identifier)

    def test_declared_size_count_and_total_limits(self):
        api = self.api()
        for target, size in (("task", 1024 * 1024 + 1), (0, 8 * 1024 * 1024 + 1), (1, 16 * 1024 * 1024 + 1)):
            value = deepcopy(self.manifest)
            descriptor = value["task"] if target == "task" else value["attachments"][target]
            descriptor["bytes"] = size
            with self.assertRaises(ValueError):
                api.validate_manifest(value)
        value = deepcopy(self.manifest)
        value["attachments"] = [{**value["attachments"][0], "id": f"image{i}", "path": f"image{i}.png"} for i in range(17)]
        with self.assertRaises(ValueError):
            api.validate_manifest(value)
        value["attachments"] = [{**a, "bytes": 8 * 1024 * 1024} for a in value["attachments"][:9]]
        with self.assertRaises(ValueError):
            api.validate_manifest(value)

    def test_pixel_dimension_and_encoded_response_limits(self):
        api = self.api()
        with patch.object(api, "MAX_IMAGE_PIXELS", 39), self.assertRaises(ValueError):
            api.verify_manifest(self.manifest, self.root)
        with patch.object(api, "MAX_IMAGE_SIDE", 7), self.assertRaises(ValueError):
            api.verify_manifest(self.manifest, self.root)
        with patch.object(api, "MAX_RESPONSE_BYTES", 100), self.assertRaises(ValueError):
            api.image_result(self.manifest, self.root, "front")

    def test_exif_response_overflow_is_rejected_at_load_and_verify_before_read(self):
        api = self.api()
        raw = picture("JPEG", size=(8, 5), orientation=6)
        self.manifest["attachments"] = [asset(self.root, "rotated.jpg", raw, "rotated", "view", "image/jpeg")]
        path = self.save_manifest()
        self.assertLess(len(raw), 8 * 1024 * 1024)
        with patch.object(api, "MAX_RESPONSE_BYTES", 100):
            with self.assertRaisesRegex(ValueError, "response"):
                api.verify_manifest(self.manifest, self.root)
            with self.assertRaisesRegex(ValueError, "response"):
                api.load_manifest(path)

    def test_duplicate_json_keys_and_oversized_manifest_are_rejected(self):
        api = self.api()
        path = self.save_manifest()
        path.write_text('{"schema":"fusion-public-inputs/1","schema":"fusion-public-inputs/1"}')
        with self.assertRaises(ValueError):
            api.load_manifest(path)
        path.write_bytes(b" " * (1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            api.load_manifest(path)

    def test_hardlinked_asset_is_rejected(self):
        source = self.root / "views/front.png"
        os.link(source, self.root / "alias.png")
        with self.assertRaises(ValueError):
            self.api().read_asset(self.manifest, self.root, "front")

    def test_symlink_file_and_parent_are_rejected(self):
        source = self.root / "views/front.png"
        alias = self.root / "linked.png"
        try:
            alias.symlink_to(source)
        except OSError:
            self.skipTest("symlink privilege unavailable")
        value = deepcopy(self.manifest)
        value["attachments"][0]["path"] = "linked.png"
        with self.assertRaises(ValueError):
            self.api().verify_manifest(value, self.root)
        parent = self.root / "alias"
        parent.symlink_to(self.root / "views", target_is_directory=True)
        value["attachments"][0]["path"] = "alias/front.png"
        with self.assertRaises(ValueError):
            self.api().verify_manifest(value, self.root)

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_junction_root_is_rejected_before_resolve(self):
        with tempfile.TemporaryDirectory() as outer:
            junction = Path(outer) / "linked"
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(junction), str(self.root)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            try:
                with self.assertRaises(ValueError):
                    self.api().verify_manifest(self.manifest, junction)
            finally:
                os.rmdir(junction)


if __name__ == "__main__":
    unittest.main()
