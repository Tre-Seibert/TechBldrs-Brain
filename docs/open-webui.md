# Open WebUI lab notes

Lab UI is [Open WebUI](https://github.com/open-webui/open-webui) in front of tb-brain. tb-brain calls local Ollama. Do not build a custom chat app. Production UI later is inside Flow.

`docker-compose.yml` in this repo starts **Open WebUI only**. Chat is sent to tb-brain at `http://host.docker.internal:8765/v1` (model id `tb-brain`). Ollama stays on the host for tb-brain; the UI must not talk to `:11434`. Named volume `tb-brain-open-webui` lives in Docker's data root, not in this OneDrive repo.

## Start

1. Install [Ollama](https://ollama.com/) on Windows. Set `OLLAMA_MODELS` to a **non-OneDrive** disk (G: preferred; C: is tight). Example: `G:\ollama\models`.
2. Pull a local instruct model. Pick one that fits the current GPU; do not bake the size into code.

   ```powershell
   ollama pull qwen2.5:14b-instruct-q5_K_M
   # or
   ollama pull mistral-small:24b-instruct-2501-q4_K_M
   ```

3. Confirm Ollama: `curl.exe --noproxy "*" http://127.0.0.1:11434/api/tags`
4. Start tb-brain: `.\scripts\run.ps1` (must stay up; `BRAIN_HOST=0.0.0.0` so the container can reach it).
5. From the repo: `docker compose up -d`
6. Open `http://127.0.0.1:3000`. New chat, model **tb-brain**. Admin → Settings → Connections: Ollama off, only `http://host.docker.internal:8765/v1` with key `sk-tb-brain-lab`. Admin → Settings → Models → tb-brain: turn **Builtin Tools** off (Task Management's `update_task`, Notes, Calendar, Automations). The tool loop ignores those even if Open WebUI still sends them.

## How chat reaches tools

Open WebUI calls tb-brain `POST /v1/chat/completions`. `GET /v1/models` always returns id `tb-brain`. The tool loop maps that id to `LLM_MODEL` (local Ollama). Do not pick `llama3.1:8b` or `qwen2.5:7b-instruct` from an Ollama connection — those skip the tool loop.

```text
OPENAI_API_BASE_URL=http://host.docker.internal:8765/v1
OPENAI_API_KEY=sk-tb-brain-lab
```

Those are set in `docker-compose.yml`. `ENABLE_OLLAMA_API=false` so Compose cannot re-inject Ollama `:11434`.

## What not to enable

- Cloud model connections (OpenAI, Anthropic, Ollama Cloud) for client data
- Web search / browser tools
- Write-back to Flow
- Storing chat exports with PHI in this OneDrive repo
