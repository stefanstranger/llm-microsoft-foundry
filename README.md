---
title: LLM Microsoft Foundry
description: Use Microsoft Foundry chat and decision models with the LLM command-line tool
---

LLM plugin for [Microsoft Foundry](https://learn.microsoft.com/azure/ai-foundry/) models.

Authenticates transparently using `az login` (no API key needed) and lets you
use chat and decision models deployed in a Foundry resource directly from the
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
  --api-type chat \
  --tools \
  --alias gpt54
```

| Option | Description |
|--------|-------------|
| `--model-id` | Unique local identifier (used with `llm -m`) |
| `--api-base` | Foundry resource root URL (the **resource root**, not a project URL) |
| `--deployment` | Exact deployment name in Foundry (defaults to `--model-id`) |
| `--api-type` | Inference API exposed by the deployment: `chat` or `decision` |
| `--tools` / `--no-tools` | Enable tool calling support |
| `--vision` / `--no-vision` | Enable image attachment support |
| `--alias` | Short alias(es), repeatable |

The API type defaults to `chat`, so configurations created by earlier plugin
versions continue to work. Decision models do not support the `--tools` or
`--vision` options.

Settings are stored in `<llm user dir>/foundry_models.yaml`, in the same folder
as LLM's own configuration files
(`C:\Users\<you>\AppData\Roaming\io.datasette.llm\` on Windows). You can edit it
directly:

```yaml
models:
- model_id: foundry-gpt-5.4
  api_base: https://my-resource.services.ai.azure.com
  deployment: gpt-5.4
  api_type: chat
  supports_tools: true
  supports_vision: false

- model_id: foundry-deepseek-v3.2
  api_base: https://my-resource.services.ai.azure.com
  deployment: DeepSeek-V3.2
api_type: chat
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

## Use Microsoft-Decision-1

[Microsoft-Decision-1](https://learn.microsoft.com/azure/foundry/foundry-models/how-to/use-foundry-models-microsoft-decision)
returns bounded, numerical decisions instead of generated prose. It supports
`noul` questions for probabilities, `choice` questions for classification, and
`score` questions for ordered scales.

Deploy the model in Foundry, then configure the deployment:

```powershell
llm foundry add-model `
  --model-id foundry-decision-1 `
  --api-base https://<your-resource>.services.ai.azure.com `
  --deployment <your-decision-deployment> `
  --api-type decision `
  --alias decision
```

The `--deployment` value must be the deployment name from Foundry, not the
underlying `Microsoft-Decision-1` model name.

### Pass state text and questions

Provide the state as the prompt and the questions as a JSON model option:

```powershell
$questions = @'
{
  "refund": {
    "type": "noul",
    "instructions": "Does the customer request a refund?"
  }
}
'@

llm -m decision `
  -o questions $questions `
  "I was charged twice. Please refund the duplicate charge."
```

### Pass a complete decision request

Pass a complete request through standard input when the state is structured or
the request contains several questions:

```powershell
@'
{
  "state": {
    "message": "Checkout is down again. This is my third request.",
    "customer_contact_count": 3
  },
  "questions": {
    "team": {
      "type": "choice",
      "instructions": "Which team should handle this request?",
      "criteria": {
        "billing": "Charges, invoices, and refunds",
        "engineering": "Bugs, errors, and outages",
        "support": "How-to and account questions"
      }
    },
    "severity": {
      "type": "score",
      "instructions": "How severe is this issue?",
      "criteria": ["Cosmetic", "Minor", "Major", "Critical"]
    },
    "repeat_contact": {
      "type": "noul",
      "instructions": "Has the customer contacted support before?"
    }
  }
}
'@ | llm -m decision
```

The plugin adds the configured deployment name to the request and emits the
complete Decision API response as JSON. Token counts from the response are
available through LLM's usage metadata.

> [!NOTE]
> Decision models do not support streaming, conversations, system prompts,
> attachments, tools, or schemas.

## Remove a model

```powershell
llm foundry remove-model foundry-gpt-5.4
```

## Required Azure role

The identity must have the **Cognitive Services User** role on the Foundry
resource. Owner and Contributor roles alone do not grant inference access.

## How it works

```
llm CLI
  └─ llm-microsoft-foundry plugin
       ├─ DefaultAzureCredential (azure-identity)
       │    └─ uses the Azure CLI credential cache
       ├─ Chat
       │    └─ POST <api_base>/openai/v1/chat/completions
       └─ Decision
            └─ POST <api_base>/providers/microsoft/v1/systemone
```

Chat and decision requests use the Entra token scope required by their
respective Foundry APIs. No proxy, extra service, or master key is involved.

## Development

```powershell
git clone https://github.com/stefanstranger/llm-microsoft-foundry
cd llm-microsoft-foundry
llm install -e .
uv sync
uv run pytest
```
