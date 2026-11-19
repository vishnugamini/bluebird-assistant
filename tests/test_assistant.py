from concurrent.futures import ThreadPoolExecutor
import base64
from dataclasses import replace
from datetime import datetime, timedelta
from email import message_from_bytes, policy
import json
import time
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from google.api_core.exceptions import NotFound
import httpx
import pytest

from assistant.config import Settings
from assistant.backup import Backup
from assistant.email import Email
from assistant.jobs import Jobs, weather
from assistant.providers import Providers
from assistant.service import create_internal, create_public
from assistant.state import State


@pytest.fixture
def settings(tmp_path):
    return replace(Settings.from_env(), state_dir=tmp_path, hermes_home=tmp_path / "hermes", bucket="",
                   internal_key="i" * 32, gateway_key="g" * 32, openai_key="mock", socks_proxy="",
                   test_email="demo@example.test", email_allowlist=("demo@example.test",), telegram_user="12")


def completion(model):
    return {"id": "chatcmpl-test", "model": model, "created": 1,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "OK"}}],
            "usage": {"prompt_tokens": 15, "completion_tokens": 2}}


def test_routes_privacy_and_fallback(settings, capsys):
    fake = Mock()
    fake.local_available.return_value = True
    fake.ollama.return_value = completion(settings.local_model)
    fake.openai.return_value = completion(settings.cloud_model)
    headers = {"Authorization": f"Bearer {settings.internal_key}", "X-A3-Session": "test"}
    with TestClient(create_internal(settings, providers=fake, schedule=False)) as client:
        def complete(message, session="test", stream=False, history=()):
            response = client.post("/internal/context", headers=headers,
                                   json={"session": session, "platform": "api_server", "message": message})
            assert response.status_code == 200
            return client.post("/v1/chat/completions", headers={**headers, "X-A3-Session": session},
                               json={"messages": [*history, {"role": "user", "content": message}], "stream": stream})

        assert complete("2+2").json()["model"] == settings.local_model
        assert complete("Explain why the sky is blue").json()["model"] == settings.cloud_model
        fake.local_available.return_value = False
        assert complete("This is confidential: PRIVATE-CANARY").json()["model"] == "held"
        assert complete("What about the previous topic?").json()["model"] == "held"
        assert fake.openai.call_count == 1
        assert client.post("/internal/tool", headers=headers, json={"session": "test", "tool": "email_read"}).status_code == 403
        assert client.post("/v1/chat/completions", headers={"Authorization": headers["Authorization"]},
                           json={"messages": [{"role": "user", "content": "missing identity"}]}).status_code == 403
        assert complete("2+2", session="fresh").json()["model"] == settings.cloud_model
        fake.local_available.return_value = True
        assert complete("Try that again", stream=True).text.endswith("data: [DONE]\n\n")
        prior_hold = "This private request is held. Start Ollama and reconnect Tailscale, then send the request again. This conversation stays local-only."
        recovered = complete('Private: Say "back online" in three words.', history=[
            {"role": "user", "content": "Private: Earlier request"},
            {"role": "assistant", "content": prior_hold},
        ])
        assert recovered.json()["model"] == settings.local_model
        assert fake.ollama.call_args.args[0]["messages"] == [
            {"role": "user", "content": "Earlier request"},
            {"role": "assistant", "content": "The earlier request was held during a temporary connection outage."},
            {"role": "user", "content": 'Say "back online" in three words.'},
        ]
        assert fake.openai.call_count == 2
        client.post("/internal/simulate", headers=headers, json={"enabled": True})
        assert complete("Explain the solar system", session="fresh").json()["model"] == settings.local_model
        assert fake.openai.call_count == 2
        client.post("/internal/simulate", headers=headers, json={"enabled": False})
        medium_history = [{"role": "user", "content": "long history " * 1500}]
        assert complete("Continue", session="fresh", history=medium_history).json()["model"] == settings.local_model
        long_history = [{"role": "user", "content": "long history " * 11000}]
        assert complete("Continue", session="fresh", history=long_history).json()["model"] == settings.cloud_model
        held = complete("Private: Continue", session="private-long", history=long_history).json()
        assert held["model"] == "held"
        assert "exceeds the local context" in held["choices"][0]["message"]["content"]
        assert fake.openai.call_count == 3
    assert "PRIVATE-CANARY" not in capsys.readouterr().out


