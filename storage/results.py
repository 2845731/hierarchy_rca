from datetime import datetime, date
from decimal import Decimal
from pathlib import Path
import hashlib
import json
import math
import os
import threading


def json_safe(value):
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (Decimal, Path)):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    return value


def dumps(value, **kwargs):
    return json.dumps(json_safe(value), ensure_ascii=False, allow_nan=False, **kwargs)


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class CaseStorage:
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.Lock()
        root.mkdir(parents=True, exist_ok=True)

    def save(self, name: str, value):
        with self.lock:
            write_json(self.root / name, value)

    def append(self, name: str, value):
        with self.lock:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(dumps(value) + "\n")

    def text(self, name: str, value: str):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
