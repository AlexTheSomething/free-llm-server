# ============================================================
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
    """Kernel -> PC via ntfy (keyless, topic name is the shared secret)."""
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
