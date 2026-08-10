"""
llm-microsoft-foundry — LLM plugin for Microsoft Foundry models.

Authentication is handled transparently via azure-identity
(DefaultAzureCredential), which picks up an `az login` session.
No API key is required; bearer tokens are fetched and cached automatically.
"""
from __future__ import annotations

import json
import time
from typing import Iterator, Optional

import click
import httpx
import llm
import yaml
from pydantic import Field

# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

_FOUNDRY_SCOPE = "https://ai.azure.com/.default"
_FOUNDRY_API_VERSION = "v1"

_token_cache: dict = {}  # {"token": str, "expires_on": float}


def _get_access_token() -> str:
    """Return a cached (or freshly fetched) Entra access token."""
    now = time.time()
    if _token_cache.get("token") and _token_cache.get("expires_on", 0) > now + 60:
        return _token_cache["token"]

    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:
        raise click.ClickException(
            "azure-identity is required. Install it with: pip install azure-identity"
        ) from exc

    credential = DefaultAzureCredential()
    token = credential.get_token(_FOUNDRY_SCOPE)
    _token_cache["token"] = token.token
    _token_cache["expires_on"] = token.expires_on
    return token.token


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


@llm.hookimpl
def register_models(register):
    config = _load_config()
    for entry in config.get("models", []):
        model = FoundryModel(
            model_id=entry["model_id"],
            deployment=entry.get("deployment", entry["model_id"]),
            api_base=entry["api_base"],
            supports_tools=entry.get("supports_tools", False),
            supports_vision=entry.get("supports_vision", False),
        )
        aliases = entry.get("aliases", [])
        register(model, aliases=aliases)


