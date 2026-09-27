#!/usr/bin/env python3
"""free-llm-server (fls) — ONE FILE.

Run your own OpenAI-compatible LLM server on Kaggle's free GPUs and wire it
to Hermes / any OpenAI-speaking client. The Kaggle kernel is embedded below
(KERNEL_TEMPLATE) and rendered with your settings at push time — no package,
no other files needed.

    python fls.py                    interactive menu
    python fls.py setup              first-time: kaggle CLI + token + key
    python fls.py start [--watch]    push kernel -> tunnel -> wire Hermes
    python fls.py stop               kill the kernel (frees GPU quota)
    python fls.py status             uptime, URL, quota
    python fls.py logs               open the Kaggle kernel page
    python fls.py swap               change the GGUF model
    python fls.py fallback           flip Hermes back to its previous model
    python fls.py client-config      paste-ready config for any client
    python fls.py check-updates      pinned-vs-latest ollama check
"""
import argparse
import datetime as dt
import json
import pathlib
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser

DIM = "\033[2m"; BOLD = "\033[1m"; END = "\033[0m"

# ============================================================================
# state (~/.free-llm-server/state.json)
# ============================================================================
STATE_DIR = pathlib.Path.home() / ".free-llm-server"
STATE_FILE = STATE_DIR / "state.json"

DEFAULTS = {
    "setup_done": False,
    "kaggle_username": "",
    "kernel_slug": "free-llm-server",
    # pinned versions / sources (version pinning addon)
    "ollama_version": "latest",
    "model_repo": "huihui-ai/Huihui-Qwen3.8-27B-abliterated-GGUF",
    "model_file": "Huihui-Qwen3.8-27B-abliterated-UD-Q4_K_XL.gguf",
    "model_name": "qwen3.8-27b-abliterated",
    # optional: Kaggle Dataset slug holding the GGUF (empty = download from HF each session)
    "dataset_slug": "",
    # random bearer key generated at setup
    "api_key": "",
    # ntfy topic used as the kernel -> PC status channel
    "ntfy_topic": "",
    # saved hermes model config, so `fallback` can restore it
    "hermes_backup": None,
    # session log for quota guard: [{pushed_at, stopped_at, url}]
    "sessions": [],
    "gpu_warn_hours": 25.0,
    "weekly_gpu_budget_hours": 30.0,
}


