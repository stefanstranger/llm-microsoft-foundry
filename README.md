# llm-microsoft-foundry

LLM plugin for [Microsoft Foundry](https://learn.microsoft.com/azure/ai-foundry/) models.

Authenticates transparently using `az login` (no API key needed) and lets you
talk to any model deployed in a Foundry resource directly from the
[llm](https://llm.datasette.io/) CLI.

## Install

```powershell
llm install llm-microsoft-foundry
# or, from a local clone:
llm install -e .
```

Dependencies (`azure-identity`, `httpx`) are installed automatically.

## Authenticate

```powershell
az login
az account set --subscription "<subscription-id-or-name>"
```

The plugin uses `DefaultAzureCredential`, so any credential that works with the
Azure SDK (az cli cache, managed identity, env vars) works here too.

## Configure a model

```powershell
llm foundry add-model \
  --model-id foundry-gpt-5.4 \
  --api-base https://<your-resource>.services.ai.azure.com \
  --deployment gpt-5.4 \
  --tools \
  --alias gpt54
```

| Option | Description |
|--------|-------------|
| `--model-id` | Unique local identifier (used with `llm -m`) |
| `--api-base` | Foundry resource root URL (the **resource root**, not a project URL) |
| `--deployment` | Exact deployment name in Foundry (defaults to `--model-id`) |
| `--tools` / `--no-tools` | Enable tool calling support |
| `--vision` / `--no-vision` | Enable image attachment support |
| `--alias` | Short alias(es), repeatable |

Settings are stored in `<llm user dir>/foundry_models.yaml` — same folder as llm's own config files (`C:\Users\<you>\AppData\Roaming\io.datasette.llm\` on Windows). You can edit it directly:

```yaml
models:
- model_id: foundry-gpt-5.4
  api_base: https://my-resource.services.ai.azure.com
  deployment: gpt-5.4
  supports_tools: true
  supports_vision: false

- model_id: foundry-deepseek-v3.2
  api_base: https://my-resource.services.ai.azure.com
  deployment: DeepSeek-V3.2
  supports_tools: false
  supports_vision: false
  aliases:
  - deepseek
```

> **Migrating from an older version?** If you have a `foundry_config.json`, it is migrated to `foundry_models.yaml` automatically on the next `llm` invocation.


## Use

```powershell
# List configured models
llm foundry list-models

# Prompt a model
llm -m foundry-gpt-5.4 "What is the capital of France?"

# Or using an alias
llm -m gpt54 "Explain quantum entanglement in one sentence"

# Set as default model
llm models default foundry-gpt-5.4

# Check which Azure identity is active
llm foundry whoami
```

### Options

```powershell
llm -m foundry-gpt-5.4 -o temperature 0.2 -o max_tokens 512 "Write a haiku"
```

| Option | Default | Description |
|--------|---------|-------------|
| `temperature` | model default | Sampling temperature (0–2) |
| `max_tokens` | model default | Max output tokens |
| `top_p` | model default | Nucleus sampling probability |

## Remove a model

```powershell
llm foundry remove-model foundry-gpt-5.4
```

## Required Azure role

The identity must have the **Cognitive Services User** role on the Foundry
resource (Owner/Contributor alone does not grant inference access).

## How it works

```
llm CLI
  └─ llm-microsoft-foundry plugin
       ├─ DefaultAzureCredential (azure-identity)  ← uses az login cache
       │    └─ Entra token for https://ai.azure.com/.default
       └─ POST <api_base>/openai/deployments/<deployment>/chat/completions?api-version=v1
              Authorization: Bearer <token>
```

No proxy, no extra service, no master key — direct to Foundry.

## Development

```powershell
git clone https://github.com/stefanstranger/llm-microsoft-foundry
cd llm-microsoft-foundry
llm install -e .
```

