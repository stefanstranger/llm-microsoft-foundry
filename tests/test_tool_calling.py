import json

import click
import httpx
import llm
import pytest
from click.testing import CliRunner
from llm.parts import TextPart, ToolCallPart

import llm_microsoft_foundry as foundry

API_BASE = "https://example.services.ai.azure.com"
CHAT_URL = f"{API_BASE}/openai/v1/chat/completions"


def lookup_chat(name: str) -> str:
    "Find a chat by its display name."
    return f"chat-id-for-{name}"


def count_letters(word: str, letter: str) -> int:
    "Count how many times a letter appears in a word."
    return word.count(letter)


class FakeFoundry:
    """Serve queued chat-completions responses through httpx.MockTransport."""

    def __init__(self, monkeypatch):
        self.requests: list[dict] = []
        self.responses: list[httpx.Response] = []
        real_client = httpx.Client

        def client_factory(*args, **kwargs):
            return real_client(transport=httpx.MockTransport(self._handle))

        monkeypatch.setattr(foundry.httpx, "Client", client_factory)
        monkeypatch.setattr(foundry, "_get_access_token", lambda *_: "token")

    def _handle(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url) == CHAT_URL
        assert request.headers["Authorization"] == "Bearer token"
        self.requests.append(json.loads(request.content))
        return self.responses.pop(0)

    def reply(self, message: dict, finish_reason: str = "stop"):
        self.responses.append(
            httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", **message},
                            "finish_reason": finish_reason,
                        }
                    ],
                },
            )
        )

    def reply_stream(self, deltas: list[dict]):
        lines = []
        for delta in deltas:
            chunk = {"id": "chatcmpl-1", "choices": [{"index": 0, "delta": delta}]}
            lines.append(f"data: {json.dumps(chunk)}\n\n")
        lines.append("data: [DONE]\n\n")
        self.responses.append(
            httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content="".join(lines).encode(),
            )
        )

    def reply_text(self, text: str, stream: bool):
        if stream:
            self.reply_stream([{"role": "assistant", "content": text}])
        else:
            self.reply({"content": text})

    def reply_tool_calls(self, calls: list[tuple[str, str, dict]], stream: bool):
        if stream:
            deltas: list[dict] = [{"role": "assistant", "content": None}]
            for index, (call_id, name, arguments) in enumerate(calls):
                deltas.append(
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ]
                    }
                )
            self.reply_stream(deltas)
        else:
            self.reply(
                {
                    "content": None,
                    "tool_calls": [
                        _tool_call(call_id, name, json.dumps(arguments))
                        for call_id, name, arguments in calls
                    ],
                },
                finish_reason="tool_calls",
            )

    def reply_error(self, status: int, body: dict):
        self.responses.append(httpx.Response(status, json=body))


def _tool_call(call_id: str, name: str, arguments: str) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _model(**kwargs) -> foundry.FoundryModel:
    return foundry.FoundryModel(
        model_id="foundry-test",
        deployment="test-deployment",
        api_base=API_BASE,
        supports_tools=kwargs.pop("supports_tools", True),
        **kwargs,
    )


LOOKUP_CHAT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "lookup_chat",
        "description": "Find a chat by its display name.",
        "parameters": {
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "type": "object",
        },
    },
}


# ---------------------------------------------------------------------------
# 1. Outgoing tool schemas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stream", [False, True])
def test_tools_are_serialized_as_openai_function_tools(monkeypatch, stream):
    api = FakeFoundry(monkeypatch)
    api.reply_text("No tool needed.", stream)
    custom = llm.Tool(
        name="search_index",
        description="Search the local index.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms"},
                "limit": {"type": "integer", "minimum": 1},
            },
            "required": ["query"],
        },
        implementation=lambda query, limit=5: query,
    )

    response = _model().prompt(
        "Find something", tools=[lookup_chat, custom], stream=stream
    )

    assert response.text() == "No tool needed."
    assert api.requests[0]["tools"] == [
        LOOKUP_CHAT_SCHEMA,
        {
            "type": "function",
            "function": {
                "name": "search_index",
                "description": "Search the local index.",
                "parameters": custom.input_schema,
            },
        },
    ]


