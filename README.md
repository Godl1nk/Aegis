# Aegis

> **Branch note:** `dev` is the default branch and contains the latest development changes, but it may be unstable. For the more stable curated branch, use [`main`](https://github.com/pewdiepie-archdaemon/odysseus/tree/main).

```
───────────────────────────────────────────────
 ⊹ ࣪ ˖ ૮( ˶ᵔ ᵕ ᵔ˶ )っ  Aegis vers. 1.0
───────────────────────────────────────────────
```

![Aegis](docs/odysseus.jpg)

A self-hosted AI workspace -- meant to be the self-hosted version of the UI experience you get from ChatGPT and Claude. But with more jank and fun. Running on your own hardware, with your own data -- local-first, privacy-first, and no trojan.

## Features
  - **Chat** -- chat with any local model or API; adding them is super simple.<br>　<sub>vLLM · llama.cpp · Ollama · OpenRouter · OpenAI · GitHub Copilot</sub>
  - **Agent** -- hand it tools and let it run the whole task itself.<br>　<sub>built on [opencode](https://github.com/anomalyco/opencode) · MCP · web · files · shell · skills · memory</sub>
  - **Cookbook** -- Scans your hardware, recommends models, click to download and serve.. easy!<br>　<sub>built on [llmfit](https://github.com/AlexsJones/llmfit) · VRAM-aware · GGUF / FP8 / AWQ · fit scoring · vLLM / llama.cpp serving</sub>
  - **Deep Research** -- multi-step runs that gather, read, and synthesize sources into a nice visual report.<br>　<sub>adapted from [Tongyi DeepResearch](https://github.com/Alibaba-NLP/DeepResearch)</sub>
  - **Compare** -- a fun tool to compare models side by side. Test completely blind, no bias!<br>　<sub>multi-model · blind test · synthesis</sub>
  - **Documents** -- YOU write the text, AI is there to assist, not the opposite.<br>　<sub>multi-tab editor · markdown · HTML · CSV · syntax highlighting · AI edits · suggestions</sub>
  - **Memory / Skills** -- Persistent memory and skills, your agent evolves over time as it better understands you and your tasks!<br>　<sub>ChromaDB · fastembed (ONNX) · vector + keyword retrieval · import/export</sub>
  - **Email** -- IMAP/SMTP inbox with AI triage built in: urgency reminders, auto-tag, auto-summary, auto-reply drafts, auto-spam.<br>　<sub>IMAP · SMTP · per-account routing · CalDAV-aware</sub>
  - **Notes & Tasks** -- Quick notes with reminders, a todo list, and scheduled tasks the agent can act on.<br>　<sub>note pings · checklist · cron-style tasks · ntfy / browser / email channels</sub>
  - **Calendar** -- Local-first calendar with CalDAV sync to Radicale / Nextcloud / Apple / Fastmail.<br>　<sub>CalDAV pull · .ics import/export · per-calendar colors · agent-aware</sub>
  - **Works on mobile** -- looks and runs great on your phone, not just desktop.<br>　<sub>responsive · installable (PWA) · touch gestures</sub>
  - **Extras** -- more to explore, happy if you give it a go!<br>　<sub>image editor · theme editor · file uploads (vision + PDF) · web search · presets · sessions · 2FA</sub>

## Demo
A full, hover-to-play tour lives on the landing page (`docs/index.html`).

<details>
<summary>Screenshots / clips</summary>

### Chat & Agents
![Chat & Agents](docs/chat.gif)
### Deep Research
![Deep Research](docs/research.gif)
### Compare
![Compare](docs/compare.gif)
### Documents
![Documents](docs/document.gif)
### Notes & Tasks
![Notes & Tasks](docs/notes.gif)

</details>

## Quick Start

Defaults work out of the box: clone, run, then configure models/search/email
inside **Settings**. Only edit `.env` for deployment-level overrides like
`APP_BIND`, `APP_PORT`, `AUTH_ENABLED`, `DATABASE_URL`, or a pre-seeded admin password.

On first setup, Aegis creates an admin account (`admin` unless
`ODYSSEUS_ADMIN_USER` is set) and prints a temporary password in the terminal.
For Docker installs, the same line is in `docker compose logs odysseus`.
Use that for the first login, then change it in **Settings**.

Contributing? See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, testing, and
pull request guidelines.

### Docker (recommended)
```bash
git clone https://github.com/Godl1nk/Aegis.git
cd Aegis
cp .env.example .env       # optional, but recommended for explicit defaults
docker compose up -d --build
```
To include optional extras in the image (PDF viewer, Office extraction; includes AGPL PyMuPDF), build with `docker compose build --build-arg INSTALL_OPTIONAL=true` before `up`.

Open `http://localhost:7000` when the containers are healthy. Docker Compose
binds the web UI to `127.0.0.1` by default. If the port is taken, set
`APP_PORT=7001` in `.env` and recreate the container. Set `APP_BIND=0.0.0.0`
only when you intentionally want LAN/reverse-proxy access.

### Native Linux / macOS
```bash
git clone https://github.com/Godl1nk/Aegis.git
cd Aegis
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python setup.py
python -m uvicorn app:app --host 127.0.0.1 --port 7000
```
Requirements: Python 3.11+. Cookbook also needs `tmux` for background model
downloads and serves. The app itself is lightweight; local model serving is the
heavy part and depends on the model, runtime, GPU, and VRAM, so small hosts can
connect to API or remote model servers instead. Use `--host 0.0.0.0` only when you intentionally want LAN/reverse-proxy access.

### Apple Silicon
Docker on macOS cannot use the Metal GPU. For GPU-accelerated Cookbook on an
M-series Mac, run Aegis natively:

```bash
git clone https://github.com/Godl1nk/Aegis.git
cd Aegis
./start-macos.sh
```

It launches at `http://127.0.0.1:7860`. To expose it to your phone over a trusted LAN/VPN such as Tailscale, bind all interfaces:

```bash
ODYSSEUS_HOST=0.0.0.0 ./start-macos.sh
# then open http://<tailscale-ip>:7860
```

The script also reads `.env` at startup, so `APP_BIND=0.0.0.0` and `APP_PORT`
set there are picked up automatically without a command-line override each run.

Keep `AUTH_ENABLED=true` (the default) before binding outside loopback. Do not
expose this port directly to the public internet. To build a clickable app wrapper:

```bash
./build-macos-app.sh
```

<details>
<summary>Cookbook, GPU, Ollama, and troubleshooting notes</summary>

**Docker bundled services.** Compose starts Aegis, ChromaDB, SearXNG, and
ntfy. Aegis and the bundled service ports bind to `127.0.0.1` by default, so
they are reachable from the host but not exposed to your LAN/public internet
unless you opt in.

**Cookbook storage in Docker.** Downloads live in `./data/huggingface`
(`~/.cache/huggingface` in the container). Cookbook-installed Python CLIs and
serve engines live in `./data/local` (`~/.local` in the container), so they
survive container recreation.

**Remote servers.** In **Cookbook -> Settings -> Servers**, generate the
Aegis SSH key and add the public key to the remote server's
`~/.ssh/authorized_keys`. From the host you can also run:

```bash
ssh-copy-id -i data/ssh/id_ed25519.pub user@server
```

**Docker GPU overlays.** CPU-only users can skip this section. Cookbook can
only detect GPUs that Docker exposes to the container — if the host runtime or
device passthrough is not configured, Cookbook sees the iGPU, another card, or
CPU instead of your intended GPU.

For NVIDIA, `scripts/check-docker-gpu.sh` diagnoses GPU passthrough and can
optionally install the host runtime or update `.env`.

```bash
# Read-only diagnostic (default — installs nothing, never edits .env):
scripts/check-docker-gpu.sh

# Print OS-specific install commands without running them:
scripts/check-docker-gpu.sh --print-install-commands

# Install NVIDIA Container Toolkit on Ubuntu/Debian (requires sudo):
scripts/check-docker-gpu.sh --install-nvidia-toolkit

# Write COMPOSE_FILE to .env (only when GPU passthrough is confirmed working):
scripts/check-docker-gpu.sh --enable-nvidia-overlay

# Full assisted setup — install toolkit, then enable overlay if passthrough works:
scripts/check-docker-gpu.sh --install-nvidia-toolkit --enable-nvidia-overlay
```

Safety notes:
- The app never installs host GPU runtime automatically.
- The app never edits `.env` automatically.
- `.env` is only modified when `--enable-nvidia-overlay` is explicitly passed,
  and only after GPU passthrough succeeds. `--yes` skips prompts but does not
  bypass the passthrough gate.
- `.env.bak.*` backups created by `--enable-nvidia-overlay` are ignored by
  Git and the Docker build context.

To enable manually without the script, add this to `.env`:

```bash
COMPOSE_FILE=docker-compose.yml:docker/gpu.nvidia.yml
```

**AMD / ROCm.** AMD setup is read-only diagnostic plus manual `.env` edit. Run:

```bash
scripts/check-docker-amd-gpu.sh
```

Then add the reported values to `.env`, replacing `RENDER_GID` with your host's
numeric render group id:

```bash
COMPOSE_FILE=docker-compose.yml:docker/gpu.amd.yml
RENDER_GID=989
```

For NVIDIA/AMD GPU support, also read the comments in the selected overlay file: docker/gpu.nvidia.yml or docker/gpu.amd.yml.

**Stack-management UIs (Portainer, Coolify, Dockhand, etc.).** These tools
often accept only a single Compose file and do not reliably honor `COMPOSE_FILE`
or multiple `-f` overlays. CLI users should keep using the `COMPOSE_FILE`
overlay workflow above. For stack UIs, point the stack at one of the standalone
files instead, which bundle the base stack plus the GPU settings:

- `docker-compose.gpu-nvidia.yml` — still requires the NVIDIA Container Toolkit
  on the host.
- `docker-compose.gpu-amd.yml` — still requires host ROCm/kfd/DRI setup, the
  `video`/`render` group membership, and `RENDER_GID` when needed.

The base `docker-compose.yml` plus the `docker/gpu.*.yml` overlays remain the
source of truth; the standalone files mirror them for single-file deployments.

Verify after enabling either overlay:

```bash
docker compose exec odysseus nvidia-smi -L   # NVIDIA
docker compose exec odysseus sh -lc 'test -e /dev/kfd && test -d /dev/dri && ls -l /dev/kfd /dev/dri/renderD*'  # AMD
```

> **GPU passthrough ≠ llama.cpp CUDA.** `nvidia-smi` passing inside the
> container confirms Docker GPU access, but llama.cpp also needs `cudart` and
> the CUDA Toolkit at runtime. If Cookbook logs show `Unable to find cudart
> library`, `Could NOT find CUDAToolkit`, `CUDA Toolkit not found`, or
> tensors/layers assigned to CPU, that is a Cookbook/llama.cpp build issue —
> not a Docker passthrough failure. Re-install the serve engine via
> **Cookbook → Dependencies** to get a CUDA-enabled build.
>
> The same split applies to AMD/ROCm: seeing `/dev/kfd` and `/dev/dri` inside
> the container confirms device passthrough, not ROCm userspace or a
> ROCm-enabled vLLM/llama.cpp build. `rocm-smi` and `rocminfo` are not expected
> inside the slim Aegis image.

**Ollama with Docker.** If Ollama runs on the host, add this endpoint in
Settings:

```text
http://host.docker.internal:11434/v1
```

Ollama must listen outside its own loopback interface:

```bash
OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

This connects Odysseus in Docker to an Ollama server that is already running on
your host machine; it does not start Ollama inside the container.
`host.docker.internal` is Docker's hostname for the host machine from inside the
container. Cookbook **Serve** is a separate workflow for serving downloaded
models through Odysseus/llama.cpp, so Windows users with an existing Ollama
install usually only need to add the endpoint in Settings.

**Useful checks.**

```bash
docker compose ps
docker compose logs --tail=120 odysseus
docker compose logs odysseus | grep -E 'ChromaDB|MemoryVectorStore|DEGRADED'
```

**macOS details.** `start-macos.sh` installs Homebrew deps, creates the venv,
runs setup, and starts uvicorn on port `7860` because AirPlay often holds
`7000`. It uses llama.cpp/Ollama for Metal. vLLM/SGLang are CUDA/ROCm-only and
do not run on macOS. MLX-only models are not served by Aegis.

</details>

### Native Windows

**One-command launcher** (creates the venv, installs deps, runs setup, starts the
server; safe to re-run):

```powershell
git clone https://github.com/Godl1nk/Aegis.git
cd Aegis
powershell -ExecutionPolicy Bypass -File .\launch-windows.ps1
```

Or do it by hand:

```powershell
git clone https://github.com/Godl1nk/Aegis.git
cd Aegis
py -3.11 -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
python setup.py
python -m uvicorn app:app --host 127.0.0.1 --port 7000
```

If `python` points at an older interpreter, use `py -3.12` (or another installed
3.11+ version) for the venv step.

**Requirements:** Python 3.11+. The core app (chat, agent, memory, documents,
email, calendar, deep research) runs fully native. For full **Cookbook** background
model downloads and the agent shell tool, also install
[Git for Windows](https://git-scm.com/download/win) (provides `bash.exe`).
Local GPU *serving* of vLLM/SGLang needs Linux/WSL2; for a local model on Windows,
[Ollama](https://ollama.com/download) is the easiest path — point Aegis at
`http://localhost:11434/v1` in Settings.

Open `http://localhost:7000`, log in with the generated admin password,
and configure everything else inside **Settings**.

## Troubleshooting & Advanced Setup

### `chromadb-client` conflicts with embedded ChromaDB
If `chromadb-client` (the lightweight HTTP-only package) is installed alongside the full `chromadb` package, Odysseus starts but ChromaDB silently falls back to HTTP-only mode and fails.

**Fix:** uninstall `chromadb-client` and force-reinstall the full package:
```bash
./venv/bin/pip uninstall chromadb-client -y
./venv/bin/pip install --force-reinstall chromadb
```

### HTTPS + LAN/Tailscale exposure
To expose Odysseus on a local network or Tailscale with HTTPS:
1. Change the bind address to `0.0.0.0` in `.env` (`APP_BIND=0.0.0.0` or `ODYSSEUS_HOST=0.0.0.0`).
2. Generate a locally-trusted cert for your LAN/Tailscale IPs using [mkcert](https://github.com/FiloSottile/mkcert):
   ```bash
   mkcert -install
   mkcert -cert-file cert.pem -key-file key.pem 192.168.1.100 tailscale-ip
   ```
3. Run `uvicorn` with the generated certs:
   ```bash
   python -m uvicorn app:app --host 0.0.0.0 --port 7000 --ssl-certfile=cert.pem --ssl-keyfile=key.pem
   ```
4. Install the `mkcert` CA on any other device you want to access Odysseus from (e.g., for iOS, email the `rootCA.pem` to yourself, install the profile, and trust it in Certificate Trust Settings).

### Optional Dependencies
`requirements-optional.txt` contains packages that unlock extra features. It is not installed by default.

| Package | Feature unlocked |
|---------|-----------------|
| `faster-whisper` | Local speech-to-text (microphone -> text) via the "local" STT provider. |
| `duckduckgo-search` | DuckDuckGo as a search provider option. |
| `PyMuPDF` | PDF page rendering in the side viewer panel and form-filling. (Note: AGPL-3.0) |
| `markitdown` | Office/EPUB document text extraction (converts .docx/.xlsx/.pptx/.xls/.epub to Markdown). |

### Outlook / Office 365 email
Odysseus email accounts currently use IMAP/SMTP username-password auth. Outlook
and Microsoft 365 generally require OAuth instead, so normal Microsoft mailbox
passwords will fail. See [docs/email-outlook.md](docs/email-outlook.md) for the
current limitation and the planned integration direction.

## Security Notes
Aegis is a self-hosted workspace with powerful local tools: shell access, file uploads, model downloads, web research, email/calendar integrations, and API tokens. Treat it like an admin console.

- Keep `AUTH_ENABLED=true` for any network-accessible deployment.
- Keep `LOCALHOST_BYPASS=false` outside local development.
- Use `SECURE_COOKIES=true` when Aegis is served through HTTPS by a trusted reverse proxy or private access gateway.
- Do not expose it directly to the public internet without HTTPS and a trusted reverse proxy or private access layer.
- Keep `.env`, `data/`, `logs/`, databases, uploads, generated media, backups, auth/session files, API keys, and model/provider tokens out of Git and private shares. They are ignored by default.
- Review `data/auth.json` after first boot: disable open signup unless you intentionally want it, make only your own account admin, and keep demo/test accounts non-admin.
- Non-admin users do not get shell/Python/file read/write by default, and admin-only routes/tools such as MCP management, API tokens, webhooks, model/cookbook serving, backup/vault, and app settings are admin-gated. Other features are controlled by per-user privileges, so review each user's privileges before exposing a deployment.
- Rotate any API keys or tokens that were ever pasted into a shared chat, demo, screenshot, or log.
- If you enable API tokens or webhooks, create separate tokens per integration and delete unused ones.
- Prefer binding manual development runs to `127.0.0.1`; bind to `0.0.0.0` only when you intentionally want LAN/reverse-proxy access.
- Keep ChromaDB, SearXNG, ntfy, Ollama, vLLM, llama.cpp, databases, and raw model/provider APIs internal-only. Expose only the authenticated Aegis web/API entrypoint through your trusted proxy or private access layer.
- Before publishing a fork, run `git status --short` and confirm no private files from `.env`, `data/`, `logs/`, uploads, backups, or local databases are staged.

### Private or proxied deployments
Aegis serves plain HTTP on its app port. Docker Compose binds Aegis and the bundled services to `127.0.0.1` by default, so a typical production/private setup is:

1. Keep Aegis on localhost, for example `127.0.0.1:7000`.
2. Terminate HTTPS at a trusted reverse proxy or private access gateway.
3. Put the authenticated Aegis web/API entrypoint behind that layer.
4. Keep raw service and model ports internal-only.

Cloudflare Access, Tailscale, Caddy, nginx, and Traefik can all fit this pattern; none are required by Aegis. If your access layer reaches Aegis on the same host, proxy to `http://127.0.0.1:7000` and keep `AUTH_ENABLED=true`, `LOCALHOST_BYPASS=false`, and `SECURE_COOKIES=true`.

Common internal-only ports from the default docs/compose setup:

| Port | Service |
|---|---|
| `7000` | Aegis raw app port |
| `8080` | SearXNG |
| `8091` | ntfy |
| `8100` | ChromaDB host port for manual/compose access |
| `11434` | Ollama |
| `8000-8020` | Common local model/provider APIs |

## Contributing
Help is welcome. The best entry points are fresh-install testing, provider setup
bugs, mobile/editor polish, docs, and small focused refactors. See
[ROADMAP.md](ROADMAP.md) for the current help-wanted list.

## Configuration
Most setup is done inside the app with `/setup` or **Settings**. Use `.env`
for deployment-level defaults and secrets you want present before first boot.
Key settings:

| Variable | Default | Description |
|---|---|---|
| `LLM_HOST` | `localhost` | Your LLM server (e.g. `llm-host.local:8000`) |
| `LLM_HOSTS` | -- | Comma-separated list for model discovery |
| `OPENAI_API_KEY` | -- | Optional OpenAI key. Prefer adding providers in the app unless pre-seeding. |
| `SEARXNG_INSTANCE` | `http://localhost:8080` | SearXNG URL. Docker overrides this to `http://searxng:8080`. |
| `SEARXNG_SECRET` | generated on first Docker boot | Optional SearXNG cookie/CSRF secret. Leave blank unless you need to pin it. |
| `APP_BIND` | `127.0.0.1` | Docker Compose host bind address for the web UI. Use `0.0.0.0` only for intentional LAN/reverse-proxy access. |
| `APP_PORT` | `7000` | Docker Compose host port for the web UI. |
| `AUTH_ENABLED` | `true` | Enable/disable login |
| `LOCALHOST_BYPASS` | `false` | Development-only auth bypass for loopback requests. Keep false for shared/network deployments. |
| `SECURE_COOKIES` | `false` | Set true when serving Aegis through HTTPS at a trusted proxy or private access gateway. |
| `DATABASE_URL` | `sqlite:///./data/app.db` | Database connection string |
| `CHROMADB_HOST` | `localhost` | ChromaDB host for vector memory. Docker overrides this to `chromadb`. |
| `CHROMADB_PORT` | `8100` | ChromaDB port for manual host runs. Docker overrides this to `8000`. |
| `EMBEDDING_URL` | -- | OpenAI-compatible embeddings endpoint |
| `ODYSSEUS_CHAT_UPLOAD_MAX_BYTES` | `10485760` | Chat/agent attachment cap in bytes. Raise for larger local PDFs or text documents. |
| `ODYSSEUS_GALLERY_UPLOAD_MAX_BYTES` | `104857600` | Gallery image upload cap in bytes (100 MB). |
| `ODYSSEUS_GALLERY_TRANSFORM_UPLOAD_MAX_BYTES` | `26214400` | Gallery transform input cap in bytes (25 MB). |
| `ODYSSEUS_MEMORY_IMPORT_MAX_BYTES` | `10485760` | Memory import file cap in bytes (10 MB). |
| `ODYSSEUS_PERSONAL_UPLOAD_MAX_BYTES` | `26214400` | Personal document upload cap in bytes (25 MB). |
| `ODYSSEUS_EMAIL_COMPOSE_UPLOAD_MAX_BYTES` | `26214400` | Email compose attachment cap in bytes (25 MB). |
| `ODYSSEUS_STT_MAX_AUDIO_BYTES` | `26214400` | Speech-to-text audio cap in bytes (25 MB). |
| `ODYSSEUS_ICS_MAX_BYTES` | `10485760` | Calendar `.ics` import cap in bytes (10 MB). |

All upload-limit vars are validated (must be a positive integer) and optional; an invalid value fails fast at startup.

### Built-in MCP servers (optional setup)

Aegis auto-registers a few built-in MCP servers at startup. The npx-based ones (currently the browser server, `@playwright/mcp`) only start when their npm package is already in the local npx cache. If a package isn't cached, that server is skipped with a startup log message explaining what to do, so a fresh install does not block on a multi-minute npm download or hang if Playwright system deps are missing.

To enable the browser MCP (page navigation, screenshots, vision), run once:

```bash
npx -y @playwright/mcp@latest --version
```

That installs `@playwright/mcp` plus Playwright (~300MB total). Restart Aegis and the server will register at startup.

## Architecture
```
app.py                   # FastAPI entry point
core/      auth, database, middleware, constants
src/       llm_core, agent_loop, agent_tools, chat_processor, search/
routes/    chat, session, document, memory, model … endpoints
services/  docs, memory, search, hwfit (Cookbook) …
static/    index.html + app.js + style.css + js/ (modular front-end)
docs/      landing page (index.html) + preview clips
```

### Memory Architecture

Aegis memory is split into explicit user-managed memory, background synthesized
memory, source-grounded observations, reviewable summaries, and vector search.
The public `/api/memory` routes keep the older `memory.json`-shaped API so the
UI, slash commands, Codex integration, and older tools do not need to know which
backend is active.

The runtime source of truth is **Memory V2** when the app is using the normal
`data/` directory. `src/memory.py` is the compatibility facade. It exposes the
legacy `MemoryManager` API, but stores records through `src/memory_v2.py` in the
SQLite application database. Temporary test directories still use plain JSON so
tests and one-off scripts do not accidentally mutate the real app database.

Core tables live in `core/database.py`:

| Table | Purpose |
|---|---|
| `memory_items` | Saved and synthesized memories shown/retrieved by the app. |
| `memory_observations` | Source-grounded facts extracted from chats or other inputs. |
| `memory_summary_versions` | Versioned review summaries of what Aegis knows. |
| `memory_feedback` | Relevant/not relevant/corrected/deleted/prioritized/deprioritized signals. |
| `memory_jobs` | Background Dreaming job state, progress, result, and errors. |

`data/memory.json` is still written as a compatibility mirror. On first Memory
V2 use, existing JSON memories are imported once into `memory_items` with their
original IDs and metadata where possible (`owner`, `category`, `source`,
`session_id`, `pinned`, `uses`). If the migration marker exists but the database
has no memory rows, the importer can re-run so a partially migrated install is
not stranded.

#### Memory Types

- **Saved memories** are explicit or auto-extracted long-term facts. They use
  `kind="saved"` and are managed through the existing add/edit/delete/pin/tidy
  flows.
- **Synthesized memories** are background Dreaming results. They use
  `kind="synthesized"` and `source="dream"`. Legacy `save()` calls do not archive
  synthesized rows; Dreaming owns their lifecycle.
- **Observations** are evidence rows, not directly injected as top-level memory
  unless Dreaming promotes them into synthesized items.
- **Summary versions** are review snapshots. Restoring an older summary marks
  newer active synthesized memories as archived instead of deleting evidence.

#### Dreaming Process

Dreaming is implemented in `services/memory/dreamer.py`. It is intentionally
conservative and local-first:

1. A completed non-incognito chat schedules `dream_from_session()` from
   `routes/chat_helpers.py` when memory, saved-memory reference, chat-history
   reference, Dreaming, and chat-source toggles are enabled.
2. The dreamer creates a `memory_jobs` row so status is visible in the Brain
   summary UI and repeated runs can be deduplicated.
3. Recent session messages are normalized into text. Obvious durable facts are
   extracted with the lightweight fallback extractor. Sensitive categories are
   ignored unless the user explicitly asked Aegis to remember/save them.
4. Each candidate becomes a `memory_observations` row keyed by owner, source
   type, source ID, and a stable fingerprint.
5. New observations are promoted into synthesized `memory_items` with
   `source_refs` pointing back to the chat and observation.
6. Synthesized memories are embedded in ChromaDB with owner metadata.
7. If new items were added, `memory_summary_versions` gets a new active
   review summary.

The current implementation is heuristic-first. The schema and jobs model are
designed for richer LLM synthesis later: clustering duplicates, reconciling
contradictions, scoring confidence, and rebuilding summaries from all sources.

#### Retrieval Process

Memory retrieval is planned in `routes/chat_helpers.py` and executed by
`src/chat_processor.py`:

1. Per-request gates are evaluated: incognito/Nobody mode, `no_memory`,
   `memory_enabled`, `reference_saved_memories`, and `reference_chat_history`.
2. If saved memories are disabled, no memory is injected.
3. If chat history reference is disabled, synthesized Dreaming memories are
   filtered out and only saved memories remain.
4. Pinned memories are always injected when memory is enabled.
5. Other memories are retrieved with hybrid ranking:
   - ChromaDB vector search for semantic recall.
   - Keyword/BM25-style scoring for exact and category-aware matches.
   - A small recency tiebreaker.
6. Injected memory IDs are usage-counted and saved in assistant message metadata
   as `memories_used`, which powers `/api/memory/sources/{response_id}`.

The memory vector store is a shared Chroma collection, but every vector carries
`owner` and `kind` metadata. Search and near-duplicate checks accept an owner
filter, and the caller also intersects vector hits with owner-scoped database
rows before anything reaches the model.

#### Controls And UI

Memory settings are exposed in the Brain modal:

- **Memory enabled**: top-level read/write gate for chat memory behavior.
- **Reference saved memories**: controls explicit saved-memory recall.
- **Reference chat history**: controls Dreaming/synthesized memory recall.
- **Dreaming synthesis**: controls background synthesis.
- **Dream sources / Chats**: controls whether chat sessions feed Dreaming.

The Summary tab calls:

- `GET /api/memory/summary`
- `GET /api/memory/summary/history`
- `POST /api/memory/summary/restore`
- `GET /api/memory/dream/status`
- `POST /api/memory/dream/run`

Memory source feedback uses:

- `GET /api/memory/sources/{response_id}`
- `POST /api/memory/sources/feedback`

All memory mutation routes require the `can_manage_memory` privilege. Read-only
memory routes remain owner-scoped. Codex/API-token memory writes go through
`routes/codex_routes.py`, which scope-checks the token and temporarily runs the
normal memory route as the token owner.

#### Source Lifecycle And Deletion

Deleting a saved memory marks its `memory_items` row deleted and removes its
vector immediately. Deleting a chat session invalidates related observations
through `MemoryV2Store.invalidate_source()`, archives those observations, and
marks synthesized memories that cite them as stale. Feedback with `deleted`
also marks the target memory deleted and removes the vector.

Memory tidy/audit runs through `services/memory/memory_extractor.py`. It is
owner-scoped, preserves other users' memories, refuses unsafe over-deletion, and
returns the exact number of removed rows so the UI can report the real result.

#### Privacy And Incognito

Nobody/incognito mode disables memory read and write for the chat turn. It also
disables RAG injection, background auto-extraction, Dreaming, and skill
extraction. Temporary memory suppression can also be requested per call through
`no_memory`.

Source-derived memory is always owner-scoped. Legacy ownerless rows can be
claimed for a user during migration/auth flows; otherwise multi-user routes
verify ownership before edit/delete/pin/source feedback.

#### Operational Notes

- SQLite stores the authoritative Memory V2 rows in `data/app.db`.
- `data/memory.json` remains a compatibility mirror and import seed.
- ChromaDB stores memory embeddings in the `odysseus_memories` collection.
- If ChromaDB or embeddings are unavailable, keyword retrieval and SQLite/JSON
  storage continue to work; vector add/search failures are logged and degraded.
- Wiping memory clears legacy JSON, Memory V2 tables, migration state, and the
  memory vector collection.
- Python tests for this area are in `tests/test_memory_v2_retrieval.py` and
  related `tests/test_memory_*` files.

## Data
All user data lives in `data/` (gitignored): `app.db` (sessions, messages, documents),
`memory.json`, `presets.json`, `uploads/`, `personal_docs/`, `chroma/`, `settings.json`.

## Star History

<a href="https://www.star-history.com/?repos=Godl1nk%2FAegis&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=Godl1nk/Aegis&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=Godl1nk/Aegis&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=Godl1nk/Aegis&type=date&legend=top-left" />
 </picture>
</a>

## License
MIT -- see [LICENSE](LICENSE) and [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md).

```
                                  |
                                 |||
                                |||||
                  |    |    |   |||||||
                 )_)  )_)  )_)   ~|~
                )___))___))___)\  |
               )____)____)_____)\\|
             _____|____|____|_____\\\__
             \                       /
       ~^~^~~^~^~~^~^~~^~^~~^~^~~^~^~~^~^~~^~^~
               ~^~  all aboard!  ~^~
       ~^~^~~^~^~~^~^~~^~^~~^~^~~^~^~~^~^~~^~^~
```
