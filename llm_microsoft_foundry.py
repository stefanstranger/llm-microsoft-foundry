"""
llm-microsoft-foundry — LLM plugin for Microsoft Foundry models.

Authentication is handled transparently via azure-identity
(DefaultAzureCredential), which picks up an `az login` session.
No API key is required; bearer tokens are fetched and cached automatically.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator, Mapping
from typing import Any

import click
import httpx
import llm
import yaml
from pydantic import Field

# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

_FOUNDRY_CHAT_SCOPE = "https://ai.azure.com/.default"
_FOUNDRY_DECISION_SCOPE = "https://cognitiveservices.azure.com/.default"

_token_cache: dict[str, dict[str, str | float]] = {}


def _get_access_token(scope: str = _FOUNDRY_CHAT_SCOPE) -> str:
    """Return a cached (or freshly fetched) Entra access token."""
    now = time.time()
    cached = _token_cache.get(scope, {})
    cached_token = cached.get("token")
    expires_on = cached.get("expires_on", 0)
    if (
        isinstance(cached_token, str)
        and isinstance(expires_on, (int, float))
        and expires_on > now + 60
    ):
        return cached_token

    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as exc:
        raise click.ClickException(
            "azure-identity is required. Install it with: pip install azure-identity"
        ) from exc

    credential = DefaultAzureCredential()
    token = credential.get_token(scope)
    _token_cache[scope] = {
        "token": token.token,
        "expires_on": token.expires_on,
    }
    return token.token


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


@llm.hookimpl
def register_models(register):
    config = _load_config()
    for entry in config.get("models", []):
        api_type = entry.get("api_type", "chat")
        common_args = {
            "model_id": entry["model_id"],
            "deployment": entry.get("deployment", entry["model_id"]),
            "api_base": entry["api_base"],
        }
        if api_type == "chat":
            model = FoundryModel(
                **common_args,
                supports_tools=entry.get("supports_tools", False),
                supports_vision=entry.get("supports_vision", False),
            )
        elif api_type == "decision":
            model = FoundryDecisionModel(**common_args)
        else:
            raise click.ClickException(
                f"Unsupported api_type {api_type!r} for model "
                f"{entry['model_id']!r}. Expected 'chat' or 'decision'."
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
                f"(deployment: {m.get('deployment', m['model_id'])}, "
                f"api type: {m.get('api_type', 'chat')})"
            )

    @foundry_group.command(name="add-model")
    @click.option(
        "--model-id",
        required=True,
        help="Local model identifier (e.g. foundry-gpt-5.4)",
    )
    @click.option(
        "--api-base",
        required=True,
        help="Foundry resource root URL, e.g. https://my-resource.services.ai.azure.com",
    )
    @click.option(
        "--deployment",
        default=None,
        help="Exact deployment name (defaults to model-id)",
    )
    @click.option(
        "--api-type",
        type=click.Choice(["chat", "decision"]),
        default="chat",
        show_default=True,
        help="Foundry inference API exposed by the deployment",
    )
    @click.option(
        "--tools/--no-tools",
        default=False,
        help="Whether the model supports tool calling",
    )
    @click.option(
        "--vision/--no-vision",
        default=False,
        help="Whether the model supports image input",
    )
    @click.option(
        "--alias", "aliases", multiple=True, help="Short alias(es) for the model"
    )
    def add_model(model_id, api_base, deployment, api_type, tools, vision, aliases):
        "Add a Microsoft Foundry model to the configuration"
        if api_type == "decision" and (tools or vision):
            raise click.UsageError(
                "Decision models do not support --tools or --vision."
            )
        config = _load_config()
        models = config.setdefault("models", [])
        # Replace existing entry with same model_id
        models = [m for m in models if m["model_id"] != model_id]
        entry: dict = {
            "model_id": model_id,
            "api_base": api_base.rstrip("/"),
            "deployment": deployment or model_id,
            "api_type": api_type,
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
        config["models"] = [
            m for m in config.get("models", []) if m["model_id"] != model_id
        ]
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
        temperature: float | None = Field(
            description="Sampling temperature (0–2). Higher = more random.",
            ge=0,
            le=2,
            default=None,
        )
        max_tokens: int | None = Field(
            description="Maximum number of tokens to generate.",
            ge=1,
            default=None,
        )
        top_p: float | None = Field(
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
        """Translate LLM's message chain into chat-completions messages.

        ``prompt.messages`` is the full history LLM wants sent for this turn,
        including earlier conversation turns, assistant tool calls, and
        tool results, so ``conversation`` is not consulted here.
        """
        del conversation
        messages: list[dict] = []
        current_system: str | None = None
        for message in prompt.messages:
            current_system = _append_message(messages, message, current_system)

        if prompt.system and not any(m["role"] == "system" for m in messages):
            messages.insert(0, {"role": "system", "content": prompt.system})

        return messages

    def _build_body(self, prompt, conversation) -> dict:
        body: dict = {
            "model": self.deployment,
            "messages": self._build_messages(prompt, conversation),
            "stream": True,
        }
        if prompt.tools:
            body["tools"] = [_tool_spec(tool) for tool in prompt.tools]
        opts = prompt.options
        if opts.temperature is not None:
            body["temperature"] = opts.temperature
        if opts.max_tokens is not None:
            body["max_tokens"] = opts.max_tokens
        if opts.top_p is not None:
            body["top_p"] = opts.top_p
        return body

    @staticmethod
    def _switch_to_max_completion_tokens(body: dict) -> dict:
        """Translate max_tokens to max_completion_tokens in a request body copy."""
        if "max_tokens" not in body:
            return body
        updated = dict(body)
        updated["max_completion_tokens"] = updated.pop("max_tokens")
        return updated

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
                if r.status_code >= 400:
                    try:
                        detail = r.json()
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        detail = None
                    if _wants_max_completion_tokens(detail) and "max_tokens" in body:
                        body = self._switch_to_max_completion_tokens(body)
                        r = client.post(
                            self._chat_url(),
                            headers=headers,
                            json=body,
                            timeout=120,
                        )
                _raise_for_status(r)
                data = r.json()
                response.response_json = data
                message = data["choices"][0]["message"]
                text = message.get("content") or ""
                parsed_calls = [
                    _parse_tool_call(
                        tool_call.get("id"),
                        (tool_call.get("function") or {}).get("name"),
                        (tool_call.get("function") or {}).get("arguments"),
                    )
                    for tool_call in message.get("tool_calls") or []
                ]
                for tool_call in parsed_calls:
                    response.add_tool_call(tool_call)
                if text:
                    yield text

    def _stream(self, headers: dict, body: dict, response) -> Iterator[str]:
        chunks: list = []
        # Streamed tool calls arrive as fragments keyed by their index.
        tool_calls: dict[int, dict[str, Any]] = {}
        attempts = [body]
        if "max_tokens" in body:
            attempts.append(self._switch_to_max_completion_tokens(body))

        with httpx.Client() as client:
            for index, attempt_body in enumerate(attempts):
                with client.stream(
                    "POST",
                    self._chat_url(),
                    headers=headers,
                    json=attempt_body,
                    timeout=120,
                ) as r:
                    if r.status_code >= 400:
                        r.read()  # must consume body before accessing it in stream context
                        if index == 0 and "max_tokens" in attempt_body:
                            try:
                                detail = r.json()
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                detail = None
                            if _wants_max_completion_tokens(detail):
                                continue
                        _raise_for_status(r)

                    for line in r.iter_lines():
                        if not line.startswith("data: "):
                            continue
                        payload = line[len("data: ") :]
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
                        delta = choices[0].get("delta") or {}
                        for fragment in delta.get("tool_calls") or []:
                            _merge_tool_call_fragment(tool_calls, fragment)
                        text = delta.get("content") or ""
                        if text:
                            yield text
                    break
        if chunks:
            response.response_json = chunks[-1]
        parsed_calls = [
            _parse_tool_call(call["id"], call["name"], call["arguments"])
            for _, call in sorted(tool_calls.items())
        ]
        for tool_call in parsed_calls:
            response.add_tool_call(tool_call)


def _parse_json_object(value: str | Mapping[str, Any], name: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be valid JSON: {exc.msg}") from exc

    if not isinstance(parsed, dict):
        raise ValueError(f"{name} must be a JSON object")  # noqa: TRY004
    return parsed


class FoundryDecisionModel(llm.Model):
    can_stream = False

    class Options(llm.Options):  # pyright: ignore[reportIncompatibleVariableOverride]
        questions: str | dict[str, Any] | None = Field(
            description=(
                "Decision questions as a JSON object. When omitted, the prompt "
                "must contain the complete Decision API request."
            ),
            default=None,
        )

    def __init__(self, model_id: str, deployment: str, api_base: str):
        self.model_id = model_id
        self.deployment = deployment
        self.api_base = api_base.rstrip("/")

    def _decision_url(self) -> str:
        return f"{self.api_base}/providers/microsoft/v1/systemone"

    def _build_body(self, prompt) -> dict[str, Any]:
        if prompt.system:
            raise ValueError("Decision models do not support system prompts")
        if prompt.attachments:
            raise ValueError("Decision models do not support attachments")

        options = prompt.options
        if not isinstance(options, self.Options):
            raise TypeError("Decision API requires decision model options")

        if options.questions is not None:
            if prompt.prompt is None:
                raise ValueError("Decision API state text is required")
            return {
                "model": self.deployment,
                "state": prompt.prompt,
                "questions": _parse_json_object(options.questions, "questions"),
            }

        if prompt.prompt is None:
            raise ValueError(
                "Provide a JSON request payload or state text with the questions option"
            )

        payload = _parse_json_object(prompt.prompt, "Decision API request")
        if "state" not in payload:
            raise ValueError("Decision API request must include state")
        if not isinstance(payload.get("questions"), dict):
            raise ValueError(  # noqa: TRY004
                "Decision API request questions must be a JSON object"
            )
        payload["model"] = self.deployment
        return payload

    def execute(self, prompt, stream, response, conversation) -> Iterator[str]:
        del stream
        if conversation is not None and conversation.responses:
            raise ValueError("Decision models do not support conversations")

        token = _get_access_token(_FOUNDRY_DECISION_SCOPE)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        with httpx.Client() as client:
            api_response = client.post(
                self._decision_url(),
                headers=headers,
                json=self._build_body(prompt),
                timeout=120,
            )
        _raise_for_status(api_response)

        try:
            data = api_response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise click.ClickException(
                "Foundry Decision API returned invalid JSON."
            ) from exc
        if not isinstance(data, dict):
            raise click.ClickException(
                "Foundry Decision API response must be a JSON object."
            )

        usage = data.get("usage")
        if isinstance(usage, dict):
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")
            response.set_usage(
                input=input_tokens if type(input_tokens) is int else None,
                output=output_tokens if type(output_tokens) is int else None,
            )

        response.response_json = data
        yield json.dumps(data, ensure_ascii=False)


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


def _append_message(out: list[dict], message, current_system: str | None):
    """Append one ``llm.parts.Message`` to ``out`` in chat-completions format.

    Returns the active system prompt so repeated identical system messages
    are only sent once.
    """
    from llm.parts import AttachmentPart, TextPart, ToolCallPart, ToolResultPart

    text_bits: list[str] = []
    attachments = []
    tool_calls: list[dict] = []
    for part in message.parts:
        if isinstance(part, TextPart):
            text_bits.append(part.text)
        elif isinstance(part, AttachmentPart) and part.attachment:
            attachments.append(part.attachment)
        elif isinstance(part, ToolCallPart) and not part.server_executed:
            tool_calls.append(
                {
                    "id": part.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": part.name,
                        "arguments": json.dumps(part.arguments),
                    },
                }
            )
        elif isinstance(part, ToolResultPart) and not part.server_executed:
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": part.tool_call_id,
                    "content": part.output,
                }
            )

    if message.role == "tool":
        return current_system

    text = "".join(text_bits)
    if message.role == "system":
        if not text or text == current_system:
            return current_system
        out.append({"role": "system", "content": text})
        return text

    if message.role == "assistant":
        entry: dict[str, Any] = {"role": "assistant", "content": text}
        if tool_calls:
            entry["tool_calls"] = tool_calls
            # Chat completions expects null content for tool-call-only turns.
            if not text:
                entry["content"] = None
        out.append(entry)
        return current_system

    if text or attachments:
        out.append({"role": message.role, "content": _msg_content(text, attachments)})
    return current_system


def _tool_spec(tool) -> dict:
    """Describe an LLM tool in the chat-completions function-tool format."""
    function: dict[str, Any] = {"name": tool.name}
    if tool.description:
        function["description"] = tool.description
    function["parameters"] = tool.input_schema
    return {"type": "function", "function": function}


def _merge_tool_call_fragment(
    tool_calls: dict[int, dict[str, Any]], fragment: Mapping[str, Any]
) -> None:
    """Fold one streamed tool-call delta into the call at its index."""
    index = fragment.get("index")
    if not isinstance(index, int):
        raise click.ClickException(
            f"Foundry streamed a tool call without an index: {json.dumps(fragment)}"
        )
    call = tool_calls.setdefault(index, {"id": None, "name": None, "arguments": ""})
    if fragment.get("id") and not call["id"]:
        call["id"] = fragment["id"]
    function = fragment.get("function") or {}
    if function.get("name") and not call["name"]:
        call["name"] = function["name"]
    if function.get("arguments"):
        call["arguments"] += function["arguments"]


def _parse_tool_call(
    tool_call_id: str | None, name: str | None, arguments: str | None
) -> llm.ToolCall:
    """Build an ``llm.ToolCall``, rejecting missing names or malformed arguments."""
    label = f"tool call {tool_call_id!r}" + (f" to {name!r}" if name else "")
    if not name:
        raise click.ClickException(f"Foundry returned {label} without a function name.")
    # An absent or empty argument string means the function takes no arguments.
    if arguments is None or not arguments.strip():
        parsed: Any = {}
    else:
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError as exc:
            raise click.ClickException(
                f"Foundry returned malformed JSON arguments for {label}: "
                f"{exc.msg}. Raw arguments: {arguments}"
            ) from exc
    if not isinstance(parsed, dict):
        raise click.ClickException(
            f"Foundry returned arguments for {label} that are not a JSON object. "
            f"Raw arguments: {arguments}"
        )
    return llm.ToolCall(name=name, arguments=parsed, tool_call_id=tool_call_id)


def _raise_for_status(r: httpx.Response):
    if r.status_code >= 400:
        # Streaming responses need read() before accessing body
        if not r.is_stream_consumed:
            try:
                r.read()
            except httpx.HTTPError as exc:
                raise click.ClickException(
                    f"Foundry API error {r.status_code}: {exc}"
                ) from exc
        try:
            detail = r.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            detail = r.text
        raise click.ClickException(
            f"Foundry API error {r.status_code}: {json.dumps(detail, indent=2)}"
        )


def _wants_max_completion_tokens(error_detail) -> bool:
    """Return True if API asks to use max_completion_tokens instead of max_tokens."""
    if not isinstance(error_detail, dict):
        return False
    err = error_detail.get("error")
    if not isinstance(err, dict):
        return False

    param = err.get("param")
    code = err.get("code")
    message = str(err.get("message", "")).lower()

    return (
        param == "max_tokens"
        and code == "unsupported_parameter"
        and "max_completion_tokens" in message
    )