# ---------------------------------------------------------------------------
# 2. Non-streaming tool-only response with null content
# ---------------------------------------------------------------------------


def test_non_streaming_tool_only_response_registers_tool_calls(monkeypatch):
    api = FakeFoundry(monkeypatch)
    api.reply_tool_calls(
        [
            ("call_a", "lookup_chat", {"name": "Project X"}),
            ("call_b", "count_letters", {"word": "banana", "letter": "a"}),
        ],
        stream=False,
    )

    response = _model().prompt(
        "Find a chat", tools=[lookup_chat, count_letters], stream=False
    )

    assert response.text() == ""
    assert response.tool_calls() == [
        llm.ToolCall(
            name="lookup_chat",
            arguments={"name": "Project X"},
            tool_call_id="call_a",
        ),
        llm.ToolCall(
            name="count_letters",
            arguments={"word": "banana", "letter": "a"},
            tool_call_id="call_b",
        ),
    ]
    assert api.requests[0]["stream"] is False


# ---------------------------------------------------------------------------
# 3. Text and tool calls in the same response
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stream", [False, True])
def test_response_with_text_and_tool_calls_preserves_both(monkeypatch, stream):
    api = FakeFoundry(monkeypatch)
    if stream:
        api.reply_stream(
            [
                {"role": "assistant", "content": "Let me "},
                {"content": "check."},
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "lookup_chat",
                                "arguments": '{"name": "Ops"}',
                            },
                        }
                    ]
                },
            ]
        )
    else:
        api.reply(
            {
                "content": "Let me check.",
                "tool_calls": [_tool_call("call_1", "lookup_chat", '{"name": "Ops"}')],
            },
            finish_reason="tool_calls",
        )

    response = _model().prompt("Find Ops", tools=[lookup_chat], stream=stream)

    assert response.text() == "Let me check."
    assert response.tool_calls() == [
        llm.ToolCall(
            name="lookup_chat", arguments={"name": "Ops"}, tool_call_id="call_1"
        )
    ]
    [message] = response.messages()
    assert message.role == "assistant"
    assert message.parts == [
        TextPart(text="Let me check."),
        ToolCallPart(
            name="lookup_chat", arguments={"name": "Ops"}, tool_call_id="call_1"
        ),
    ]


# ---------------------------------------------------------------------------
# 4. Streamed fragmented arguments and interleaved calls
# ---------------------------------------------------------------------------


def test_streamed_fragmented_interleaved_tool_calls_are_assembled(monkeypatch):
    api = FakeFoundry(monkeypatch)

    def fragment(index, arguments, call_id=None, name=None):
        function = {"arguments": arguments}
        if name:
            function["name"] = name
        call = {"index": index, "function": function}
        if call_id:
            call["id"] = call_id
            call["type"] = "function"
        return {"tool_calls": [call]}

    api.reply_stream(
        [
            {"role": "assistant", "content": None},
            fragment(0, "", call_id="call_0", name="lookup_chat"),
            fragment(0, '{"na'),
            fragment(1, "", call_id="call_1", name="count_letters"),
            fragment(1, '{"word": "ban'),
            fragment(0, 'me": "Team'),
            fragment(1, 'ana", "letter"'),
            fragment(0, ' Chat"}'),
            fragment(1, ': "n"}'),
            {"content": None},
        ]
    )

    response = _model().prompt(
        "Do both", tools=[lookup_chat, count_letters], stream=True
    )

    assert response.text() == ""
    assert response.tool_calls() == [
        llm.ToolCall(
            name="lookup_chat",
            arguments={"name": "Team Chat"},
            tool_call_id="call_0",
        ),
        llm.ToolCall(
            name="count_letters",
            arguments={"word": "banana", "letter": "n"},
            tool_call_id="call_1",
        ),
    ]
    assert api.requests[0]["stream"] is True