@llm.hookimpl
def register_commands(cli):
    @cli.group(name="foundry")
    def foundry_group():
        "Commands for the llm-microsoft-foundry plugin"

    @foundry_group.command(name="list-models")
    def list_models():
        "List configured Microsoft Foundry models"
        config = _load_config()
        models = config.get("models", [])
        if not models:
            click.echo(
                "No models configured. Run 'llm foundry add-model' or edit "
                + str(_config_path())
            )
            return
        for m in models:
            click.echo(
                f"{m['model_id']!r}  ->  {m['api_base']}  "
                f"(deployment: {m.get('deployment', m['model_id'])})"
            )

    @foundry_group.command(name="add-model")
    @click.option("--model-id", required=True, help="Local model identifier (e.g. foundry-gpt-5.4)")
    @click.option(
        "--api-base",
        required=True,
        help="Foundry resource root URL, e.g. https://my-resource.services.ai.azure.com",
    )
    @click.option("--deployment", default=None, help="Exact deployment name (defaults to model-id)")
    @click.option("--tools/--no-tools", default=False, help="Whether the model supports tool calling")
    @click.option("--vision/--no-vision", default=False, help="Whether the model supports image input")
    @click.option("--alias", "aliases", multiple=True, help="Short alias(es) for the model")
    def add_model(model_id, api_base, deployment, tools, vision, aliases):
        "Add a Microsoft Foundry model to the configuration"
        config = _load_config()
        models = config.setdefault("models", [])
        # Replace existing entry with same model_id
        models = [m for m in models if m["model_id"] != model_id]
        entry: dict = {
            "model_id": model_id,
            "api_base": api_base.rstrip("/"),
            "deployment": deployment or model_id,
            "supports_tools": tools,
            "supports_vision": vision,
        }
        if aliases:
            entry["aliases"] = list(aliases)
        models.append(entry)
        config["models"] = models
        _save_config(config)
        click.echo(f"Added model {model_id!r}. Restart llm or reload models to use it.")

    @foundry_group.command(name="remove-model")
    @click.argument("model_id")
    def remove_model(model_id):
        "Remove a Microsoft Foundry model from the configuration"
        config = _load_config()
        before = len(config.get("models", []))
        config["models"] = [m for m in config.get("models", []) if m["model_id"] != model_id]
        _save_config(config)
        removed = before - len(config["models"])
        click.echo(f"Removed {removed} model(s).")

    @foundry_group.command(name="whoami")
    def whoami():
        "Show the current Azure identity used for Foundry authentication"
        try:
            token = _get_access_token()
            # Decode the JWT claims (informational only, no verification)
            import base64

            parts = token.split(".")
            if len(parts) >= 2:
                padding = "=" * (4 - len(parts[1]) % 4)
                claims = json.loads(base64.urlsafe_b64decode(parts[1] + padding))
                upn = (
                    claims.get("upn")
                    or claims.get("unique_name")
                    or claims.get("oid", "unknown")
                )
                tenant = claims.get("tid", "unknown")
                click.echo(f"Authenticated as: {upn}")
                click.echo(f"Tenant:           {tenant}")
            else:
                click.echo("Token obtained but could not decode claims.")
        except Exception as exc:
            raise click.ClickException(f"Authentication failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _config_path():
    return llm.user_dir() / "foundry_models.yaml"


def _legacy_json_path():
    return llm.user_dir() / "foundry_config.json"


def _load_config() -> dict:
    path = _config_path()
    legacy = _legacy_json_path()

    # Migrate old JSON config to YAML on first load
    if not path.exists() and legacy.exists():
        old = json.loads(legacy.read_text())
        _save_config(old)
        legacy.unlink()
        return old

    if path.exists():
        return yaml.safe_load(path.read_text()) or {"models": []}

    return {"models": []}


def _save_config(config: dict):
    path = _config_path()
    path.write_text(yaml.dump(config, default_flow_style=False, allow_unicode=True))


# ---------------------------------------------------------------------------
# Model implementation
# ---------------------------------------------------------------------------


class FoundryModel(llm.Model):
    can_stream = True

    class Options(llm.Options):
        temperature: Optional[float] = Field(
            description="Sampling temperature (0–2). Higher = more random.",
            ge=0,
            le=2,
            default=None,
        )
        max_tokens: Optional[int] = Field(
            description="Maximum number of tokens to generate.",
            ge=1,
            default=None,
        )
        top_p: Optional[float] = Field(
            description="Nucleus sampling probability mass.",
            ge=0,
            le=1,
            default=None,
        )

    def __init__(
        self,
        model_id: str,
        deployment: str,
        api_base: str,
        supports_tools: bool = False,
        supports_vision: bool = False,
    ):
        self.model_id = model_id
        self.deployment = deployment
        self.api_base = api_base.rstrip("/")
        self.supports_tools = supports_tools
        if supports_vision:
            self.attachment_types = {
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp",
            }

    def _chat_url(self) -> str:
        return f"{self.api_base}/openai/v1/chat/completions"

    def _build_messages(self, prompt, conversation) -> list[dict]:
        messages: list[dict] = []

        if conversation:
            for prev in conversation.responses:
                if prev.prompt.system and not any(
                    m.get("role") == "system" for m in messages
                ):
                    messages.append({"role": "system", "content": prev.prompt.system})
                if prev.prompt.prompt:
                    messages.append(
                        {
                            "role": "user",
                            "content": _msg_content(
                                prev.prompt.prompt, prev.prompt.attachments
                            ),
                        }
                    )
                messages.append({"role": "assistant", "content": prev.text()})

        if prompt.system and not any(m.get("role") == "system" for m in messages):
            messages.append({"role": "system", "content": prompt.system})

        if prompt.prompt:
            messages.append(
                {
                    "role": "user",
                    "content": _msg_content(prompt.prompt, prompt.attachments),
                }
            )

        return messages

    def _build_body(self, prompt, conversation) -> dict:
        body: dict = {
            "model": self.deployment,
            "messages": self._build_messages(prompt, conversation),
            "stream": True,
        }
        opts = prompt.options
        if opts.temperature is not None:
            body["temperature"] = opts.temperature
        if opts.max_tokens is not None:
            body["max_tokens"] = opts.max_tokens
        if opts.top_p is not None:
            body["top_p"] = opts.top_p
        return body

    def execute(self, prompt, stream, response, conversation) -> Iterator[str]:
        token = _get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        body = self._build_body(prompt, conversation)

        if stream:
            yield from self._stream(headers, body, response)
        else:
            body["stream"] = False
            with httpx.Client() as client:
                r = client.post(
                    self._chat_url(), headers=headers, json=body, timeout=120
                )
                _raise_for_status(r)
                data = r.json()
                choice = data["choices"][0]
                text = choice["message"]["content"] or ""
                response.response_json = data
                yield text

    def _stream(self, headers: dict, body: dict, response) -> Iterator[str]:
        chunks: list = []
        with httpx.Client() as client:
            with client.stream(
                "POST",
                self._chat_url(),
                headers=headers,
                json=body,
                timeout=120,
            ) as r:
                if r.status_code >= 400:
                    r.read()  # must consume body before accessing it in stream context
                    _raise_for_status(r)
                for line in r.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[len("data: "):]
                    if payload.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    chunks.append(chunk)
                    choices = chunk.get("choices")
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    text = delta.get("content") or ""
                    if text:
                        yield text
        if chunks:
            response.response_json = chunks[-1]


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _msg_content(text: str, attachments=None):
    """Build message content; wraps text + image attachments into a list."""
    if not attachments:
        return text
    content = [{"type": "text", "text": text or ""}]
    for att in attachments:
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": att.url
                    or f"data:{att.resolve_type()};base64,{att.base64_content()}"
                },
            }
        )
    return content


def _raise_for_status(r: httpx.Response):
    if r.status_code >= 400:
        try:
            # Streaming responses need read() before accessing body
            if not r.is_stream_consumed:
                r.read()
            detail = r.json()
        except Exception:
            try:
                detail = r.text
            except Exception:
                detail = f"HTTP {r.status_code}"
        raise click.ClickException(
            f"Foundry API error {r.status_code}: {json.dumps(detail, indent=2)}"
        )