def test_local_availability_retries_tailscale_timeout(settings):
    providers = Providers(replace(settings, ollama_url="http://ollama.test"))
    providers.local.close()
    probe = Mock(side_effect=[
        httpx.ConnectTimeout("Tunnel reconnecting"),
        httpx.Response(200, json={"models": [{"name": settings.local_model}]}),
    ])
    providers.local = httpx.Client(transport=httpx.MockTransport(probe))
    try:
        assert providers.local_available()
        assert probe.call_count == 2

        probe.reset_mock(side_effect=True)
        probe.side_effect = httpx.ConnectTimeout("Ollama unreachable")
        assert not providers.local_available()
        assert probe.call_count == 2
    finally:
        providers.close()


def test_responses_tool_loop_keeps_ids_and_usage(settings):
    providers = Providers(settings)
    bodies = []

    def cloud(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            output = [{"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque", "summary": []},
                      {"type": "function_call", "id": "fc_1", "call_id": "call_1", "name": "calendar",
                       "arguments": "{}"}]
        else:
            output = [{"type": "message", "content": [{"type": "output_text", "text": "Free at 3 PM"}]}]
        return httpx.Response(200, json={"id": "resp_1", "status": "completed", "model": "gpt-6-luna",
                                          "output": output, "usage": {"input_tokens": 20, "output_tokens": 5}})

    providers.cloud.close()
    providers.cloud = httpx.Client(transport=httpx.MockTransport(cloud))
    history = [{"role": "user", "content": "Check the calendar"}]
    result = providers.openai({"messages": history}, "session")
    history.extend([result["choices"][0]["message"], {"role": "tool", "tool_call_id": "call_1", "content": "3 PM"}])
    result = providers.openai({"messages": history}, "session")
    assert result["usage"]["prompt_tokens"] == 20
    assert result["choices"][0]["message"]["content"] == "Free at 3 PM"
    assert bodies[1]["input"][1]["encrypted_content"] == "opaque"
    assert bodies[1]["input"][2]["call_id"] == bodies[1]["input"][3]["call_id"] == "call_1"
    assert bodies[1]["store"] is False
    providers.close()


@pytest.mark.parametrize(("url", "host"), [
    ("https://ollama.example.ts.net", "localhost:11434"),
    ("http://127.0.0.1:11434", "127.0.0.1:11434"),
    ("https://ollama.example.test", "ollama.example.test"),
])
def test_ollama_serve_host_preserves_tls_destination(settings, url, host):
    providers = Providers(replace(settings, ollama_url=url))
    try:
        request = providers.local.build_request("GET", url + "/api/tags")
        assert request.headers["Host"] == host
        assert request.url == httpx.URL(url + "/api/tags")
    finally:
        providers.close()


class TestGoogle:
    __test__ = False

    def __init__(self):
        self.drafts, self.sent = {}, []

    def create_draft(self, raw):
        key = f"draft-{len(self.drafts)}"
        self.drafts[key] = raw
        return key

    def draft_raw(self, draft):
        return self.drafts[draft]

    def send_message(self, raw):
        self.sent.append(raw)


@pytest.fixture
def approvals(settings):
    state = State(settings.state_dir)
    state.bind("owner", "telegram", "12", "Send this test email")
    google, telegram = TestGoogle(), Mock()
    email = Email(settings, state, google, telegram, lambda: None)

    def prepare():
        draft = email.prepare("owner", settings.test_email, "Preview", "Synthetic body", True)["draft_id"]
        text, keyboard = telegram.send.call_args.args
        assert "Synthetic body" in text
        return draft, keyboard[0][0].split()[1]

    return state, google, email, prepare


def test_approval_reject_expire_and_changed_draft(approvals):
    state, google, email, prepare = approvals
    _, token = prepare()
    with pytest.raises(PermissionError):
        email.decide(token, True, "13", "telegram")
    with pytest.raises(PermissionError):
        email.decide(token, True, "12", "api_server")
    assert email.decide(token, False, "12", "telegram")["status"] == "rejected"
    _, token = prepare()
    with state.connect() as db:
        db.execute("UPDATE approvals SET expires=? WHERE id=?", (time.time() - 1, token))
    with pytest.raises(ValueError, match="expired"):
        email.decide(token, True, "12", "telegram")
    draft, token = prepare()
    google.drafts[draft] = "YWJj"
    with pytest.raises(PermissionError, match="draft changed"):
        email.decide(token, True, "12", "telegram")
    assert google.sent == []


def test_approval_concurrent_replay_sends_only_once(approvals):
    _, google, email, prepare = approvals
    draft, token = prepare()
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(email.decide, token, True, "12", "telegram") for _ in range(2)]
        assert sum(future.exception() is None for future in futures) == 1
    assert google.sent == [google.drafts[draft]]