def test_streamed_tool_calls_in_single_chunk_are_assembled(monkeypatch):
    api = FakeFoundry(monkeypatch)
    api.reply_stream(
        [
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_x",
                        "type": "function",
                        "function": {"name": "lookup_chat", "arguments": '{"name"'},
                    },
                    {
                        "index": 1,
                        "id": "call_y",
                        "type": "function",
                        "function": {"name": "lookup_chat", "arguments": '{"name"'},
                    },
                ]
            },
            {
                "tool_calls": [
                    {"index": 1, "function": {"arguments": ': "B"}'}},
                    {"index": 0, "function": {"arguments": ': "A"}'}},
                ]
            },
        ]
    )

    response = _model().prompt("Find A and B", tools=[lookup_chat])

    assert [(c.tool_call_id, c.arguments) for c in response.tool_calls()] == [
        ("call_x", {"name": "A"}),
        ("call_y", {"name": "B"}),
    ]


# ---------------------------------------------------------------------------
# 5. End-to-end tool loop through LLM's chain machinery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stream", [False, True])
def test_chain_executes_local_tool_and_sends_results(monkeypatch, stream):
    api = FakeFoundry(monkeypatch)
    api.reply_tool_calls([("call_1", "lookup_chat", {"name": "Design"})], stream)
    api.reply_text("The Design chat id is chat-id-for-Design.", stream)
    executed = []

    chain = _model().chain(
        "Use the available tools to find a chat",
        system="You are helpful.",
        tools=[lookup_chat],
        stream=stream,
        after_call=lambda tool, call, result: executed.append(
            (tool.name, call.arguments, result.output)
        ),
    )

    assert chain.text() == "The Design chat id is chat-id-for-Design."
    assert executed == [("lookup_chat", {"name": "Design"}, "chat-id-for-Design")]
    assert len(api.requests) == 2
    first, second = api.requests
    assert first["messages"] == [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Use the available tools to find a chat"},
    ]
    assert second["tools"] == [LOOKUP_CHAT_SCHEMA]
    assert second["messages"] == [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Use the available tools to find a chat"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_tool_call("call_1", "lookup_chat", '{"name": "Design"}')],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "chat-id-for-Design"},
    ]


def test_chain_returns_tool_exceptions_to_model(monkeypatch):
    api = FakeFoundry(monkeypatch)

    def broken_tool(name: str) -> str:
        "Always fails."
        raise RuntimeError("lookup unavailable")

    api.reply_tool_calls([("call_err", "broken_tool", {"name": "x"})], stream=False)
    api.reply_text("The lookup failed.", stream=False)

    chain = _model().chain("Try it", tools=[broken_tool], stream=False)

    assert chain.text() == "The lookup failed."
    assert api.requests[1]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call_err",
        "content": "Error: lookup unavailable",
    }


def test_chain_with_multiple_parallel_tool_calls(monkeypatch):
    api = FakeFoundry(monkeypatch)
    api.reply_tool_calls(
        [
            ("call_a", "lookup_chat", {"name": "A"}),
            ("call_b", "count_letters", {"word": "aa", "letter": "a"}),
        ],
        stream=True,
    )
    api.reply_text("Done.", stream=True)

    chain = _model().chain("Do both", tools=[lookup_chat, count_letters], stream=True)

    assert chain.text() == "Done."
    assert api.requests[1]["messages"][1:] == [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                _tool_call("call_a", "lookup_chat", '{"name": "A"}'),
                _tool_call("call_b", "count_letters", '{"word": "aa", "letter": "a"}'),
            ],
        },
        {"role": "tool", "tool_call_id": "call_a", "content": "chat-id-for-A"},
        {"role": "tool", "tool_call_id": "call_b", "content": "2"},
    ]


# ---------------------------------------------------------------------------
# 6. Conversation history across later turns
# ---------------------------------------------------------------------------


