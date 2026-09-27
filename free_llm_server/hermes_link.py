"""Flip Hermes between the Kaggle model and the saved fallback (OpenRouter etc.)."""

import json
import subprocess

from . import state


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
        f'-H "Content-Type: application/json" -d \'{{"model":"{st["model_name"]}",'
        '"messages":[{"role":"user","content":"hi"}]}\''
    )
