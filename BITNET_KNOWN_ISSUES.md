# BitNet Known Issues & Setup Guide

> **Why this doc exists:** Setting up BitNet on Apple Silicon is non-trivial. This doc documents what was tried, what failed, and what actually works — so future developers don't waste days on dead ends.

---

## CPU Architecture

| Architecture | Status | Notes |
|---|---|---|
| **Apple Silicon (arm64)** | ✅ Fully supported | M1/M2/M3 all work. Compile with `BITNET_ARM_TL1=OFF` |
| **x86_64 Linux** | ⚠️ Unverified | TL1 kernel理论上支持 but not tested in this project |
| **Intel Mac (x86_64)** | ⚠️ Unknown | Not tested |

### How CPU Detection Works

The `scripts/setup-bitnet.sh` script detects your CPU with `uname -m`:

```bash
ARCH=$(uname -m)
case "$ARCH" in
    arm64)   echo "Apple Silicon — BitNet supported" ;;
    x86_64) echo "x86_64 — proceed at your own risk" ;;
    *)      echo "Unsupported: $ARCH" ;;
esac
```

---

## Model: What to Download

### ❌ DO NOT use these models

| Model | Why |
|---|---|
| `tiiuae/Falcon3-1B-Instruct-1.58bit` | Pretokenizer issue causes gibberish output. The 1.58bit quantization **requires** the BitNet-specific pretokenizer from `microsoft/BitNet-b1.58-2B-4T-gguf`. |
| Manual HF→GGUF conversion | The official conversion scripts produce models with the broken pretokenizer. |
| `BITNET_ARM_TL1=ON` build | Requires >8GB RAM to compile the 600+ line template-metaprogrammed TL1 kernel file. On 8GB machines this OOMs. |

### ✅ Use this model

```
microsoft/BitNet-b1.58-2B-4T-gguf
```

- Pre-converted GGUF — the BitNet team did the conversion with their custom pretokenizer
- ~1.2GB on disk
- Model file: `ggml-model-i2_s.gguf`
- Download via: `huggingface-cli download microsoft/BitNet-b1.58-2B-4T-gguf --local-dir /path/to/models`

---

## Build: llama-server

### ✅ Verified build command (Apple Silicon, 8GB RAM)

```bash
cd BitNet
mkdir -p build
cmake -B build \
    -DBITNET_ARM_TL1=OFF \
    -DCMAKE_C_COMPILER=clang \
    -DCMAKE_CXX_COMPILER=clang++ \
    -DCMAKE_BUILD_TYPE=Release
cmake --build build --target llama-server --config Release -j 4
```

Key flags:
- `BITNET_ARM_TL1=OFF` — Uses I2_S kernels which are stable and compile fast. TL1 kernels are faster at inference but OOM on 8GB machines.
- `-j 4` — 4 parallel compile jobs. More can OOM.
- clang — Required. gcc may produce incorrect code.

### Build time
- ~5-15 minutes on Apple Silicon M1
- Binary at: `BitNet/build/bin/llama-server`

### `llama-server` vs `llama-cli`

Use `llama-server`, not `llama-cli`. The server:
- Exposes an **OpenAI-compatible HTTP API** (`/v1/chat/completions`)
- Works with the same `httpx` client pattern as every other LLM provider in this project
- Can be started/stopped independently of Mneme

The old approach used `llama-cli` via PTY subprocess — this was fragile and didn't work in Docker.

---

## What Was Tried (and Failed)

### Falcon3-1B-Instruct-1.58bit from HuggingFace

**Problem:** The pretokenizer in this model is broken. Outputs gibberish like `"I don't know" expressed as "~ KNOW ~"`.

**Root cause:** The 1.58bit quantization for Falcon3 requires BitNet's custom pretokenizer (not the standard HuggingFace one). When you pull from `tiiuae/Falcon3-1B-Instruct-1.58bit`, you get the model weights but the wrong pretokenizer baked in.

**Attempted fixes:**
- Trying different chat templates (ChatML, Llama 3, Vicuna) — no improvement
- Manual HF→GGUF conversion — produces same broken pretokenizer
- Different quantization levels (f16, f32) — f32 works but is 3.8GB

### BITNET_ARM_TL1=ON build