def load() -> dict:
    st = dict(DEFAULTS)
    if STATE_FILE.exists():
        try:
            st.update(json.loads(STATE_FILE.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return st


def save(st: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, indent=2))


def is_setup(st: dict) -> bool:
    return bool(st["setup_done"] and st["kaggle_username"] and st["api_key"])


def new_api_key() -> str:
    return "sk-fls-" + secrets.token_urlsafe(24)


def session_open(st: dict) -> dict | None:
    for s in st["sessions"]:
        if s.get("stopped_at") is None:
            return s
    return None


def gpu_hours_last_7d(st: dict) -> float:
    now = time.time()
    total = 0.0
    for s in st["sessions"]:
        start = s["pushed_at"]
        end = s.get("stopped_at") or now
        if end > now - 7 * 86400:
            total += (min(end, now) - start) / 3600.0
    return round(total, 2)


# ============================================================================
# the Kaggle kernel, embedded verbatim (rendered with your settings at push)
# placeholders: __FLS_KEY__ __FLS_TOPIC__ __FLS_MODEL_NAME__
#               __FLS_MODEL_URL__ __FLS_GGUF_FILE__ __FLS_DATASET__
# ============================================================================
KERNEL_TEMPLATE = r"""# ============================================================
#  free-llm-server — Kaggle kernel half (v5)
#  Pushed by the PC-side CLI (kaggle kernels push).
#  Idempotent, restart-safe. Serves an OpenAI-compatible API
#  through a bearer-token proxy + Cloudflare quick tunnel and
#  publishes the tunnel URL to a private ntfy topic the PC
#  listens on. Do NOT run this locally — it targets Kaggle's
#  T4x2 image (needs nvidia GPUs).
#  Placeholders (injected by the PC tool before push):
#    __FLS_KEY__  __FLS_TOPIC__  __FLS_MODEL_NAME__
#    __FLS_MODEL_URL__  __FLS_GGUF_FILE__  __FLS_DATASET__
# ============================================================
import json
import os
import pathlib
import re
import shutil
import subprocess
import time
import urllib.request

KEY = "__FLS_KEY__"
TOPIC = "__FLS_TOPIC__"
MODEL = "__FLS_MODEL_NAME__"
MODEL_URL = "__FLS_MODEL_URL__"
DATASET = "__FLS_DATASET__"  # "user/dataset-slug" to mount a pre-uploaded GGUF, else ""
GGUF_FILE = "__FLS_GGUF_FILE__"
GGUF = "/root/models/" + GGUF_FILE

STATUS = {"url": None, "started_at": time.time(), "requests": 0, "last_request": None}

H = "https:"
GH = H + "//github.com/"
HF = H + "//huggingface.co/"
U_OLLAMA = GH + "ollama/ollama/releases/latest/download/ollama-linux-amd64.tar.zst"
U_OLLAMA2 = H + "//ollama.com/download/ollama-linux-amd64.tar.zst"
U_CF = GH + "cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
PAT_URL = H + r"//[-a-z0-9]+\.trycloudflare\.com"


def run(cmd, **kw):
    return subprocess.run(cmd, check=False, **kw)


def publish(event: dict):
    # Kernel -> PC via ntfy (keyless, topic name is the shared secret).
    try:
        rq = urllib.request.Request(
            H + "//ntfy.sh/" + TOPIC,
            data=json.dumps(event).encode(),
            headers={"Title": "fls"},
        )
        urllib.request.urlopen(rq, timeout=15)
    except Exception as e:
        print("[ntfy] publish failed:", e)


def fetch(urls, dest):
    for u in urls:
        for attempt in range(3):
            print("   GET", u, f"(try {attempt + 1}/3)")
            r = run(["wget", "-c", "--tries=2", "--timeout=60", u, "-O", dest])
            if r.returncode == 0 and os.path.exists(dest) and os.path.getsize(dest) > 0:
                return
            print("   wget failed, rc =", r.returncode)
    raise SystemExit("All download attempts failed.")


print("host:", run(["ldd", "--version"], capture_output=True, text=True).stdout.splitlines()[0])

# ---- 1) engine ----
print("[1/7] ensuring zstd...")
run(["bash", "-c", "apt-get update -qq && apt-get install -y -qq zstd"])
OLL = shutil_oll = "/root/ollama/bin/ollama"
if not (os.path.exists(OLL) or shutil.which("ollama")):
    print("[1/7] installing Ollama via official installer (ollama.com)...")
    r = run(["bash", "-c", "set -o pipefail; curl -fsSL " + H + "//ollama.com/install.sh | sh"])
    if r.returncode != 0 or not (shutil.which("ollama") or os.path.exists(OLL)):
        print("[1/7] installer failed — falling back to .tar.zst download...")
        fetch([U_OLLAMA, U_OLLAMA2], "/root/ollama.tar.zst")
        if run(["tar", "--zstd", "-xf", "/root/ollama.tar.zst", "-C", "/usr/local"]).returncode != 0:
            raise SystemExit("extraction failed (zstd missing?)")
        os.remove("/root/ollama.tar.zst")
OLL = shutil.which("ollama") or OLL
print("[1/7] engine OK:", OLL)

# ---- 2) model: prefer mounted Kaggle dataset, else download from HF ----
os.makedirs("/root/models", exist_ok=True)
if os.path.exists(GGUF) and os.path.getsize(GGUF) > 1e9:
    print("[2/7] model already on disk")
elif DATASET:
    hits = list(pathlib.Path("/kaggle/input").rglob("*.gguf"))
    if not hits:
        raise SystemExit("dataset mounted but no .gguf found in /kaggle/input")
    GGUF = str(hits[0])
    print("[2/7] model from dataset:", GGUF)
else:
    print("[2/7] downloading model (~8-10 min)...")
    fetch([MODEL_URL], GGUF)
print(f"[2/7] model OK: {os.path.getsize(GGUF) / 1e9:.1f} GB")

# ---- 3) ollama serve ----
env = dict(os.environ, OLLAMA_HOST="127.0.0.1:11434", OLLAMA_KEEP_ALIVE="-1")
srv = subprocess.Popen([OLL, "serve"], env=env,
                       stdout=open("/root/serve.log", "w"), stderr=subprocess.STDOUT)
for _ in range(30):
    time.sleep(2)
    try:
        urllib.request.urlopen(H + "//127.0.0.1:11434/", timeout=3)
        break
    except Exception:
        pass
print("[3/7] ollama serve up")

# ---- 4) import GGUF (no re-download) ----
pathlib.Path("/root/Modelfile").write_text("FROM " + GGUF + "\nPARAMETER num_ctx 16384\n")
print("[4/7] importing GGUF into Ollama (~1-2 min)...")
if run([OLL, "create", MODEL, "-f", "/root/Modelfile"], env=env).returncode != 0:
    print(open("/root/serve.log").read()[-600:])
    raise SystemExit("ollama create failed")
print("[4/7] imported as", MODEL)

# ---- 5) bearer-token auth proxy on :8080 (+ usage counter + shutdown endpoint) ----
try:
    import httpx
    import fastapi
    import uvicorn
except ImportError:
    run(["pip", "install", "-q", "httpx", "fastapi", "uvicorn"])
pathlib.Path("/root/proxy.py").write_text(f'''
import json, time
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
KEY = "{KEY}"
STATUS = {json.dumps(STATUS)}
app = FastAPI()
c = httpx.AsyncClient(base_url="http" + "://127.0.0.1:11434",
                      timeout=httpx.Timeout(600.0, connect=10.0))

@app.post("/admin/shutdown")
async def shutdown(req: Request):
    if req.headers.get("authorization", "") != "Bearer " + KEY:
        return {{}} if False else StreamingResponse(iter([b'{{"error":"unauthorized"}}']),
                                                    status_code=401, media_type="application/json")
    import subprocess as sp, threading
    def die():
        sp.run(["poweroff"])  # ends the Kaggle session cleanly
    threading.Thread(target=die, daemon=True).start()
    return {{"ok": True}}

@app.api_route("/{{p:path}}", methods=["GET", "POST"])
async def px(p: str, req: Request):
    if req.headers.get("authorization", "") != "Bearer " + KEY:
        return StreamingResponse(iter([b'{{"error":"unauthorized"}}']), status_code=401,
                                 media_type="application/json")
    body = await req.body()
    r = await c.send(c.build_request(req.method, "/" + p, content=body,
        headers={{"Content-Type": req.headers.get("content-type", "application/json")}}), stream=True)
    if p == "chat/completions" and req.method == "POST":
        STATUS["requests"] += 1
        STATUS["last_request"] = time.time()
        try:
            data = json.loads(body)
            STATUS["last_prompt"] = (data.get("model", ""), len(json.dumps(data.get("messages", []))) // 4)
        except Exception:
            pass
    return StreamingResponse(r.aiter_raw(), background=BackgroundTask(r.aclose),
                             status_code=r.status_code,
                             media_type=r.headers.get("content-type", "application/json"))
''')
px = subprocess.Popen(["python", "-m", "uvicorn", "proxy:app", "--host", "127.0.0.1", "--port", "8080"],
                      cwd="/root", stdout=open("/root/proxy.log", "w"), stderr=subprocess.STDOUT)
time.sleep(4)
print("[5/7] auth proxy up on :8080")

# ---- 6) public tunnel ----
fetch([U_CF], "/root/cloudflared")
os.chmod("/root/cloudflared", 0o755)
tun = subprocess.Popen(["/root/cloudflared", "tunnel", "--url", H + "//127.0.0.1:8080", "--no-autoupdate"],
                       stdout=open("/root/tunnel.log", "w"), stderr=subprocess.STDOUT)
url = None
for _ in range(30):
    time.sleep(2)
    m = re.search(PAT_URL, pathlib.Path("/root/tunnel.log").read_text(errors="ignore"))
    if m:
        url = m.group(0)
        break
if not url:
    print("[6/7] tunnel failed — log tail:")
    print(open("/root/tunnel.log").read()[-600:])
    raise SystemExit("Re-run to retry the tunnel.")

STATUS["url"] = url
publish({"type": "up", "url": url, "model": MODEL, "started_at": STATUS["started_at"]})
print("[6/7] PUBLIC URL:", url)
print("TUNNEL_URL: " + url)

# ---- 7) self-test + periodic usage/status pings (keeps PC informed) ----
def ask(key):
    h = {"Content-Type": "application/json"}
    if key:
        h["Authorization"] = "Bearer " + key
    rq = urllib.request.Request(H + "//127.0.0.1:8080/v1/chat/completions",
        data=json.dumps({"model": MODEL,
                         "messages": [{"role": "user", "content": "Reply with exactly: ONLINE"}],
                         "max_tokens": 10}).encode(), headers=h)
    return json.load(urllib.request.urlopen(rq, timeout=300))["choices"][0]["message"]["content"].strip()

try:
    print(f"[7/7] WITH key: {ask(KEY)!r}  (first call loads model into VRAM — wait a few min)")
except Exception as e:
    print("[7/7] self-test failed:", e)
try:
    ask(None)
    print("[7/7] WARNING: no-key request passed — key NOT enforced!")
except Exception:
    print("[7/7] no-key rejected (401) — key enforcement OK")

import threading
def pinger():
    while True:
        time.sleep(600)
        publish({"type": "heartbeat", "url": url, "requests": STATUS["requests"],
                 "uptime_s": int(time.time() - STATUS["started_at"])})
threading.Thread(target=pinger, daemon=True).start()

print("=" * 60)
print("LIVE. PC side watches the ntfy topic — keep this session running.")
print("Idle kills the session · 12h cap · ~30h/week quota — stop when done.")
"""

# ============================================================================
# kaggle ops: push, watch ntfy for the tunnel URL, stop
# ============================================================================
H = "https:"
NTFY = H + "//ntfy.sh"
PLACEHOLDERS = ("__FLS_KEY__", "__FLS_TOPIC__", "__FLS_MODEL_NAME__",
                "__FLS_MODEL_URL__", "__FLS_GGUF_FILE__", "__FLS_DATASET__")


def _kaggle(*args, timeout=120) -> subprocess.CompletedProcess:
    return subprocess.run(["kaggle", *args], capture_output=True, text=True, timeout=timeout)


def kaggle_installed() -> bool:
    return shutil.which("kaggle") is not None


def kaggle_token_ok() -> bool:
    return (pathlib.Path.home() / ".kaggle" / "kaggle.json").exists()


def render_kernel(st: dict) -> str:
    src = KERNEL_TEMPLATE
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


# ============================================================================
# hermes link: flip Hermes between the Kaggle model and its saved fallback
# ============================================================================
def _hermes(*args, timeout=120):
    return subprocess.run(["hermes", "config", *args], capture_output=True, text=True, timeout=timeout)


def _get_model() -> dict | None:
    r = _hermes("get", "model")
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def wire(st: dict, url: str) -> None:
    """Point Hermes's DEFAULT model at the Kaggle proxy. Backs up old config once."""
    if st.get("hermes_backup") is None:
        st["hermes_backup"] = _get_model()
    base = url.rstrip("/") + "/v1"
    for k, v in (("model.provider", "custom"), ("model.default", st["model_name"]),
                 ("model.base_url", base), ("model.api_key", st["api_key"])):
        r = _hermes("set", k, v)
        if r.returncode != 0:
            raise RuntimeError(f"hermes config set {k} failed: {r.stderr.strip()[:300]}")


def fallback(st: dict) -> str:
    """Restore whatever Hermes had before we wired the Kaggle model."""
    backup = st.get("hermes_backup")
    if not backup:
        return "no backup stored — nothing to fall back to"
    for k in ("provider", "default", "base_url", "api_key"):
        if k in backup and backup[k] is not None:
            _hermes("set", f"model.{k}", str(backup[k]))
        else:
            _hermes("delete", f"model.{k}")
    return f"hermes model restored: provider={backup.get('provider')}, default={backup.get('default')}"


def client_config(st: dict, url: str) -> str:
    """Ready-to-paste OpenAI-compatible config for ANY client."""
    base = url.rstrip("/") + "/v1"
    return (
        "OpenAI-compatible client config:\n"
        f"  Base URL: {base}\n"
        f"  API key:  {st['api_key']}\n"
        f"  Model:    {st['model_name']}\n"
        "Example curl:\n"
        f'  curl {base}/chat/completions -H "Authorization: Bearer {st["api_key"]}" '
        f'-H "Content-Type: application/json" '
        f'-d \'{{"model": "{st["model_name"]}", "messages": [{{"role": "user", "content": "hi"}}]}}\''
    )


# ============================================================================
# CLI + interactive menu
# ============================================================================
def cmd_setup(st: dict, args) -> int:
    print("== Setup ==")
    if not kaggle_installed():
        print("[1/5] installing kaggle CLI...")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "kaggle"], check=True)
    else:
        print("[1/5] kaggle CLI already installed")

    token = pathlib.Path.home() / ".kaggle" / "kaggle.json"
    if token.exists():
        print("[2/5] kaggle.json found:", token)
    else:
        print("[2/5] needs a free API token:")
        print("      kaggle.com → avatar → Settings → API → Create New Token")
        print("      (downloads kaggle.json — put it in", token.parent, ")")
        webbrowser.open("https://www.kaggle.com/settings")
        input("press Enter once kaggle.json is saved there... ")
        if not token.exists():
            print("still missing — put kaggle.json in", token.parent, "and re-run setup")
            return 1

    user = input("[3/5] your Kaggle username: ").strip()
    if not user:
        print("username required")
        return 1
    st["kaggle_username"] = user
    if not st["api_key"]:
        st["api_key"] = new_api_key()
    print(f"[3/5] bearer key generated (Hermes gets it automatically): {st['api_key']}")

    ds = input("[4/5] OPTIONAL Kaggle Dataset slug holding your GGUF (e.g. you/free-llm-gguf, "
               "Enter to skip and download from HF each session): ").strip()
    st["dataset_slug"] = ds

    st["setup_done"] = True
    save(st)
    print("[5/5] setup saved. 'Start server' is now unlocked — run `fls start`.")
    return 0


