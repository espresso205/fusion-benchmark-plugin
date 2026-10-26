"""Optional, hash-verified source material for a static evaluation report.

This adapter never invokes a document parser, CAD importer or executable. Image
and PDF signatures only select display/download behavior; they do not certify
the document's contents. Unknown formats are copied as inert .bin downloads.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat


SCHEMA_VERSION = 'fusion-report-inputs/1'
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_FILES = 256
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024
_IMAGES = {'.png', '.jpg', '.jpeg', '.webp'}


class _AssetError(ValueError):
    """A fixed, public-safe attachment failure message."""


def _no_links(path, *, allow_missing=False):
    """Check each component, including Windows junction/reparse points."""
    path = Path(os.path.abspath(path))
    for component in [*reversed(path.parents), path]:
        try:
            information = component.lstat()
        except FileNotFoundError:
            if allow_missing:
                continue
            raise _AssetError('文件不存在。') from None
        if (stat.S_ISLNK(information.st_mode) or
                getattr(information, 'st_file_attributes', 0) & 0x400):
            raise _AssetError('不支持链接文件或链接目录。')
    return path


def _label(value, default):
    if value is None:
        return default
    if (not isinstance(value, str) or not value.strip() or len(value) > 120 or
            any(ord(character) < 32 for character in value)):
        raise ValueError('附件标签必须是非空短文本。')
    return value.strip()


def _validate_file(specification, *, preview=False):
    if not isinstance(specification, dict):
        raise ValueError('附件或预览必须是对象。')
    path, digest = specification.get('path'), specification.get('sha256')
    if not isinstance(path, str) or not path or len(path) > 2048:
        raise ValueError('附件必须指定相对文件路径。')
    if not isinstance(digest, str) or re.fullmatch('[0-9a-fA-F]{64}', digest) is None:
        raise ValueError('附件必须指定完整 SHA256。')
    _label(specification.get('label'), '资料')
    if preview and 'previews' in specification:
        raise ValueError('预览不能嵌套其他预览。')
    pages = specification.get('previews', [])
    if not isinstance(pages, list):
        raise ValueError('附件预览必须是列表。')
    for page in pages:
        _validate_file(page, preview=True)
    return 1 + len(pages)


def _load_manifest(manifest):
    if not isinstance(manifest, (str, os.PathLike)):
        raise TypeError('附件清单必须是文件路径。')
    try:
        path = _no_links(Path(manifest))
        with path.open('rb') as stream:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError('附件清单超过大小限制。')
        data = json.loads(raw.decode('utf-8-sig'))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError('无法读取附件清单。') from None
    if not isinstance(data, dict) or data.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('不支持此附件清单版本。')
    entries = data.get('cases')
    if not isinstance(entries, list):
        raise ValueError('附件清单的 cases 必须是列表。')
    cases, count = {}, 0
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError('附件案例必须是对象。')
        case_id, assets = entry.get('case'), entry.get('assets')
        if (not isinstance(case_id, str) or not case_id or len(case_id) > 128 or
                any(ord(character) < 32 for character in case_id)):
            raise ValueError('附件案例必须指定文本标识。')
        if case_id in cases:
            raise ValueError('附件清单不能重复指定同一案例。')
        if not isinstance(assets, list):
            raise ValueError('案例附件必须是列表。')
        for specification in assets:
            count += _validate_file(specification)
            if count > MAX_FILES:
                raise ValueError('附件和预览数量超过限制。')
        cases[case_id] = assets
    return path.parent, cases


def _source_path(root, relative):
    normalized = relative.replace('\\', '/')
    windows = PureWindowsPath(relative)
    components = normalized.split('/')
    if (windows.drive or windows.root or normalized.startswith('/') or
            any(part in ('', '.', '..') for part in components) or
            any(character in relative for character in (':', '\x00'))):
        raise _AssetError('文件路径超出资料目录或格式不受支持。')
    path = _no_links(root.joinpath(*components))
    if not path.resolve().is_relative_to(root.resolve()):
        raise _AssetError('文件路径超出资料目录。')
    return path


def _format(path):
    extension = PurePosixPath(path.replace('\\', '/')).suffix.lower()
    if extension in _IMAGES:
        return 'image', extension
    if extension == '.pdf':
        return 'pdf', '.pdf'
    if extension == '.dxf':
        return 'dxf', '.dxf'
    return 'download', '.bin'


def _check_signature(header, extension):
    valid = True
    if extension == '.png':
        valid = header.startswith(b'\x89PNG\r\n\x1a\n')
    elif extension in ('.jpg', '.jpeg'):
        valid = header.startswith(b'\xff\xd8\xff')
    elif extension == '.webp':
        valid = header[:4] == b'RIFF' and header[8:12] == b'WEBP'
    elif extension == '.pdf':
        valid = header.startswith(b'%PDF-')
    if not valid:
        raise _AssetError('文件内容与声明的格式不符。')


def _status(statuses):
    if all(status == 'completed' for status in statuses):
        return 'completed'
    if any(status in ('completed', 'partial') for status in statuses):
        return 'partial'
    return 'unavailable'


def _copy_file(specification, root, output, relative, available_bytes, *, preview=False):
    kind, extension = _format(specification['path'])
    result = {'kind': kind, 'url': None, 'status': 'unavailable', 'message': ''}
    destination = output / (relative + extension)
    temporary = destination.with_suffix(extension + '.part')
    created = False
    try:
        if preview and kind != 'image':
            raise _AssetError('预览仅支持 PNG、JPEG 或 WebP 图片。')
        source = _source_path(root, specification['path'])
        information = source.stat()
        if not stat.S_ISREG(information.st_mode):
            raise _AssetError('附件必须是普通文件。')
        if information.st_size > MAX_FILE_BYTES:
            raise _AssetError('文件超过 64 MiB 限制。')
        if information.st_size > available_bytes:
            raise _AssetError('资料总大小超过 256 MiB 限制。')
        with source.open('rb') as stream:
            _check_signature(stream.read(16), extension)
            stream.seek(0)
            if hashlib.file_digest(stream, 'sha256').hexdigest() != specification['sha256'].lower():
                raise _AssetError('文件校验不一致。')
            stream.seek(0)
            _no_links(destination, allow_missing=True)
            destination.parent.mkdir(parents=True, exist_ok=True)
            _no_links(temporary, allow_missing=True)
            if destination.exists():
                raise _AssetError('资料目标已存在。')
            count = 0
            with temporary.open('xb') as target:
                created = True
                while chunk := stream.read(_CHUNK_BYTES):
                    count += len(chunk)
                    if count > MAX_FILE_BYTES or count > available_bytes:
                        raise _AssetError('文件复制时超过资料大小限制。')
                    target.write(chunk)
        _no_links(source)
        _no_links(temporary)
        with temporary.open('rb') as copied:
            if hashlib.file_digest(copied, 'sha256').hexdigest() != specification['sha256'].lower():
                raise _AssetError('复制后的文件校验不一致。')
        _no_links(destination, allow_missing=True)
        # rename refuses replacement on Windows; the explicit check covers other
        # supported systems when a report directory is accidentally reused.
        if destination.exists():
            raise _AssetError('资料目标已存在。')
        temporary.rename(destination)
        result.update(url=(relative + extension), status='completed')
        return result, count
    except _AssetError as error:
        result['message'] = str(error)
    except (OSError, ValueError):
        result['message'] = '无法读取或复制此文件。'
    finally:
        if created and temporary.exists():
            temporary.unlink(missing_ok=True)
    return result, 0


def build_input_assets(manifest, output, case_ids):
    """Copy requested cases' input material and return safe display metadata.

    Paths in ``manifest`` are relative to that file's parent. Invalid manifest
    structure raises ValueError before writing. Individual file errors produce
    unavailable/partial items and preserve all other requested items. Call this
    only for a new report output directory; existing attachment files are never
    overwritten. The returned mapping contains no paths or hashes from sources.
    """
    root, cases = _load_manifest(manifest)
    if isinstance(case_ids, (str, bytes)):
        raise ValueError('请求的案例必须是文本标识列表。')
    requested = list(case_ids)
    if any(not isinstance(value, str) or not value for value in requested):
        raise ValueError('请求的案例必须是文本标识列表。')
    if len(set(requested)) != len(requested):
        raise ValueError('请求的案例不能重复。')
    output = _no_links(Path(output), allow_missing=True)
    results, total = {}, 0
    for case_id in requested:
        assets = cases.get(case_id)
        if not assets:
            continue
        case_index = len(results)
        file_index, items = 0, []
        for asset_index, specification in enumerate(assets):
            item, size = _copy_file(specification, root, output,
                                   f'inputs/{case_index}/{file_index}', MAX_TOTAL_BYTES - total)
            file_index += 1
            total += size
            item['label'] = _label(specification.get('label'), f'资料 {asset_index + 1}')
            item['previews'] = []
            for page_index, page in enumerate(specification.get('previews', [])):
                preview, size = _copy_file(page, root, output,
                    f'inputs/{case_index}/{file_index}', MAX_TOTAL_BYTES - total, preview=True)
                file_index += 1
                total += size
                preview['label'] = _label(page.get('label'), f'第 {page_index + 1} 页')
                preview.pop('kind')
                item['previews'].append(preview)
            item['status'] = _status([item['status'], *(page['status'] for page in item['previews'])])
            if item['status'] == 'partial' and not item['message']:
                item['message'] = '部分预览不可用。'
            items.append(item)
        results[case_id] = {'status': _status([item['status'] for item in items]), 'items': items}
    return results