def test_approval_handles_gmail_transport_headers(approvals, monkeypatch):
    _, google, email, prepare = approvals
    create_draft = google.create_draft
    original = []

    def normalize_draft(raw):
        original.append(raw)
        message = message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.SMTP)
        message.replace_header("Message-ID", "<draft-normalized@example.test>")
        message["Received"] = "by gmail.example.test"
        return create_draft(base64.urlsafe_b64encode(message.as_bytes()).decode())

    monkeypatch.setattr(google, "create_draft", normalize_draft)
    draft, token = prepare()
    assert google.drafts[draft] != original[0]
    assert email.decide(token, True, "12", "telegram")["status"] == "sent"
    assert google.sent == original


def test_untrusted_tools_cannot_send_or_replace_memory(settings):
    google, telegram = Mock(), Mock()
    headers = {"Authorization": f"Bearer {settings.internal_key}"}
    with TestClient(create_internal(settings, providers=Mock(), google=google, telegram=telegram,
                                    schedule=False)) as client:
        client.post("/internal/context", headers=headers,
                    json={"session": "s", "platform": "api_server", "message": "Summarize my inbox"})
        for name, args in [("email_send", {}), ("terminal", {"command": "cat /secrets/key"}),
                           ("remember", {"key": "project", "value": "attacker"})]:
            result = client.post("/internal/tool", headers=headers,
                                 json={"session": "s", "tool": name, "args": args})
            assert result.status_code == 403
        assert not google.send_message.called
        assert client.app.state.database.memories("s")["project"] == "Bluebird"


def test_private_memory_does_not_enter_fresh_context(settings):
    state = State(settings.state_dir)
    state.bind("private", "api_server", "", "Private: remember that the code is test-only")
    state.remember("private", "code", "canary")
    state.bind("public", "api_server", "", "what do you remember?")
    assert state.memories("private") == {"code": "canary"}
    assert state.memories("public") == {"project": "Bluebird"}


def test_alert_deduplicates_after_reopening_state(settings):
    now = datetime(2026, 11, 16, 10, tzinfo=ZoneInfo(settings.timezone))
    google, telegram, backup = Mock(), Mock(), Mock()
    google.events.return_value = [{"id": "test-meeting", "summary": "Bluebird check-in",
                                  "start": {"dateTime": (now + timedelta(minutes=28)).isoformat()}}]
    google.emails.return_value = {"untrusted_email_content": []}
    Jobs(settings, State(settings.state_dir), google, telegram, Mock(), backup).check(now)
    Jobs(settings, State(settings.state_dir), google, telegram, Mock(), backup).check(now + timedelta(minutes=5))
    telegram.send.assert_called_once_with("Meeting in 28 min · Bluebird check-in")