def _start(st: dict) -> int:
    if not is_setup(st):
        print("✗ not set up — run `fls setup` first."); return 1
    sessions = st["sessions"]
    open_s = session_open(st)
    if open_s:
        open_s["stopped_at"] = time.time()  # previous push replaced by this one
    print("[1/3] pushing kernel to Kaggle (GPU T4×2, internet on)...")
    try:
        push(st)
    except RuntimeError as e:
        print(e)
        if input("flip Hermes back to its fallback model? [y/N]: ").lower() == "y":
            print(fallback(st)); save(st)
        return 1
    sessions = st["sessions"]
    sessions.append({"pushed_at": time.time(), "stopped_at": None, "url": None})
    save(st)

    up = wait_for_tunnel(st)
    if not up:
        print("✗ tunnel never came up. Check: `fls logs` (kernel log on kaggle.com), "
              "quota: `fls status`.")
        return 1
    for s in reversed(sessions):
        if s.get("stopped_at") is None:
            s["url"] = up["url"]
            break
    save(st)

    print("[2/3] wiring Hermes...")
    wire(st, up["url"])
    save(st)
    hrs = gpu_hours_last_7d(st)
    print("[3/3] done.")
    print(f"\nLIVE: {up['url']}  ·  model: {up['model']}")
    print(f"GPU used last 7d: {hrs}h of ~{st['weekly_gpu_budget_hours']}h")
    if hrs > st["gpu_warn_hours"]:
        print(f"⚠ QUOTA WARNING: {hrs}h ≥ warning threshold {st['gpu_warn_hours']}h — `fls stop` when done.")
    return 0


