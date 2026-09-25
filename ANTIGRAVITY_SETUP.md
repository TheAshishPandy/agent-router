# Antigravity + Agent Router

Use Agent Router as the local Anthropic-compatible gateway for Antigravity.

## Endpoint

```text
http://127.0.0.1:8001/api
```

The main endpoint is:

```text
POST /api/v1/messages
```

## Provider order

```text
Hugging Face -> Ollama -> OpenRouter
```

The active Hugging Face model is configured by `HF_MODEL` and currently defaults to:

```text
openai/gpt-oss-120b:fastest
```

The router does not make external model usage unlimited. Hugging Face and other hosted providers remain subject to their quotas, rate limits, availability, and account terms. The cascade reduces interruptions by trying the next configured provider when a provider cannot serve a request.

## Configure the local environment

Create `.env` from `.env.example` and set the Hugging Face token locally. Never commit the token.

```text
HF_TOKEN=hf_your_token_here
HF_MODEL=openai/gpt-oss-120b:fastest
OLLAMA_MODEL=qwen2.5-coder:1.5b
OPENROUTER_API_KEY=
OPENROUTER_MODEL=openrouter/free
```

## Start continuously

For a foreground continuous runner:

```powershell
.\start-agent-router.ps1
```

The runner restarts the FastAPI process five seconds after it exits and writes logs to `logs/`.

## Start with Windows

Open **PowerShell as Administrator** in the repository directory:

```powershell
.\install-agent-router-task.ps1
```

This creates the `Agent Router - Continuous` scheduled task, starts it immediately, and configures it to start at Windows boot.

To remove it:

```powershell
.\uninstall-agent-router-task.ps1
```

## Verify

```powershell
Invoke-WebRequest http://127.0.0.1:8001/health
```

Then point Antigravity at:

```text
http://127.0.0.1:8001/api
```

For `/api/v1/messages`, the response includes `X-Cascade`, which identifies the provider that served the request.
