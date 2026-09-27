"""PC-side: push the kernel to Kaggle, watch ntfy for the tunnel URL, stop, quota."""

import json
import pathlib
import shutil
import subprocess
import tempfile
import time
import urllib.request

from . import state

H = "https:"
NTFY = H + "//ntfy.sh"
KERNEL_CANDIDATES = [
    pathlib.Path(__file__).resolve().parent.parent / "kernel" / "kaggle_kernel.py",  # repo checkout
    pathlib.Path(__file__).resolve().parent / "kaggle_kernel.py",                    # installed package
]
KAGGLE_KERNEL_TEMPLATE = next(p for p in KERNEL_CANDIDATES if p.exists())

PLACEHOLDERS = ("__FLS_KEY__", "__FLS_TOPIC__", "__FLS_MODEL_NAME__",
                "__FLS_MODEL_URL__", "__FLS_GGUF_FILE__", "__FLS_DATASET__")


def _kaggle(*args, timeout=120) -> subprocess.CompletedProcess:
    return subprocess.run(["kaggle", *args], capture_output=True, text=True, timeout=timeout)


def kaggle_installed() -> bool:
    return shutil.which("kaggle") is not None


def kaggle_token_ok() -> bool:
    return (pathlib.Path.home() / ".kaggle" / "kaggle.json").exists()


def render_kernel(st: dict) -> str:
    src = KAGGLE_KERNEL_TEMPLATE.read_text()
    model_url = f"{H}//huggingface.co/{st['model_repo']}/resolve/main/{st['model_file']}"
    subs = {
        "__FLS_KEY__": st["api_key"],
        "__FLS_TOPIC__": st["ntfy_topic"],
        "__FLS_MODEL_NAME__": st["model_name"],
        "__FLS_MODEL_URL__": model_url,
        "__FLS_GGUF_FILE__": st["model_file"],
        "__FLS_DATASET__": st["dataset_slug"],
    }
    for k, v in subs.items():
        src = src.replace(k, v)
    for ph in PLACEHOLDERS:
        if ph in src:
            raise RuntimeError(f"placeholder {ph} not substituted")
    return src


def build_metadata(st: dict, workdir: pathlib.Path) -> None:
    slug = f"{st['kaggle_username']}/{st['kernel_slug']}"
    meta = {
        "id": slug,
        "code_file": "kaggle_kernel.py",
        "language": "python",
        "kernel_type": "script",
        "is_private": True,
        "enable_gpu": True,
        "enable_internet": True,
        "dataset_sources": [st["dataset_slug"]] if st["dataset_slug"] else [],
        "competition_sources": [],
        "kernel_sources": [],
        "model_sources": [],
    }
    meta["id"] = slug
    (workdir / "kernel-metadata.json").write_text(json.dumps(meta, indent=2))


def push(st: dict) -> None:
    """Render + push the kernel, one version per call."""
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        (td / "kaggle_kernel.py").write_text(render_kernel(st))
        build_metadata(st, td)
        r = _kaggle("kernels", "push", "-p", str(td), timeout=300)
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0:
            raise RuntimeError("kaggle kernels push failed:\n" + out.strip()[-2000:])
        print(out.strip()[-500:])


def kernel_status(st: dict) -> str:
    r = _kaggle("kernels", "status", f"{st['kaggle_username']}/{st['kernel_slug']}")
    out = (r.stdout or "") + (r.stderr or "")
    for s in ("complete", "running", "queued", "error", "cancelAcknowledged", "cancelRequested"):
        if s in out.lower():
            return s if s != "complete" else "running"
    return "unknown: " + out.strip()[:200]


def poll_ntfy(st: dict, since_minutes: int = 60) -> list[dict]:
    """Read messages published by the kernel (keyless; topic is the shared secret)."""
    url = f"{NTFY}/{st['ntfy_topic']}/json?poll=1&since={since_minutes}m"
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            events = [json.loads(line) for line in r.read().splitlines() if line.strip()]
    except Exception as e:
        print("ntfy poll failed:", e)
        return []
    return [m for m in events if m.get("event") == "message"]


def wait_for_tunnel(st: dict, timeout_min: int = 35) -> dict | None:
    """Poll ntfy until the kernel's 'up' message (with URL) arrives or timeout."""
    print("waiting for kernel boot + tunnel (5-15 min; model load after)...")
    deadline = time.time() + timeout_min * 60
    seen_up = None
    while time.time() < deadline:
        msgs = poll_ntfy(st, since_minutes=15)
        for m in reversed(msgs):  # newest last in ntfy poll
            try:
                data = json.loads(m["message"])
            except Exception:
                continue
            if data.get("type") == "up" and data.get("url"):
                return data
            if data.get("type") == "heartbeat":
                seen_up = data
        status = kernel_status(st)
        if "error" in status or "cancel" in status:
            print("kernel died:", status, "— re-pushing..." if seen_up is None else "")
            if seen_up is None:
                return None
        time.sleep(20)
    return None


def stop(st: dict) -> str:
    """Stop the server: prefer POST through the live tunnel, else tell the user."""
    msgs = poll_ntfy(st, since_minutes=720)
    url = None
    for m in reversed(msgs):
        try:
            d = json.loads(m["message"])
            if d.get("type") in ("up", "heartbeat") and d.get("url"):
                url = d["url"]
        except Exception:
            continue
    if url:
        import urllib.error
        req = urllib.request.Request(url + "/admin/shutdown", data=b"{}",
                                     headers={"Authorization": "Bearer " + st["api_key"],
                                              "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=30)
            return "shutdown requested via tunnel (session ends in seconds)"
        except Exception as e:
            print("tunnel shutdown failed:", e)
    return ("couldn't reach the tunnel. Open kaggle.com/code → Your Work → "
            f"'{st['kernel_slug']}' → Stop (⋮ menu) to free quota.")


def pin_ok(st: dict) -> str | None:
    """Version-pinning note: report the pinned versions vs what check-updates found."""
    return None  # check-updates lives in cli.py (needs web access)
