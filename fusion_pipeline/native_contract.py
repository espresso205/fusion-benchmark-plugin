"""Versioned native byte identity; CAD recognition/health require trusted reopen.

No dependency, configuration or geometry claim is inferred from a file signature.
"""
from pathlib import Path
import hashlib
import re
import stat
import zipfile

IDENTITY_FIELDS = ('run_id', 'case_id', 'attempt_id', 'protocol_id', 'cad_backend', 'cad_config_sha256')
SCHEMA = 'cad-native/1'
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')
_SPECS = {
    'fusion': {'filename': 'submission.f3d', 'format': 'f3d', 'document_type': 'Design'},
    'solidworks': {'filename': 'submission.SLDPRT', 'format': 'sldprt', 'document_type': 'Part'},
}


def submission_spec(backend):
    if not isinstance(backend, str) or backend not in _SPECS:
        raise ValueError('unknown CAD backend')
    return dict(_SPECS[backend])


def plain_file(path):
    """Reject aliases before resolution, including parent junctions and hardlinks."""
    path = Path(path).absolute()
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('native evidence cannot use symlink/reparse aliases')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size <= 0:
        raise ValueError('native evidence must be a nonempty regular file without aliases')
    return path


def file_hash(path):
    path = plain_file(path)
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def validate_identity(identity):
    if not isinstance(identity, dict):
        raise ValueError('native identity must be an object')
    for key in ('run_id', 'case_id', 'attempt_id'):
        value = identity.get(key)
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise ValueError('invalid native ' + key)
    protocol = identity.get('protocol_id')
    if not isinstance(protocol, str) or not protocol.strip() or len(protocol) > 256 or any(ord(c) < 32 for c in protocol):
        raise ValueError('invalid native protocol_id')
    submission_spec(identity.get('cad_backend'))
    digest = identity.get('cad_config_sha256')
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        raise ValueError('invalid CAD config digest')


def validate_native_descriptor(value, *, identity=None, path=None):
    fields = set(IDENTITY_FIELDS) | {'schema', 'format', 'document_type', 'path', 'bytes',
                                    'sha256', 'configuration', 'dependency_sha256'}
    if not isinstance(value, dict) or set(value) != fields or value['schema'] != SCHEMA:
        raise ValueError('invalid native descriptor schema')
    validate_identity(value)
    if identity is not None:
        validate_identity(identity)
        if any(value[key] != identity[key] for key in IDENTITY_FIELDS):
            raise ValueError('native run/case/attempt/protocol/backend/config identity mismatch')
    spec = submission_spec(value['cad_backend'])
    if any(value[key] != spec[key] for key in ('format', 'document_type')) or value['path'] != spec['filename']:
        raise ValueError('native document format/path mismatch')
    for key in ('sha256', 'dependency_sha256'):
        if not isinstance(value[key], str) or not _HASH.fullmatch(value[key]):
            raise ValueError('invalid native evidence hash')
    if type(value['bytes']) is not int or value['bytes'] <= 0:
        raise ValueError('invalid native byte count')
    configuration = value['configuration']
    if value['cad_backend'] == 'fusion':
        if configuration is not None:
            raise ValueError('Fusion configuration is not applicable')
    elif not isinstance(configuration, str) or not configuration.strip() or len(configuration) > 1024 or any(ord(c) < 32 for c in configuration):
        raise ValueError('SolidWorks requires an explicit configuration')
    if path is not None:
        path = plain_file(path)
        if path.name != spec['filename']:
            raise ValueError('native filename mismatch')
        if path.stat().st_size != value['bytes'] or file_hash(path) != value['sha256']:
            raise ValueError('native file changed')
        # SW 2026 native containers have file-dependent headers, unlike legacy
        # compound storage. Bind opaque bytes here; the Gateway requires an
        # actual native save receipt and trusted reopen verifies Part type.
        if value['cad_backend'] == 'fusion' and not zipfile.is_zipfile(path):
            raise ValueError('Fusion native file requires ZIP archive signature')
    return True


def make_native_descriptor(path, *, identity, configuration=None, dependency_sha256):
    validate_identity(identity)
    path = plain_file(path)
    spec = submission_spec(identity['cad_backend'])
    value = {key: identity[key] for key in IDENTITY_FIELDS}
    value.update(schema=SCHEMA, format=spec['format'], document_type=spec['document_type'],
                 path=path.name, bytes=path.stat().st_size, sha256=file_hash(path),
                 configuration=configuration, dependency_sha256=dependency_sha256)
    validate_native_descriptor(value, identity=identity, path=path)
    return value