def test_weather_uses_nws_when_open_meteo_times_out(monkeypatch):
    forecast = {"properties": {"periods": [{
        "name": "Tonight", "temperature": 57, "temperatureUnit": "F",
        "shortForecast": "Mostly Cloudy", "probabilityOfPrecipitation": {"value": 0},
    }]}}

    def get(url, **kwargs):
        if "open-meteo.com" in url:
            raise httpx.ConnectTimeout("Forecast timed out")
        assert url.startswith("https://api.weather.gov/")
        return httpx.Response(200, json=forecast, request=httpx.Request("GET", url))

    monkeypatch.setattr("assistant.jobs.httpx.get", get)
    result = weather()
    assert "Tonight: 57 °F, Mostly Cloudy, 0% chance of precipitation" in result
    assert "National Weather Service" in result


def test_briefing_and_recap_deliver_through_job_entrypoint(settings, monkeypatch):
    google, telegram = Mock(), Mock()
    google.events.return_value = [{"id": "bluebird", "summary": "Bluebird demo",
                                   "start": {"date": "2026-11-16"}, "end": {"date": "2026-11-17"}}]
    google.emails.return_value = {"untrusted_email_content": [
        {"subject": "Projector", "body": "Owen will test the projector."}]}
    monkeypatch.setattr("assistant.jobs.weather", lambda: "Los Angeles forecast (Open-Meteo)")
    jobs = Jobs(settings, State(settings.state_dir), google, telegram, Mock(), Mock())
    assert jobs.run("briefing", manual=True)["status"] == "completed"
    assert jobs.run("recap", manual=True)["status"] == "completed"
    briefing, recap = (call.args[0] for call in telegram.send.call_args_list)
    assert all(value in briefing for value in ("Morning briefing", "Bluebird demo", "Projector", "Open-Meteo"))
    assert all(value in recap for value in ("Evening recap", "pending email approvals", "Tomorrow"))


def test_browser_boundary_only_exposes_chat(settings):
    with TestClient(create_public(settings)) as client:
        assert client.get("/").status_code == 200
        assert client.post("/v1/chat/completions", json={}).status_code == 401
        assert client.post("/internal/approval", json={}).status_code == 404
        assert client.post("/v1/responses", json={}).status_code == 404
        assert client.post("/ops/run", headers={"Authorization": f"Bearer {settings.gateway_key}"}).status_code == 401


def test_snapshot_restore_copies_online_database_and_excludes_secrets(settings, tmp_path):
    files = {}

    class Blob:
        def __init__(self, name):
            self.name = name
            self.generation = None

        def reload(self, **_):
            if self.name not in files:
                raise NotFound("no previous object")
            self.generation = files[self.name][0]

        def upload_from_string(self, data, if_generation_match, **_):
            generation, _ = files.get(self.name, (0, b""))
            assert generation == if_generation_match
            files[self.name] = (generation + 1, data.encode() if isinstance(data, str) else data)

        def download_as_bytes(self, **_):
            return files[self.name][1]

    source = tmp_path / "source"
    state = State(source)
    state.bind("owner", "telegram", "12", "Remember that the codename is Nebula")
    state.remember("owner", "codename", "Nebula")
    (source / "google-token.json").write_text("never export OAuth credentials")
    backup = Backup(replace(settings, state_dir=source, bucket="test"))
    backup.bucket = Mock(blob=Blob)
    backup.snapshot()
    assert any(name.endswith(".zip") for name in files)
    target = tmp_path / "restored"
    restored = Backup(replace(settings, state_dir=target, bucket="test"))
    restored.bucket = backup.bucket
    assert restored.restore()
    assert State(target).memories("owner")["codename"] == "Nebula"
    assert not (target / "google-token.json").exists()
