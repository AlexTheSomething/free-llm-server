"""free-llm-server: run a free public-ish LLM server on Kaggle GPUs, wire it anywhere."""

import json
import pathlib
import secrets
import time

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
