# Open WebUI lab notes

Lab UI is [Open WebUI](https://github.com/open-webui/open-webui) in front of local Ollama. Do not build a custom chat app. Production UI later is inside Flow.

`docker-compose.yml` in this repo starts **Open WebUI only**. It talks to Ollama on the host (`http://host.docker.internal:11434`). Named volume `tb-brain-open-webui` lives in Docker's data root, not in this OneDrive repo.

## Start

1. Install [Ollama](https://ollama.com/) on Windows. Set `OLLAMA_MODELS` to a **non-OneDrive** disk (G: preferred; C: is tight). Example: `G:\ollama\models`.
2. Pull a local instruct model. Pick one that fits the current GPU; do not bake the size into code.

   ```powershell
   ollama pull qwen2.5:14b-instruct-q5_K_M
   # or
   ollama pull mistral-small:24b-instruct-2501-q4_K_M
   ```

3. Confirm Ollama: `curl http://127.0.0.1:11434/api/tags`
4. From the repo: `docker compose up -d`
5. Open `http://127.0.0.1:3000` and create the first admin locally.

## Two ways to get tools

### A. Open WebUI → Ollama (compose default)

Chat hits Ollama directly. Add tb-brain as an OpenAPI tool server:

- Settings → Tools → URL `http://host.docker.internal:8765/openapi.json`
- tb-brain must be running (`.\scripts\run.ps1`) and reachable from the container. If the tool server cannot connect, bind tb-brain with `BRAIN_HOST=0.0.0.0` (still Windows Firewall: local only).

### B. Open WebUI → tb-brain `/v1` (Python tool loop)

tb-brain runs the OpenAI-compatible tool loop against `LLM_BASE_URL`. Point Open WebUI at:

```text
OPENAI_API_BASE_URL=http://host.docker.internal:8765/v1
OPENAI_API_KEY=sk-tb-brain-lab
```

Uncomment those env vars in `docker-compose.yml` or set them in `.env`. This is the path that actually enforces "answer from tools, not RAG."

## What not to enable

- Cloud model connections (OpenAI, Anthropic, Ollama Cloud) for client data
- Web search / browser tools
- Write-back to Flow
- Storing chat exports with PHI in this OneDrive repo