def test_conversation_history_preserves_tool_turns(monkeypatch):
    api = FakeFoundry(monkeypatch)
    api.reply(
        {
            "content": "Looking it up.",
            "tool_calls": [_tool_call("call_1", "lookup_chat", '{"name": "Ops"}')],
        },
        finish_reason="tool_calls",
    )
    api.reply_text("Ops is chat-id-for-Ops.", stream=False)
    api.reply_text("You asked about Ops.", stream=False)

    conversation = _model().conversation(tools=[lookup_chat])
    first = conversation.chain("Find Ops", system="Be brief.", stream=False)
    assert first.text() == "Looking it up. Ops is chat-id-for-Ops."
    second = conversation.chain("What did I ask?", stream=False)
    assert second.text() == "You asked about Ops."

    assert api.requests[2]["messages"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Find Ops"},
        {
            "role": "assistant",
            "content": "Looking it up.",
            "tool_calls": [_tool_call("call_1", "lookup_chat", '{"name": "Ops"}')],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "chat-id-for-Ops"},
        {"role": "assistant", "content": "Ops is chat-id-for-Ops."},
        {"role": "user", "content": "What did I ask?"},
    ]


def test_cli_tool_loop_and_continuation(monkeypatch, tmp_path):
    from llm.cli import cli

    monkeypatch.setenv("LLM_USER_PATH", str(tmp_path))
    (tmp_path / "foundry_models.yaml").write_text(
        "models:\n"
        "- model_id: foundry-cli-test\n"
        f"  api_base: {API_BASE}\n"
        "  deployment: test-deployment\n"
        "  api_type: chat\n"
        "  supports_tools: true\n"
    )
    api = FakeFoundry(monkeypatch)
    api.reply_tool_calls([("call_1", "lookup_chat", {"name": "Ops"})], stream=True)
    api.reply_text("Found chat-id-for-Ops.", stream=True)
    api.reply_text("It was Ops.", stream=True)
    functions = (
        "def lookup_chat(name: str) -> str:\n"
        '    "Find a chat by its display name."\n'
        '    return f"chat-id-for-{name}"\n'
    )
    runner = CliRunner()

    first = runner.invoke(
        cli,
        ["-m", "foundry-cli-test", "Find Ops", "--functions", functions, "--td"],
        catch_exceptions=False,
    )
    second = runner.invoke(
        cli,
        ["-c", "Which chat?", "--functions", functions],
        catch_exceptions=False,
    )

    assert first.exit_code == 0, first.output
    assert "lookup_chat" in first.output
    assert "Found chat-id-for-Ops." in first.output
    assert second.exit_code == 0, second.output
    assert "It was Ops." in second.output
    assert api.requests[2]["messages"] == [
        {"role": "user", "content": "Find Ops"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [_tool_call("call_1", "lookup_chat", '{"name": "Ops"}')],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "chat-id-for-Ops"},
        {"role": "assistant", "content": "Found chat-id-for-Ops."},
        {"role": "user", "content": "Which chat?"},
    ]


# ---------------------------------------------------------------------------
# 7. Malformed arguments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("arguments", ['{"name": "Ops"', '["not", "an", "object"]'])
def test_non_streaming_malformed_arguments_raise(monkeypatch, arguments):
    api = FakeFoundry(monkeypatch)
    api.reply(
        {
            "content": None,
            "tool_calls": [_tool_call("call_bad", "lookup_chat", arguments)],
        },
        finish_reason="tool_calls",
    )

    response = _model().prompt("Find Ops", tools=[lookup_chat], stream=False)

    with pytest.raises(click.ClickException) as excinfo:
        response.text()
    message = excinfo.value.message
    assert "lookup_chat" in message
    assert "call_bad" in message
    assert arguments in message


def test_streaming_malformed_arguments_raise(monkeypatch):
    api = FakeFoundry(monkeypatch)
    api.reply_stream(
        [
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_bad",
                        "type": "function",
                        "function": {"name": "lookup_chat", "arguments": '{"name": '},
                    }
                ]
            }
        ]
    )

    response = _model().prompt("Find Ops", tools=[lookup_chat], stream=True)

    with pytest.raises(click.ClickException, match="call_bad"):
        response.text()


def test_tool_call_without_name_raises(monkeypatch):
    api = FakeFoundry(monkeypatch)
    api.reply_stream(
        [
            {
                "tool_calls": [
                    {"index": 0, "id": "call_x", "function": {"arguments": "{}"}}
                ]
            }
        ]
    )

    response = _model().prompt("Find Ops", tools=[lookup_chat], stream=True)

    with pytest.raises(click.ClickException, match="call_x"):
        response.text()