def cmd_start(st: dict, args) -> int:
    rc = _start(st)
    if rc != 0:
        return rc
    if not args.watch:
        return 0
    print("watchdog ON — re-pushing automatically if the session dies (Ctrl-C to stop watching).")
    try:
        while True:
            time.sleep(120)
            status = kernel_status(st)
            print(" ", status)
            if status in ("error", "cancelacknowledged", "cancelrequested"):
                print("session died — re-pushing...")
                if _start(st) != 0:
                    return 1
    except KeyboardInterrupt:
        print("(watching stopped — server keeps running; `fls stop` to kill it)")
    return 0


def cmd_stop(st: dict, args) -> int:
    if not is_setup(st):
        print("✗ not set up."); return 1
    msg = stop(st)
    s = session_open(st)
    if s:
        s["stopped_at"] = time.time()
        save(st)
    print(msg)
    return 0


def cmd_status(st: dict, args) -> int:
    if not is_setup(st):
        print("not set up — nothing running."); return 0
    s = session_open(st)
    if s:
        up = dt.datetime.now() - dt.datetime.fromtimestamp(s["pushed_at"])
        print(f"session: pushed {up} ago, url={s.get('url')}")
    else:
        print("no tracked session running.")
    print(f"GPU-hrs last 7d: {gpu_hours_last_7d(st)}h (warn at {st['gpu_warn_hours']}h)")
    try:
        print("kaggle kernel status:", kernel_status(st))
    except Exception as e:
        print("kaggle status check failed:", e)
    return 0


