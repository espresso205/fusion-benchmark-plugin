"""Versioned, byte-bound public inputs shared by the ledger and worker.

This admits modeling inputs, unlike the optional report attachment adapter.
All listed files are required. Paths are relative to one public case root.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import warnings


SCHEMA = "fusion-public-inputs/1"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ATTACHMENTS = 16
MAX_TASK_BYTES = 1024 * 1024
MAX_DXF_BYTES = 16 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
MAX_IMAGE_SIDE = 12_000
MAX_RESPONSE_BYTES = 12 * 1024 * 1024
_FIELDS = {"id", "role", "path", "mime", "bytes", "sha256", "display_name"}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10)),
             "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³"}
_MIMES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".dxf": "application/dxf"}


def _identity(value, label):
    if not isinstance(value, str) or not _ID.fullmatch(value) or value.upper() in _RESERVED:
        raise ValueError(f"invalid public input {label}")


def _parts(relative):
    if not isinstance(relative, str) or not relative or len(relative) > 512 or "\\" in relative:
        raise ValueError("input path must be a canonical relative POSIX path")
    parts = relative.split("/")
    for part in parts:
        if (not part or part in (".", "..") or part.endswith((".", " "))
                or any(ord(c) < 32 or ord(c) == 127 or c in ':<>"|?*' for c in part)
                or part.split(".")[0].upper() in _RESERVED):
            raise ValueError("unsafe public input path")
    return parts


def _no_links(path):
    """Inspect lexical ancestors before resolution, including Windows junctions."""
    path = Path(path).absolute()
    for component in (*reversed(path.parents), path):
        information = component.lstat()
        if stat.S_ISLNK(information.st_mode) or getattr(information, "st_file_attributes", 0) & 0x400:
            raise ValueError("public input links and reparse points are not allowed")
    return path


def _regular(path):
    path = _no_links(path)
    information = path.stat()
    if not stat.S_ISREG(information.st_mode) or information.st_nlink != 1 or information.st_size <= 0:
        raise ValueError("public input must be a nonempty regular file without hardlinks")
    return path, information


def _read_file(path, limit, expected=None):
    path, before = _regular(path)
    if before.st_size > limit:
        raise ValueError("public input exceeds byte limit")
    if expected is not None and before.st_size != expected["bytes"]:
        raise ValueError("public input bytes/size identity mismatch")
    with path.open("rb") as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("public input changed while opening")
        raw = source.read(limit + 1)
        after_read = os.fstat(source.fileno())
    _, after = _regular(path)
    identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink)
    if identity(before) != identity(after_read) or identity(before) != identity(after) or len(raw) > limit:
        raise ValueError("public input changed while reading")
    if expected is not None and (len(raw) != expected["bytes"] or hashlib.sha256(raw).hexdigest() != expected["sha256"]):
        raise ValueError("public input hash/size identity mismatch")
    return raw


def _limit(descriptor):
    return MAX_TASK_BYTES if descriptor["role"] == "task" else (
        MAX_DXF_BYTES if descriptor["mime"] == "application/dxf" else MAX_IMAGE_BYTES)


def iter_assets(manifest):
    """Yield task first, then attachments in the manifest's declared order."""
    yield manifest["task"]
    yield from manifest["attachments"]


