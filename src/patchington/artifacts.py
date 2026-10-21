"""Content-addressed evidence with canonical JSON and corruption checks."""

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path


class ArtifactError(ValueError):
    """Invalid, missing or corrupted evidence."""


class ArtifactStore:
    MAX_BYTES = 16 * 1024 * 1024

    def __init__(self, root: Path):
        root = Path(root).absolute()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.is_symlink() or not root.is_dir():
            raise ArtifactError("artifact root must be a real directory")
        self.root = root.resolve()

    def _path(self, digest: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ArtifactError("invalid artifact digest")
        path = self.root / digest[:2] / digest[2:]
        if path.parent.is_symlink() or path.is_symlink():
            raise ArtifactError("symlink evidence refused")
        return path

    def put(self, data: bytes) -> str:
        if not isinstance(data, bytes) or len(data) > self.MAX_BYTES:
            raise ArtifactError("evidence must be bounded bytes")
        digest = hashlib.sha256(data).hexdigest()
        path = self._path(digest)
        path.parent.mkdir(mode=0o700, exist_ok=True)
        if path.exists():
            if self.get(digest) != data:
                raise ArtifactError("artifact digest collision")
            return digest
        fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), 0o400)
            try:
                os.link(name, path, follow_symlinks=False)
            except FileExistsError:
                self.get(digest)
        finally:
            os.unlink(name)
        return digest

    def get(self, digest: str) -> bytes:
        path = self._path(digest)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ArtifactError("evidence must be a regular file")
                data = stream.read(self.MAX_BYTES + 1)
        except OSError as exc:
            raise ArtifactError("artifact unavailable") from exc
        if len(data) > self.MAX_BYTES or hashlib.sha256(data).hexdigest() != digest:
            raise ArtifactError("artifact digest mismatch")
        return data

    def put_json(self, value: dict) -> str:
        if not isinstance(value, dict):
            raise ArtifactError("JSON evidence must be an object")
        return self.put(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False, allow_nan=False).encode())

    def get_json(self, digest: str) -> dict:
        try:
            value = json.loads(self.get(digest))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ArtifactError("invalid JSON evidence") from exc
        if not isinstance(value, dict):
            raise ArtifactError("JSON evidence must be an object")
        return value
