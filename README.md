# free-llm-server

**One file** (`fls.py`) that runs **your own OpenAI-compatible LLM server on Kaggle's free GPUs** and wire it to Hermes, VS Code, or any OpenAI-speaking client. No API keys to pay for, no VPS, no caps except Kaggle's ~30 GPU-hours/week.

```
┌─ fls (free-llm-server) ──────────────────────┐
│ [1] Setup      (first time — ~10 min)        │
│ [2] Start      (✗ locked until setup)        │
│ [3] Status / [4] Stop / [5] Logs             │
│ [6] Fallback / [7] Client config             │
└──────────────────────────────────────────────┘
```

## Install

```bash
curl -O https://raw.githubusercontent.com/AlexTheSomething/free-llm-server/main/fls.py
python fls.py          # interactive menu, or use subcommands
```

(No dependencies beyond Python 3.10+; the Kaggle CLI is installed by `setup` for you.)

## How it works

```
python fls.py start
  → renders the embedded Kaggle kernel with your settings
  → pushes it as a PRIVATE Kaggle kernel (GPU T4×2, internet on)
  → kernel: installs Ollama, imports your GGUF, starts an auth proxy
    on :8080 + a Cloudflare quick tunnel, publishes the URL to ntfy
  → fls picks the URL up, wires Hermes (or prints client config),
    tracks quota
python fls.py stop
  → POSTs /admin/shutdown through the live tunnel (or tells you where to click)
```

- **Kaggle has no inbound internet**, so the kernel reports its tunnel URL to a
  keyless [ntfy](https://ntfy.sh) topic whose name only you know (derived per-setup).
- The API is **bearer-token protected** (401 without the key — verified).
- Quota guard tracks your pushed/stopped sessions: warns at 25 GPU-hrs of the ~30 h weekly budget.

## Commands

| command | what it does |
|---|---|
| `fls setup` | install Kaggle CLI, save `kaggle.json`, generate the API key |
| `fls start` | push → wait for tunnel → wire Hermes → print LIVE url |
| `fls start --watch` | stay attached: auto re-push if the session dies (12 h cap / crash) |
| `fls stop` | kill the kernel, free GPU quota |
| `fls status` | uptime, URL, GPU-hours used this week |
| `fls swap` | point at a different HF GGUF repo/file for the next session |
| `fls fallback` | flip Hermes back to its previous model |
| `fls client-config` | paste-ready config for any OpenAI-compatible app |

(`fls` = `python fls.py`; alias it if you like.)

## Known limits (honest)

- First boot after a push takes ~10-12 min (engine + 17 GB model). A Kaggle
  Dataset holding the GGUF cuts that (~`fls setup` step 4 asks for the slug).
- Generation on T4×2 at 4-bit is ~5-8 tok/s. Real but not fast.
- `fls stop` falls back to manual (notebook UI → Stop) if the tunnel is already dead.
- Tested against Kaggle's 22.04-era image (Ollama ≥0.34 `.tar.zst` path).

## License

MIT
