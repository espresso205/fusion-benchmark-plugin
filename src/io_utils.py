"""Strict JSON and stable evidence hashes."""
import datetime as dt
import hashlib
import json
import math
from pathlib import Path


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def strict_json(path):
    def pairs(items):
        obj = {}
        for k, v in items:
            if k in obj:
                raise ValueError(f'duplicate JSON key: {k}')
            obj[k] = v
        return obj
    def reject(value):
        raise ValueError(f'non-finite JSON number: {value}')
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            reject(value)
        return number
    return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=pairs,
                      parse_constant=reject, parse_float=finite_float)


def tree_hashes(root):
    root = Path(root)
    return {str(p.relative_to(root)).replace('\\', '/'): sha(p)
            for p in sorted(root.rglob('*')) if p.is_file() and p.suffix in ('.py', '.json', '.toml')
            and '__pycache__' not in p.parts}
