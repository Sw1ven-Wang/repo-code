"""Explicit one-time download; runtime workers only load audited offline files."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MODELS = {"embedding": "intfloat/multilingual-e5-small", "reranker": "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--build", action="store_true")
    parser.add_argument("--container-download", action="store_true", help="Download from the Linux network path")
    parser.add_argument("--download-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(); args.models_root.mkdir(parents=True, exist_ok=True)
    revisions = {}
    if args.container_download:
        subprocess.run(["docker","run","--rm","--entrypoint","python","--mount",
            f"type=bind,source={args.models_root.resolve()},target=/models","--mount",
            f"type=bind,source={Path(__file__).resolve()},target=/app/scripts/setup_memory_models.py,readonly",
            "repo-code-native-validation:1.5.1","/app/scripts/setup_memory_models.py","--models-root","/models","--output","/models/unused.json","--download-only"],check=True)
        revisions=json.loads((args.models_root/"model-revisions.json").read_text())
    for kind, model in ([] if args.container_download else MODELS.items()):
        with urllib.request.urlopen("https://huggingface.co/api/models/"+model, timeout=30) as response:
            info = json.load(response)
        revision = info["sha"]; revisions[kind] = {"model": model, "revision": revision}
        previous=args.models_root/'model-revisions.json'
        known=json.loads(previous.read_text(encoding='utf-8')) if previous.exists() else {}
        folder=args.models_root/kind
        if folder.exists() and any(folder.rglob('*')) and known.get(kind)!={'model':model,'revision':revision}:
            raise ValueError('Existing model files have a different or unverified revision; use a fresh models directory')
        available = {x["rfilename"] for x in info["siblings"]}
        has_safe = "model.safetensors" in available
        names = [n for n in available if n.endswith((".json", ".txt", ".model")) and not n.startswith(("onnx/", "openvino/"))]
        names += ["model.safetensors" if has_safe else "pytorch_model.bin"]
        for name in sorted(set(names)):
            if ".." in Path(name).parts: raise ValueError("unsafe model path")
            target = args.models_root / kind / name; target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists(): continue
            temporary = target.with_suffix(target.suffix+".partial")
            print("Downloading", kind, name, flush=True)
            with urllib.request.urlopen(f"https://huggingface.co/{model}/resolve/{revision}/{name}", timeout=120) as response, temporary.open("wb") as out:
                while chunk := response.read(1024*1024): out.write(chunk)
            temporary.replace(target)
    if args.download_only:
        (args.models_root/"model-revisions.json").write_text(json.dumps(revisions),encoding="utf-8")
        return
    if args.build:
        subprocess.run(["docker", "build", "--pull=false", "-t", "repo-code-memory-worker:2.0",
                        "-f", str(ROOT/"examples/sandbox/Dockerfile.memory-worker"), str(ROOT/"examples/sandbox")], check=True)
    image = json.loads(subprocess.check_output(["docker", "image", "inspect", "repo-code-memory-worker:2.0", "--format", "{{json .RepoDigests}}"], text=True))[0]
    files = {p.relative_to(args.models_root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(args.models_root.rglob("*")) if p.is_file()}
    profile = {"image": image, "models_path": str(args.models_root.resolve()), "models": revisions,
               "dimension": 384, "normalization": "l2", "prefix_policy": "e5-query-passage-v1", "files": files}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(profile, indent=2)+"\n", encoding="utf-8")


if __name__ == "__main__": main()
