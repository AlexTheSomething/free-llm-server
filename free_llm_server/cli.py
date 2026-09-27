"""CLI + grayed-out interactive menu for free-llm-server."""

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
import webbrowser

from . import hermes_link
from . import kaggle_ops
from . import state
DIM = "\033[2m"; BOLD = "\033[1m"; END = "\033[0m"


# ---------------- commands ----------------

def cmd_setup(st: dict, args) -> int:
    print("== Setup ==")
    if not kaggle_ops.kaggle_installed():
        print("[1/5] installing kaggle CLI...")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "kaggle"], check=True)
    else:
        print("[1/5] kaggle CLI already installed")

    import pathlib
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
        st["api_key"] = state.new_api_key()
    print(f"[3/5] bearer key generated (Hermes gets it automatically): {st['api_key']}")

    ds = input("[4/5] OPTIONAL Kaggle Dataset slug holding your GGUF (e.g. you/free-llm-gguf, "
               "Enter to skip and download from HF each session): ").strip()
    st["dataset_slug"] = ds

    st["setup_done"] = True
    state.save(st)
    print("[5/5] setup saved. 'Start server' is now unlocked — run `fls start`.")
    return 0


def _start(st: dict) -> int:
    if not state.is_setup(st):
        print("✗ not set up — run `fls setup` first."); return 1
    sessions = st["sessions"]
    open_s = state.session_open(st)
    if open_s:
        open_s["stopped_at"] = time.time()  # previous push replaced by this one
    print("[1/3] pushing kernel to Kaggle (GPU T4×2, internet on)...")
    try:
        kaggle_ops.push(st)
    except RuntimeError as e:
        print(e)
        if input("flip Hermes back to its fallback model? [y/N]: ").lower() == "y":
            print(hermes_link.fallback(st)); state.save(st)
        return 1
    sessions = st["sessions"]
    sessions.append({"pushed_at": time.time(), "stopped_at": None, "url": None})
    state.save(st)

    up = kaggle_ops.wait_for_tunnel(st)
    if not up:
        print("✗ tunnel never came up. Check: `fls logs` (kernel log on kaggle.com), "
              "quota: `fls status`.")
        return 1
    for s in reversed(sessions):
        if s.get("stopped_at") is None:
            s["url"] = up["url"]
            break
    state.save(st)

    print("[2/3] wiring Hermes...")
    hermes_link.wire(st, up["url"])
    state.save(st)
    hrs = state.gpu_hours_last_7d(st)
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
            status = kaggle_ops.kernel_status(st)
            print(" ", status)
            if status in ("error", "cancelacknowledged", "cancelrequested"):
                print("session died — re-pushing...")
                if _start(st) != 0:
                    return 1
    except KeyboardInterrupt:
        print("(watching stopped — server keeps running; `fls stop` to kill it)")
    return 0


def cmd_stop(st: dict, args) -> int:
    if not state.is_setup(st):
        print("✗ not set up."); return 1
    msg = kaggle_ops.stop(st)
    s = state.session_open(st)
    if s:
        s["stopped_at"] = time.time()
        state.save(st)
    print(msg)
    return 0


def cmd_status(st: dict, args) -> int:
    if not state.is_setup(st):
        print("not set up — nothing running."); return 0
    s = state.session_open(st)
    if s:
        up = dt.datetime.now() - dt.datetime.fromtimestamp(s["pushed_at"])
        print(f"session: pushed {up} ago, url={s.get('url')}")
    else:
        print("no tracked session running.")
    print(f"GPU-hrs last 7d: {state.gpu_hours_last_7d(st)}h (warn at {st['gpu_warn_hours']}h)")
    try:
        print("kaggle kernel status:", kaggle_ops.kernel_status(st))
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
    state.save(st)
    print("saved. `fls start` will push the new model next session.")
    return 0


def cmd_fallback(st: dict, args) -> int:
    print(hermes_link.fallback(st)); state.save(st); return 0


def cmd_client_config(st: dict, args) -> int:
    s = state.session_open(st)
    url = (s or {}).get("url") or input("tunnel URL: ").strip()
    print(hermes_link.client_config(st, url))
    return 0


def cmd_check_updates(st: dict, args) -> int:
    import urllib.request, json as _json
    def latest(repo):
        with urllib.request.urlopen(f"https:{''}//api.github.com/repos/{repo}/releases/latest", timeout=20) as r:
            return _json.load(r)["tag_name"]
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
    ready = state.is_setup(st)
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
    return fn(state.load(), None) if fn else 1


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
    st = state.load()
    if args.cmd == "menu":
        return menu(st)
    dispatch = {"setup": cmd_setup, "start": cmd_start, "stop": cmd_stop, "status": cmd_status,
                "logs": cmd_logs, "swap": cmd_swap, "fallback": cmd_fallback,
                "client-config": cmd_client_config, "check-updates": cmd_check_updates}
    return dispatch[args.cmd](st, args)


if __name__ == "__main__":
    raise SystemExit(main())