def validate_manifest(manifest, case_id=None):
    """Validate only structure and declared limits; never read the filesystem."""
    if not isinstance(manifest, dict) or set(manifest) != {"schema", "case_id", "task", "attachments"}:
        raise ValueError("invalid public input manifest fields")
    if manifest["schema"] != SCHEMA:
        raise ValueError("unsupported public input manifest schema")
    _identity(manifest["case_id"], "case_id")
    if case_id is not None and manifest["case_id"] != case_id:
        raise ValueError("public input case identity mismatch")
    if not isinstance(manifest["attachments"], list) or len(manifest["attachments"]) > MAX_ATTACHMENTS:
        raise ValueError("public input attachment count exceeds limit or is invalid")
    identifiers, paths, total = set(), [], 0
    for index, descriptor in enumerate(iter_assets(manifest)):
        if not isinstance(descriptor, dict) or set(descriptor) != _FIELDS:
            raise ValueError("invalid public input descriptor fields")
        _identity(descriptor["id"], "asset id")
        identifier = descriptor["id"].casefold()
        if identifier in identifiers:
            raise ValueError("public input IDs must be unique including Windows case aliases")
        identifiers.add(identifier)
        _parts(descriptor["path"])
        path = descriptor["path"].casefold()
        if any(path == previous or path.startswith(previous + "/") or previous.startswith(path + "/") for previous in paths):
            raise ValueError("public input paths collide on Windows")
        paths.append(path)
        if index == 0:
            if (descriptor["id"], descriptor["role"], descriptor["path"], descriptor["mime"]) != (
                    "task", "task", "TASK.md", "text/markdown"):
                raise ValueError("task must be TASK.md with task id/role and text/markdown MIME")
        else:
            if descriptor["role"] not in ("drawing", "view", "reference"):
                raise ValueError("unsupported public attachment role")
            extension = PurePosixPath(descriptor["path"]).suffix.lower()
            if extension not in _MIMES or descriptor["mime"] != _MIMES[extension]:
                raise ValueError("unsupported attachment format or MIME/extension mismatch")
        if type(descriptor["bytes"]) is not int or not 0 < descriptor["bytes"] <= _limit(descriptor):
            raise ValueError("public input bytes must be positive and within the format limit")
        if not isinstance(descriptor["sha256"], str) or not _SHA.fullmatch(descriptor["sha256"]):
            raise ValueError("public input sha256 must be lowercase hexadecimal")
        label = descriptor["display_name"]
        if (not isinstance(label, str) or not label.strip() or len(label) > 120
                or any(ord(c) < 32 or ord(c) == 127 for c in label)):
            raise ValueError("public input display_name must be nonempty short text")
        total += descriptor["bytes"]
    if total > MAX_TOTAL_BYTES:
        raise ValueError("public input total bytes exceed limit")
    return True


def _image(raw, mime):
    from PIL import Image
    signature = b"\x89PNG\r\n\x1a\n" if mime == "image/png" else b"\xff\xd8\xff"
    if not raw.startswith(signature):
        raise ValueError("image signature does not match MIME/extension")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format != ("PNG" if mime == "image/png" else "JPEG"):
                    raise ValueError("decoded image format does not match MIME")
                width, height = image.size
                if width <= 0 or height <= 0 or max(width, height) > MAX_IMAGE_SIDE or width * height > MAX_IMAGE_PIXELS:
                    raise ValueError("image dimensions/pixels exceed limit")
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError("multiple image frames are unsupported")
                image.verify()
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
                orientation = image.getexif().get(274, 1)
                if type(orientation) is not int or orientation not in range(1, 9):
                    raise ValueError("unsupported EXIF orientation")
                return image.copy(), orientation
    except (OSError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError) as error:
        raise ValueError("image cannot be decoded safely") from error


def _content(descriptor, raw):
    mime = descriptor["mime"]
    if mime.startswith("image/"):
        # Admission must guarantee the actual oriented payload is readable later,
        # including base64/JSON expansion, not just the source-file byte limit.
        _image_response(descriptor, raw, MAX_ATTACHMENTS - 1)
    elif mime == "text/markdown":
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeError as error:
            raise ValueError("TASK.md must be UTF-8 text") from error
        if not text.strip() or "\x00" in text:
            raise ValueError("TASK.md must contain nonempty text without NUL")
    elif mime == "application/dxf":
        if not raw.startswith(b"AutoCAD Binary DXF\r\n\x1a\x00"):
            # ASCII DXF permits 999/value comment pairs before its first section.
            # Skip only complete comments; arbitrary prefixes and comment-only
            # files still cannot obtain DXF admission from an embedded marker.
            lines = raw.removeprefix(b"\xef\xbb\xbf").lstrip().splitlines()
            index = 0
            while index + 1 < len(lines) and lines[index].strip() == b"999":
                index += 2
            if index + 1 >= len(lines) or lines[index].strip() != b"0" or lines[index + 1].strip() != b"SECTION":
                raise ValueError("DXF signature is invalid")


def _read_descriptor(root, descriptor):
    root = _no_links(root)
    if not root.is_dir():
        raise ValueError("public input root must be a directory")
    path = root.joinpath(*_parts(descriptor["path"]))
    raw = _read_file(path, _limit(descriptor), descriptor)
    _content(descriptor, raw)
    return raw