**Problem:** The TL1 kernel file (`llama-kv-b.update-constants.impl.inc`) is 600+ lines of template metaprogramming. On 8GB RAM machines it OOMs during compilation.

**Fix:** Use `BITNET_ARM_TL1=OFF` — the I2_S kernels work fine for this 2B model and compile in seconds.

### llama-cli subprocess approach

**Problem:** Running `llama-cli` as a PTY subprocess was fragile:
- Signal handling issues
- Didn't work reliably in Docker containers
- Each inference spawned a new process (slow)
- Output parsing was brittle

**Fix:** Use `llama-server` as a persistent HTTP server with OpenAI-compatible API.

---

## Quick Start

```bash
# 1. Full setup + start + test
./scripts/setup-bitnet.sh

# 2. Just download model (no build/start)
./scripts/setup-bitnet.sh --model-only

# 3. Start the server (if already built)
./scripts/start-llm-server.sh --background

# 4. Test Mneme → BitNet integration
curl -X POST http://localhost:8080/ingest \
  -H "Content-Type: application/json" \
  -d '{"file_paths":["*.py"]}'
```

---

## Mneme Integration

### Environment variables

```bash
BITNET_HOST=localhost       # or host.docker.internal (Docker)
BITNET_PORT=8081           # llama-server listen port
BITNET_MODEL=bitnet-b1.58-2b-4t  # model alias (matches -a flag)
BITNET_TIMEOUT=60           # seconds
BITNET_DISABLED=            # set to "1" to force keyword fallback
```

### Docker Compose

The `llm` service in `docker-compose.yml` is commented out by default. To enable:

```yaml
services:
  llm:
    image: ubuntu:22.04
    volumes:
      - ./BitNet:/BitNet
    ports:
      - "8081:8081"
    command: /BitNet/build/bin/llama-server [args...]
```

Alternatively, run `llama-server` on the host and Mneme in Docker with `host.docker.internal:8081`.

### Intent Detection

Mneme uses BitNet for intent detection in the pre-tool hook:

```
User prompt → BitNet intent detection → retrieval query → Qdrant search → response
```

The `detect_intent()` function in `src/retrieval/bitnet_client.py` returns:
- `intent`: `continue_previous_work` | `retry_previous_attempt` | `fix_previous_failure` | `general`
- `detected_tags`: list of `category=value` tags

If the BitNet server is unreachable, it falls back to keyword regex heuristics and marks the result `degraded=True`.

---

## File Layout

```
Mneme/
├── BitNet/                              # BitNet source + build
│   ├── build/bin/llama-server          # Compiled binary
│   └── models/BitNet-b1.58-2B-4T-gguf/
│       └── ggml-model-i2_s.gguf        # The working model (~1.2GB)
├── scripts/
│   ├── setup-bitnet.sh                 # Clone + download + build + test
│   └── start-llm-server.sh             # Start the server
└── src/retrieval/bitnet_client.py     # Python HTTP client
```

---

## Troubleshooting

### "Server didn't become healthy in 60s"

Check the log:
```bash
tail -f /tmp/bitnet-server.log
# or
cat logs/llama-server.log
```

Common causes:
- Model file is corrupt or missing — re-download
- Wrong model file path — verify with `ls -la BitNet/models/*/ggml-model*.gguf`
- Port already in use — kill existing process: `lsof -ti :8081 | xargs kill -9`

### "Health check failed" in Python client

```python
from src.retrieval.bitnet_client import BitNetClient
client = BitNetClient()
print(client.health_check())  # True = server reachable
```

If False:
- Is `llama-server` running? `ps aux | grep llama-server`
- Is the port correct? Match `BITNET_PORT` env var with the `-p` flag in `start-llm-server.sh`
- Firewall blocking localhost? Try `curl http://localhost:8081/health`

### Gibberish output from the model

You downloaded the wrong model. Use `microsoft/BitNet-b1.58-2B-4T-gguf`:
```bash
huggingface-cli download microsoft/BitNet-b1.58-2B-4T-gguf --local-dir ./BitNet/models/BitNet-b1.58-2B-4T-gguf
```

### Build OOM on 8GB machine

Use `BITNET_ARM_TL1=OFF` (already set in `setup-bitnet.sh`). If still OOM, reduce jobs: `-j 2` instead of `-j 4`.