# ---------------------------------------------------------------------------
# 8. Ordinary chat and decision-model restrictions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stream", [False, True])
def test_ordinary_chat_has_no_tool_fields(monkeypatch, stream):
    api = FakeFoundry(monkeypatch)
    api.reply_text("Paris.", stream)
    api.reply_text("About 2 million.", stream)

    model = _model(supports_tools=False)
    conversation = model.conversation()
    first = conversation.prompt(
        "Capital of France?",
        system="Answer briefly.",
        stream=stream,
        temperature=0.2,
        top_p=0.9,
        max_tokens=50,
    )
    assert first.text() == "Paris."
    second = conversation.prompt("Population?", stream=stream)
    assert second.text() == "About 2 million."

    first_body, second_body = api.requests
    assert first_body == {
        "model": "test-deployment",
        "messages": [
            {"role": "system", "content": "Answer briefly."},
            {"role": "user", "content": "Capital of France?"},
        ],
        "stream": stream,
        "temperature": 0.2,
        "max_tokens": 50,
        "top_p": 0.9,
    }
    assert "tools" not in second_body
    assert "tool_choice" not in second_body
    assert second_body["messages"] == [
        {"role": "system", "content": "Answer briefly."},
        {"role": "user", "content": "Capital of France?"},
        {"role": "assistant", "content": "Paris."},
        {"role": "user", "content": "Population?"},
    ]


def test_image_attachments_are_preserved(monkeypatch):
    api = FakeFoundry(monkeypatch)
    api.reply_text("A cat.", stream=False)

    response = _model(supports_vision=True).prompt(
        "What is this?",
        attachments=[
            llm.Attachment(url="https://example.com/cat.png", type="image/png")
        ],
        stream=False,
    )

    assert response.text() == "A cat."
    assert api.requests[0]["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What is this?"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.com/cat.png"},
                },
            ],
        }
    ]


def test_tools_rejected_when_capability_not_enabled():
    with pytest.raises(ValueError, match="does not support tools"):
        _model(supports_tools=False).prompt("Hi", tools=[lookup_chat])


def test_decision_models_still_reject_tools():
    model = foundry.FoundryDecisionModel(
        model_id="foundry-decision",
        deployment="decision-deployment",
        api_base=API_BASE,
    )
    assert model.supports_tools is False
    with pytest.raises(ValueError, match="does not support tools"):
        model.prompt("{}", tools=[lookup_chat])


def test_register_models_keeps_tools_opt_in(monkeypatch):
    monkeypatch.setattr(
        foundry,
        "_load_config",
        lambda: {
            "models": [
                {"model_id": "plain", "api_base": API_BASE},
                {"model_id": "tooling", "api_base": API_BASE, "supports_tools": True},
            ]
        },
    )
    registered = []

    foundry.register_models(lambda model, aliases=(): registered.append(model))

    assert [m.supports_tools for m in registered] == [False, True]


# ---------------------------------------------------------------------------
# 9. max_tokens retry keeps tool definitions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stream", [False, True])
def test_max_tokens_retry_preserves_tools(monkeypatch, stream):
    api = FakeFoundry(monkeypatch)
    api.reply_error(
        400,
        {
            "error": {
                "param": "max_tokens",
                "code": "unsupported_parameter",
                "message": "Use 'max_completion_tokens' instead.",
            }
        },
    )
    api.reply_tool_calls([("call_1", "lookup_chat", {"name": "Ops"})], stream)

    response = _model().prompt(
        "Find Ops", tools=[lookup_chat], stream=stream, max_tokens=100
    )

    assert response.tool_calls() == [
        llm.ToolCall(
            name="lookup_chat", arguments={"name": "Ops"}, tool_call_id="call_1"
        )
    ]
    first, retry = api.requests
    assert first["max_tokens"] == 100
    assert "max_completion_tokens" not in first
    assert retry["max_completion_tokens"] == 100
    assert "max_tokens" not in retry
    assert first["tools"] == retry["tools"] == [LOOKUP_CHAT_SCHEMA]
    assert first["messages"] == retry["messages"]