def verify_manifest(manifest, root):
    """Revalidate every required asset against its registered bytes and SHA256."""
    validate_manifest(manifest)
    for descriptor in iter_assets(manifest):
        _read_descriptor(root, descriptor)
    return True


def load_manifest(path, case_id=None):
    """Load strict JSON and validate files relative to the manifest's directory."""
    path = Path(path)
    raw = _read_file(path, MAX_MANIFEST_BYTES)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key in public input manifest")
            result[key] = value
        return result

    def reject(value):
        raise ValueError("invalid numeric value in public input manifest")

    try:
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject)
    except UnicodeError as error:
        raise ValueError("public input manifest must be UTF-8") from error
    validate_manifest(manifest, case_id)
    verify_manifest(manifest, path.parent)
    return manifest


def legacy_manifest(case_id, root):
    """Create a transient manifest for the old fixed pair; never rewrite it."""
    root = _no_links(root)
    descriptors = []
    for identifier, name, mime, limit in (("task", "TASK.md", "text/markdown", MAX_TASK_BYTES),
                                           ("drawing", "drawing.dxf", "application/dxf", MAX_DXF_BYTES)):
        raw = _read_file(root / name, limit)
        descriptors.append({"id": identifier, "role": identifier, "path": name, "mime": mime,
                            "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "display_name": name})
    manifest = {"schema": SCHEMA, "case_id": case_id, "task": descriptors[0], "attachments": descriptors[1:]}
    verify_manifest(manifest, root)
    return manifest


def read_asset(manifest, root, asset_id):
    """Return (descriptor, bytes) from one verified read, addressed only by ID."""
    validate_manifest(manifest)
    descriptor = next((item for item in iter_assets(manifest) if item["id"] == asset_id), None)
    if descriptor is None:
        raise ValueError("unknown public input asset id")
    return deepcopy(descriptor), _read_descriptor(root, descriptor)


def _image_payload(raw, source_mime):
    """Build precisely the pixels/bytes later sent to MCP, without resizing."""
    from PIL import ImageOps
    image, orientation = _image(raw, source_mime)
    try:
        returned, mime, transform = raw, source_mime, "none"
        if orientation != 1:
            transformed = ImageOps.exif_transpose(image)
            try:
                output = io.BytesIO()
                # JPEG CMYK is not a PNG mode; preserve geometry while converting color.
                if transformed.mode == "CMYK":
                    converted = transformed.convert("RGB")
                    transformed.close()
                    transformed = converted
                transformed.save(output, format="PNG")
                returned, mime = output.getvalue(), "image/png"
                width, height = transformed.size
            finally:
                transformed.close()
            transform = f"exif_transpose:{orientation}"
        else:
            width, height = image.size
    finally:
        image.close()
    return returned, mime, transform, width, height


def _image_response(descriptor, raw, index):
    returned, mime, transform, width, height = _image_payload(raw, descriptor["mime"])
    metadata = {"id": descriptor["id"], "index": index,
                "source_sha256": descriptor["sha256"], "returned_sha256": hashlib.sha256(returned).hexdigest(),
                "width": width, "height": height, "transform": transform,
                "source_mime": descriptor["mime"], "mime": mime}
    result = {"content": [{"type": "text", "text": json.dumps(metadata, ensure_ascii=False)},
                          {"type": "image", "data": base64.b64encode(returned).decode("ascii"), "mimeType": mime}],
              "structuredContent": metadata, "isError": False}
    # Include the JSON-RPC envelope and room for a normal request identifier.
    envelope = {"jsonrpc": "2.0", "id": "x" * 256, "result": result}
    if len(json.dumps(envelope, ensure_ascii=False, allow_nan=False).encode("utf-8")) + 1 > MAX_RESPONSE_BYTES:
        raise ValueError("encoded image MCP response exceeds byte limit")
    return result


def image_result(manifest, root, asset_id):
    """Return native MCP image content and compact provenance metadata.

    Original bytes are returned unchanged unless EXIF requires transposition.
    Transposed pixels use lossless PNG without resizing or cropping.
    """
    descriptor, raw = read_asset(manifest, root, asset_id)
    if descriptor["mime"] not in ("image/png", "image/jpeg"):
        raise ValueError("requested public input is not an image")
    index = next(i for i, item in enumerate(manifest["attachments"]) if item["id"] == asset_id)
    return _image_response(descriptor, raw, index)
