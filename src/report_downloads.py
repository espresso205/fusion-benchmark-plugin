"""Package verified model bytes for local inspection, independent of scoring."""
import hashlib
from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile

from .report_inputs import _no_links

MAX_MODEL_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024


def _copy_model(slot, output, remaining):
    digest = slot.get('sha256')
    if not isinstance(digest, str) or re.fullmatch('[a-f0-9]{64}', digest) is None:
        raise ValueError('invalid identity')
    source = _no_links(Path(slot['path']))
    if not source.is_absolute() or not source.is_file() or source.stat().st_size > MAX_MODEL_BYTES:
        raise ValueError('model unavailable')
    relative = 'models/' + digest + '.step'
    destination = _no_links(output / relative, allow_missing=True)
    with source.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
            raise ValueError('model changed')
        if destination.exists():
            with destination.open('rb') as saved:
                if hashlib.file_digest(saved, 'sha256').hexdigest() != digest:
                    raise ValueError('download changed')
            return relative, 0
        destination.parent.mkdir(parents=True, exist_ok=True)
        stream.seek(0)
        temporary = destination.with_suffix('.tmp')
        size = 0
        created = False
        try:
            with temporary.open('xb') as target:
                created = True
                while chunk := stream.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_MODEL_BYTES or size > remaining:
                        raise ValueError('download limit')
                    target.write(chunk)
            with temporary.open('rb') as saved:
                if hashlib.file_digest(saved, 'sha256').hexdigest() != digest:
                    raise ValueError('model changed')
            _no_links(source)
            temporary.rename(destination)
        finally:
            if created:
                temporary.unlink(missing_ok=True)
    return relative, size


def build_model_downloads(model_sources, output):
    """Return relative download URLs; ZIP includes only original candidates."""
    output = _no_links(Path(output), allow_missing=True)
    result = {'cases': {}, 'submission_zip': None}
    candidates = []
    total = 0
    for case, frames in model_sources.items():
        row = {}
        selections = [('candidate', frames.get('world', {}).get('candidate'), '生成 STEP'),
                      ('reference', frames.get('world', {}).get('reference'), '标准答案 STEP')]
        aligned = frames.get('scoring', {}).get('candidate')
        if aligned and aligned.get('sha256') != (selections[0][1] or {}).get('sha256'):
            selections.append(('aligned_candidate', aligned, '对齐后的 STEP'))
        for role, slot, label in selections:
            if not slot:
                continue
            try:
                relative, size = _copy_model(slot, output, MAX_TOTAL_BYTES - total)
                total += size
            except (OSError, ValueError, KeyError, TypeError):
                continue
            row[role] = {'url': relative, 'label': label}
            if role == 'candidate':
                safe = str(case)
                if not re.fullmatch('[A-Za-z0-9_-]{1,100}', safe):
                    safe = 'case-' + hashlib.sha256(safe.encode('utf-8')).hexdigest()[:24]
                candidates.append((relative, safe + '/submission.step'))
        if row:
            result['cases'][case] = row
    if candidates:
        temporary = output / 'submission.zip.tmp'
        created = False
        try:
            with temporary.open('xb') as stream:
                created = True
                with ZipFile(stream, 'w', compression=ZIP_DEFLATED) as archive:
                    for relative, name in candidates:
                        archive.write(output / relative, name)
            destination = output / 'submission.zip'
            if destination.exists():
                raise FileExistsError('submission archive already exists')
            temporary.rename(destination)
            result['submission_zip'] = {'url': 'submission.zip', 'label': '下载可用生成 STEP（ZIP）'}
        except OSError:
            pass
        finally:
            if created:
                temporary.unlink(missing_ok=True)
    return result
