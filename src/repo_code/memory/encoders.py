"""Pinned real local model adapter. No synthetic semantic fallback."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
from ..util import RepoError, digest


class LocalEncoder:
    def __init__(self, profile):
        self.profile = profile; self.process = None; self.lock = threading.Lock()
        self.identity = digest({k: v for k, v in profile.items() if k != "models_path"})
        self.seconds = 0.0

    def _start(self):
        if self.process is not None: return
        image = self.profile.get("image", "")
        if not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", image): raise RepoError("ENCODER_PIN_REQUIRED")
        root = Path(self.profile["models_path"]).resolve(strict=True)
        manifest = self.profile.get("files", {})
        if not manifest: raise RepoError("ENCODER_FILES_REQUIRED")
        for name, expected in manifest.items():
            path = root / name
            if not path.resolve().is_relative_to(root) or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise RepoError("ENCODER_FILE_MISMATCH")
        actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
        if actual != set(manifest): raise RepoError("ENCODER_FILE_MEMBERSHIP")
        script = Path(__file__).with_name("encoder_worker.py").resolve()
        args = ["docker", "run", "--rm", "--interactive", "--network", "none", "--user", "65534:65534",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only", "--memory", "4g",
                "--cpus", "2", "--pids-limit", "64", "--tmpfs", "/tmp:rw,nosuid,nodev,size=128m",
                "--mount", f"type=bind,source={root},target=/models,readonly",
                "--mount", f"type=bind,source={script},target=/app/encoder_worker.py,readonly", image]
        env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "TEMP", "TMP", "DOCKER_HOST", "DOCKER_CONTEXT") if k in os.environ}
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, encoding="utf-8", env=env, bufsize=1)
        self.lines = queue.Queue()
        process=self.process;lines=self.lines
        def read():
            try:
                for line in process.stdout:lines.put(line)
            finally:lines.put(None)
        threading.Thread(target=read, daemon=True).start()
        ready = self._response(120)
        if not ready.get("ready") or ready.get("dimension") != self.profile["dimension"]:
            self.close(); raise RepoError("ENCODER_DIMENSION_MISMATCH")

    def _response(self, timeout):
        try: line = self.lines.get(timeout=timeout)
        except queue.Empty:
            self.close(); raise RepoError("ENCODER_TIMEOUT") from None
        if line is None: self.close(); raise RepoError("ENCODER_PROCESS_FAILED")
        try: value = json.loads(line)
        except ValueError: self.close(); raise RepoError("ENCODER_PROTOCOL_ERROR") from None
        if "error" in value: raise RepoError("ENCODER_MODEL_ERROR", value["error"])
        return value

    def invoke(self, operation, texts, query=""):
        with self.lock:
            started = time.monotonic()
            try:
                self._start()
                self.process.stdin.write(json.dumps({"operation": operation, "texts": texts, "query": query}, ensure_ascii=False)+"\n")
                self.process.stdin.flush()
                return self._response(90)["result"]
            except OSError:
                self.close();raise RepoError('ENCODER_PROCESS_FAILED') from None
            finally: self.seconds += time.monotonic()-started

    def embed(self, texts, query=False):
        prefix = "query: " if query else "passage: "
        return self.invoke("embed", [prefix+t for t in texts])

    def healthcheck(self):
        with self.lock:
            started=time.monotonic()
            try:self._start();return {'identity':self.identity,'dimension':self.profile['dimension']}
            except OSError:
                self.close();raise RepoError('SEMANTIC_MODELS_UNAVAILABLE') from None
            finally:self.seconds+=time.monotonic()-started

    def rerank(self, query, texts): return self.invoke("rerank", texts, query)

    def close(self):
        process = self.process
        if process is None: return
        self.process = None
        try:
            process.stdin.close(); process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            process.kill(); process.wait(timeout=5)