def cmd_logs(st: dict, args) -> int:
    print("live log: kaggle.com → Code → your kernel → 'Active Events' / latest version log.")
    webbrowser.open(f"https://www.kaggle.com/code/{st['kaggle_username']}/{st['kernel_slug']}")
    return 0


def cmd_swap(st: dict, args) -> int:
    repo = input("HF GGUF repo (e.g. unsloth/Qwen3.8-27B-GGUF): ").strip()
    fname = input("GGUF filename inside that repo: ").strip()
    if not repo or not fname:
        print("both required."); return 1
    st["model_repo"], st["model_file"] = repo, fname
    st["model_name"] = fname.rsplit(".", 1)[0].lower().replace(".", "-")
    save(st)
    print("saved. `fls start` will push the new model next session.")
    return 0


def cmd_fallback(st: dict, args) -> int:
    print(fallback(st)); save(st); return 0


def cmd_client_config(st: dict, args) -> int:
    s = session_open(st)
    url = (s or {}).get("url") or input("tunnel URL: ").strip()
    print(client_config(st, url))
    return 0


def cmd_check_updates(st: dict, args) -> int:
    def latest(repo):
        with urllib.request.urlopen(f"https://api.github.com/repos/{repo}/releases/latest", timeout=20) as r:
            return json.load(r)["tag_name"]
    print("pinned versions (this runbook): ollama install path = .tar.zst era (v0.34+), "
          "engine fetched 'latest' at session start by design.")
    try:
        print("latest ollama release:", latest("ollama/ollama"))
    except Exception as e:
        print("update check failed:", e)
    return 0


