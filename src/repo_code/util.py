from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

class RepoError(Exception):
    def __init__(self, code: str, message: str = ""):
        self.code, self.message = code, message or code
        super().__init__(self.message)


class ProviderNotDispatched(RepoError):
    """A trusted provider wrapper proved rejection before the inference call.

    Ordinary provider/network failures must retain unknown-usage reservations.
    Error codes or missing Usage alone are never proof of non-dispatch.
    """
    def __init__(self,code,message=""):
        super().__init__(code,message)
        self.provider_details={'dispatch_status':'NOT_DISPATCHED','stage':'host_admission'}


def canonical(value: Any) -> bytes:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    def check(v):
        if isinstance(v, dict):
            if any(not isinstance(k, str) for k in v):
                raise RepoError("INVALID_JSON_KEY")
            for x in v.values(): check(x)
        elif isinstance(v, (list, tuple)):
            for x in v: check(x)
    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def uid(prefix="") -> str:
    return prefix + uuid.uuid4().hex


def now() -> float:
    return time.time()


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        if hasattr(os,"fchmod"):os.fchmod(fd, mode)
        else:os.chmod(tmp,mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
        if os.name == "posix":
            d = os.open(path.parent, os.O_RDONLY)
            try: os.fsync(d)
            finally: os.close(d)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def private_dir(path: Path) -> Path:
    path = path.absolute()
    for p in [path, *path.parents]:
        if p.is_symlink(): raise RepoError("LINKED_PRIVATE_PATH", str(p))
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def redact(text: str) -> str:
    text = re.sub(r"(?i)(api[_-]?key|authorization|password|secret)(\s*[:=]\s*)([^\s,;]+)", r"\1\2[REDACTED]", text)
    return re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[REDACTED]", text)


class Faults:
    """Test-owned injection; never configurable through the worker protocol."""
    def __init__(self, callback=None): self.callback = callback
    def hit(self, name: str):
        if self.callback: self.callback(name)


def redact_data(value):
    """Best-effort display/audit redaction, not a general secret detector."""
    if isinstance(value,str):return redact(value)
    if isinstance(value,list):return [redact_data(x) for x in value]
    if isinstance(value,dict):
        return {k:("[REDACTED]" if k.lower() in {"api_key","password","authorization","secret"} else redact_data(v)) for k,v in value.items()}
    return value


from contextlib import contextmanager
@contextmanager
def execution_lock(path):
    """One live executor per Run, including competing local CLI processes."""
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open("a+b") as handle:
        try:
            if os.name=="nt":
                import msvcrt
                handle.write(b"0");handle.flush();handle.seek(0)
                msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:raise RepoError("EXECUTOR_ALREADY_ACTIVE") from None
        try:yield
        finally:
            if os.name=="nt":handle.seek(0);msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(handle,fcntl.LOCK_UN)


def release_content_identity(release):
    """Behavior-bearing release identity, excluding lineage labels/generation.

    This is byte/content identity, NOT a claim to detect semantic equivalence.
    Learning provenance is enforced independently through the lineage graph.
    """
    if hasattr(release, "model_dump"):
        release = release.model_dump(mode="json")
    return "release-content:" + digest({key: release[key] for key in (
        "protocol_version", "components", "worker_build_digest",
        "dependencies_digest", "gate_policy_digest")})
