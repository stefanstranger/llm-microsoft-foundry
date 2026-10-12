import json
import time
from typing import ClassVar

import click
import httpx
from click.testing import CliRunner

import llm_microsoft_foundry as foundry


class FakeClient:
    requests: ClassVar[list[dict]] = []
    response_json: ClassVar[object] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def post(self, url, **kwargs):
        self.requests.append({"url": url, **kwargs})
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json=self.response_json,
        )


def test_register_models_defaults_existing_config_to_chat(monkeypatch):
    monkeypatch.setattr(
        foundry,
        "_load_config",
        lambda: {
            "models": [
                {
                    "model_id": "existing-model",
                    "api_base": "https://example.services.ai.azure.com",
                    "deployment": "existing-deployment",
                },
                {
                    "model_id": "decision-model",
                    "api_base": "https://example.services.ai.azure.com",
                    "deployment": "decision-deployment",
                    "api_type": "decision",
                },
            ]
        },
    )
    registered = []

    foundry.register_models(
        lambda model, aliases=(): registered.append((model, aliases))
    )

    assert isinstance(registered[0][0], foundry.FoundryModel)
    assert isinstance(registered[1][0], foundry.FoundryDecisionModel)


def test_decision_request_with_state_and_questions(monkeypatch):
    FakeClient.requests = []
    requested_scopes = []
    FakeClient.response_json = {
        "model": "microsoft-decision-1",
        "answers": {
            "refund": {
                "type": "noul",
                "noul": 0.97,
            }
        },
        "usage": {"input_tokens": 42, "output_tokens": 0},
    }
    monkeypatch.setattr(foundry.httpx, "Client", FakeClient)
    monkeypatch.setattr(
        foundry,
        "_get_access_token",
        lambda scope: requested_scopes.append(scope) or "token",
    )
    model = foundry.FoundryDecisionModel(
        model_id="foundry-decision",
        deployment="decision-deployment",
        api_base="https://example.services.ai.azure.com/",
    )
    assert model.can_stream is False

    response = model.prompt(
        "Please refund the duplicate charge.",
        questions={
            "refund": {
                "type": "noul",
                "instructions": "Is a refund requested?",
            }
        },
    )

    assert json.loads(response.text())["answers"]["refund"]["noul"] == 0.97
    assert FakeClient.requests[0]["url"] == (
        "https://example.services.ai.azure.com/providers/microsoft/v1/systemone"
    )
    assert FakeClient.requests[0]["json"] == {
        "model": "decision-deployment",
        "state": "Please refund the duplicate charge.",
        "questions": {
            "refund": {
                "type": "noul",
                "instructions": "Is a refund requested?",
            }
        },
    }
    assert response.response_json == FakeClient.response_json
    assert response.usage().input == 42
    assert response.usage().output == 0
    assert requested_scopes == [foundry._FOUNDRY_DECISION_SCOPE]


def test_decision_request_accepts_complete_json_and_enforces_deployment(
    monkeypatch,
):
    FakeClient.requests = []
    FakeClient.response_json = {"answers": {}, "usage": {}}
    monkeypatch.setattr(foundry.httpx, "Client", FakeClient)
    monkeypatch.setattr(foundry, "_get_access_token", lambda *_: "token")
    model = foundry.FoundryDecisionModel(
        model_id="foundry-decision",
        deployment="configured-deployment",
        api_base="https://example.services.ai.azure.com",
    )
    payload = {
        "model": "untrusted-deployment",
        "state": {"ticket": "Duplicate charge"},
        "questions": {
            "team": {
                "type": "choice",
                "instructions": "Which team?",
                "criteria": {
                    "billing": "Payment problems",
                    "support": "Everything else",
                },
            }
        },
    }

    response = model.prompt(json.dumps(payload))

    assert json.loads(response.text())["answers"] == {}
    assert FakeClient.requests[0]["json"] == {
        **payload,
        "model": "configured-deployment",
    }


def test_decision_request_rejects_invalid_payload(monkeypatch):
    monkeypatch.setattr(foundry, "_get_access_token", lambda *_: "token")
    model = foundry.FoundryDecisionModel(
        model_id="foundry-decision",
        deployment="decision-deployment",
        api_base="https://example.services.ai.azure.com",
    )

    response = model.prompt('{"state": "missing questions"}')

    try:
        response.text()
    except ValueError as exc:
        assert str(exc) == ("Decision API request questions must be a JSON object")
    else:
        raise AssertionError("Expected invalid decision payload to fail")


def test_access_token_cache_is_scoped(monkeypatch):
    calls = []

    class Token:
        token = "token"
        expires_on = time.time() + 3600

    class Credential:
        def get_token(self, scope):
            calls.append(scope)
            return Token()

    monkeypatch.setattr("azure.identity.DefaultAzureCredential", lambda: Credential())
    foundry._token_cache.clear()

    foundry._get_access_token(foundry._FOUNDRY_CHAT_SCOPE)
    foundry._get_access_token(foundry._FOUNDRY_CHAT_SCOPE)
    foundry._get_access_token(foundry._FOUNDRY_DECISION_SCOPE)

    assert calls == [
        foundry._FOUNDRY_CHAT_SCOPE,
        foundry._FOUNDRY_DECISION_SCOPE,
    ]


def test_add_decision_model_configuration(monkeypatch, tmp_path):
    monkeypatch.setattr(foundry.llm, "user_dir", lambda: tmp_path)
    cli = click.Group()
    foundry.register_commands(cli)
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "foundry",
            "add-model",
            "--model-id",
            "foundry-decision",
            "--api-base",
            "https://example.services.ai.azure.com/",
            "--deployment",
            "decision-deployment",
            "--api-type",
            "decision",
            "--alias",
            "decision",
        ],
    )

    assert result.exit_code == 0
    assert foundry._load_config() == {
        "models": [
            {
                "model_id": "foundry-decision",
                "api_base": "https://example.services.ai.azure.com",
                "deployment": "decision-deployment",
                "api_type": "decision",
                "supports_tools": False,
                "supports_vision": False,
                "aliases": ["decision"],
            }
        ]
    }


def test_add_decision_model_rejects_chat_capabilities(monkeypatch, tmp_path):
    monkeypatch.setattr(foundry.llm, "user_dir", lambda: tmp_path)
    cli = click.Group()
    foundry.register_commands(cli)
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "foundry",
            "add-model",
            "--model-id",
            "foundry-decision",
            "--api-base",
            "https://example.services.ai.azure.com",
            "--api-type",
            "decision",
            "--tools",
        ],
    )

    assert result.exit_code == 2
    assert "Decision models do not support --tools or --vision." in result.output