MENU = [
    ("Setup  (first time — installs Kaggle CLI, saves token, generates key)", "setup", None),
    ("Start server  (push kernel → tunnel → wire Hermes)", "start", "✗ locked until setup — run `fls setup`"),
    ("Status  (uptime, URL, quota)", "status", None),
    ("Stop server  (kills kernel — frees GPU quota)", "stop", None),
    ("Open kernel logs  (Kaggle notebook page)", "logs", None),
    ("Fallback  (flip Hermes back to its previous model)", "fallback", None),
    ("Client config  (paste-ready config for any OpenAI-compatible app)", "client-config", None),
]


def menu(st: dict) -> int:
    print("┌─ free-llm-server " + "─" * 40)
    ready = is_setup(st)
    for i, (label, _, _) in enumerate(MENU, 1):
        locked = (i == 2 and not ready)
        print(f"│ [{i}] {DIM + label + '   ✗ (run setup first)' if locked else label}")
    print("│ [q] quit\n└" + "─" * 56)
    choice = input("> ").strip()
    mapping = {"1": cmd_setup, "2": cmd_start, "3": cmd_status,
               "4": cmd_stop, "5": cmd_logs, "6": cmd_fallback, "7": cmd_client_config}
    if choice == "q":
        return 0
    if choice == "2" and not ready:
        print("✗ locked. run setup first."); return 0
    fn = mapping.get(choice)
    return fn(load(), argparse.Namespace(watch=False)) if fn else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="fls", description="free LLM server on Kaggle GPUs")
    sub = p.add_subparsers(dest="cmd")
    for name in ("menu", "setup", "start", "stop", "status", "logs", "swap",
                 "fallback", "client-config", "check-updates"):
        sp = sub.add_parser(name)
        if name == "start":
            sp.add_argument("--watch", action="store_true",
                            help="stay attached: auto re-push if the session dies")
    args = p.parse_args(sys.argv[1:] if sys.argv[1:] else ["menu"])
    st = load()
    if args.cmd == "menu":
        return menu(st)
    dispatch = {"setup": cmd_setup, "start": cmd_start, "stop": cmd_stop, "status": cmd_status,
                "logs": cmd_logs, "swap": cmd_swap, "fallback": cmd_fallback,
                "client-config": cmd_client_config, "check-updates": cmd_check_updates}
    return dispatch[args.cmd](st, args)


if __name__ == "__main__":
    raise SystemExit(main())
