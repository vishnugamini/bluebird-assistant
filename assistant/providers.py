"""Chat-completion boundary for Hermes with an OpenAI Responses cloud adapter.

Protocol mappings follow https://developers.openai.com/api/docs/guides/function-calling
and https://developers.openai.com/api/docs/guides/reasoning .
"""
import json
import threading
import time
import uuid

import httpx

from assistant.config import LOCAL_MAX_OUTPUT_TOKENS


def local_output_tokens(payload):
    return min(payload.get("max_tokens") or LOCAL_MAX_OUTPUT_TOKENS, LOCAL_MAX_OUTPUT_TOKENS)


class ProviderFailure(Exception):
    def __init__(self, code, retryable=False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def check_status(response):
    if response.status_code >= 400:
        raise ProviderFailure(f"provider_http_{response.status_code}",
                              response.status_code in (408, 429, 500, 502, 503, 504))


def checked(response):
    check_status(response)
    body = response.json()
    if body.get("error"):
        raise ProviderFailure("provider_response_error")
    return body


def stream_events(response):
    check_status(response)
    for line in response.iter_lines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        if data:
            try:
                item = json.loads(data)
            except ValueError as exc:
                raise ProviderFailure("invalid_provider_stream") from exc
            if item.get("error") or item.get("type") in {"response.failed", "response.incomplete", "error"}:
                raise ProviderFailure("provider_stream_failed")
            yield item


def ollama_stream(response, on_delta, model):
    message = {"role": "assistant", "content": ""}
    calls, usage, finish = {}, {}, None
    identifier = f"chatcmpl-{uuid.uuid4().hex}"
    for event in stream_events(response):
        identifier = event.get("id", identifier)
        model = event.get("model") or model
        usage = event.get("usage") or usage
        for choice in event.get("choices", []):
            delta = choice.get("delta", {})
            if delta.get("content"):
                message["content"] += delta["content"]
                on_delta(delta["content"], model)
            for part in delta.get("tool_calls", []):
                call = calls.setdefault(part.get("index", 0), {"id": "", "type": "function",
                                                               "function": {"name": "", "arguments": ""}})
                if part.get("id"):
                    call["id"] = part["id"]
                for key in ("name", "arguments"):
                    call["function"][key] += part.get("function", {}).get(key) or ""
            finish = choice.get("finish_reason") or finish
    if not finish:
        raise ProviderFailure("local_incomplete_stream")
    if calls:
        message["tool_calls"] = [calls[index] for index in sorted(calls)]
    return {"id": identifier, "object": "chat.completion", "model": model, "created": int(time.time()),
            "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage}


class Providers:
    def __init__(self, settings):
        self.settings = settings
        self.cloud = httpx.Client(timeout=httpx.Timeout(180, connect=10), follow_redirects=False)
        ollama_url = httpx.URL(settings.ollama_url)
        # Serve selects its HTTPS backend via TLS SNI and forwards the HTTP Host.
        # Loopback-bound Ollama accepts a loopback Host.
        headers = None
        if ollama_url.scheme == "https" and ollama_url.host.endswith(".ts.net"):
            headers = {"Host": "localhost:11434"}
        self.local = httpx.Client(proxy=settings.socks_proxy or None,
                                 headers=headers,
                                 timeout=httpx.Timeout(240, connect=3), follow_redirects=False)
        self.reasoning = {}
        self.lock = threading.RLock()

    def close(self):
        self.cloud.close()
        self.local.close()

    def local_available(self):
        if not self.settings.ollama_url:
            return False
        url = self.settings.ollama_url.rstrip("/") + "/api/tags"
        # A brief Tailscale reconnect can outlast the first health request.
        for _ in range(2):
            try:
                models = checked(self.local.get(url, timeout=3)).get("models", [])
                return any(m.get("name") == self.settings.local_model for m in models)
            except httpx.TransportError:
                continue
            except (httpx.HTTPError, ValueError, ProviderFailure):
                return False
        return False

    def ollama(self, payload, on_delta=None):
        fields = {key: payload[key] for key in ("messages", "tools", "tool_choice") if key in payload}
        fields.update(model=self.settings.local_model, stream=False, reasoning_effort="none",
                      temperature=0, max_tokens=local_output_tokens(payload))
        try:
            url = self.settings.ollama_url.rstrip("/") + "/v1/chat/completions"
            if on_delta:
                fields.update(stream=True, stream_options={"include_usage": True})
                with self.local.stream("POST", url, json=fields) as response:
                    check_status(response)
                    if "text/event-stream" in response.headers.get("content-type", ""):
                        result = ollama_stream(response, on_delta, self.settings.local_model)
                    else:
                        response.read()
                        result = checked(response)
            else:
                result = checked(self.local.post(url, json=fields))
        except httpx.HTTPError as exc:
            raise ProviderFailure("local_connection_failed", True) from exc
        if not result.get("choices") or not result["choices"][0].get("message"):
            raise ProviderFailure("local_invalid_response")
        message = result["choices"][0]["message"]
        if not message.get("content") and not message.get("tool_calls"):
            raise ProviderFailure("local_empty_response")
        result["model"] = result.get("model", self.settings.local_model)
        return result

    def responses_input(self, messages, session):
        result = []
        for message in messages:
            role = message["role"]
            if role == "tool":
                result.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                               "output": message.get("content") or ""})
                continue
            content = message.get("content")
            if isinstance(content, list):
                # This project accepts text only. Refuse unhandled content instead of silently dropping it.
                if any(block.get("type") != "text" for block in content):
                    raise ValueError("The assistant currently accepts text messages.")
                content = "\n".join(block.get("text", "") for block in content)
            if content:
                result.append({"role": role, "content": content})
            calls = message.get("tool_calls", [])
            reasoning = self.reasoning.get((session, calls[0]["id"])) if calls else None
            if reasoning:
                result.extend(reasoning)
            for call in calls:
                result.append({"type": "function_call", "call_id": call["id"],
                               "name": call["function"]["name"],
                               "arguments": call["function"]["arguments"]})
        return result

    def openai(self, payload, session, on_delta=None):
        if not self.settings.openai_key:
            raise ProviderFailure("openai_key_missing")
        with self.lock:
            items = self.responses_input(payload["messages"], session)
        tools = [{"type": "function", **tool["function"]} for tool in payload.get("tools", [])]
        body = {"model": self.settings.cloud_model, "input": items, "store": False,
                "include": ["reasoning.encrypted_content"], "reasoning": {"effort": "medium"},
                "max_output_tokens": min(payload.get("max_tokens", 4096), 8192)}
        if tools:
            body["tools"] = tools
            if "tool_choice" in payload:
                choice = payload["tool_choice"]
                body["tool_choice"] = ({"type": "function", "name": choice["function"]["name"]}
                                       if isinstance(choice, dict) else choice)
            body["parallel_tool_calls"] = False
        try:
            response = self._cloud_response(body, on_delta)
        except httpx.HTTPError as exc:
            raise ProviderFailure("cloud_connection_failed", True) from exc
        if response.get("status") != "completed":
            raise ProviderFailure("cloud_incomplete_response")
        content, calls, reasoning = [], [], []
        for item in response.get("output", []):
            if item["type"] == "reasoning":
                reasoning.append(item)
            elif item["type"] == "function_call":
                calls.append({"id": item["call_id"], "type": "function",
                              "function": {"name": item["name"], "arguments": item["arguments"]}})
            elif item["type"] == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        content.append(part["text"])
                    elif part.get("type") == "refusal":
                        content.append(part["refusal"])
        if not content and not calls:
            raise ProviderFailure("cloud_empty_response")
        if calls and reasoning:
            with self.lock:
                if len(self.reasoning) > 2000:
                    self.reasoning.clear()
                self.reasoning[(session, calls[0]["id"])] = reasoning
        message = {"role": "assistant", "content": "\n".join(content) or None}
        if calls:
            message["tool_calls"] = calls
        usage = response.get("usage", {})
        return {"id": response.get("id", f"chatcmpl-{uuid.uuid4().hex}"),
                "object": "chat.completion", "created": int(time.time()),
                "model": response.get("model", self.settings.cloud_model),
                "choices": [{"index": 0, "message": message,
                             "finish_reason": "tool_calls" if calls else "stop"}],
                "usage": {"prompt_tokens": usage.get("input_tokens", 0),
                          "completion_tokens": usage.get("output_tokens", 0),
                          "total_tokens": usage.get("total_tokens", 0),
                          "prompt_tokens_details": usage.get("input_tokens_details", {}),
                          "completion_tokens_details": usage.get("output_tokens_details", {})}}

    def _cloud_response(self, body, on_delta):
        url = "https://api.openai.com/v1/responses"
        headers = {"Authorization": f"Bearer {self.settings.openai_key}"}
        if not on_delta:
            return checked(self.cloud.post(url, json=body, headers=headers))
        model = self.settings.cloud_model
        with self.cloud.stream("POST", url, json={**body, "stream": True}, headers=headers) as response:
            for event in stream_events(response):
                model = event.get("response", {}).get("model", model)
                if event.get("type") in {"response.output_text.delta", "response.refusal.delta"}:
                    on_delta(event.get("delta", ""), model)
                elif event.get("type") == "response.completed":
                    return event["response"]
        raise ProviderFailure("cloud_incomplete_stream")

    def web_search(self, query):
        if not self.settings.openai_key:
            raise ProviderFailure("openai_key_missing")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
            raise ValueError("Use a public search query of 1–500 characters.")
        body = {"model": self.settings.cloud_model, "input": query, "store": False, "max_output_tokens": 1200,
                "reasoning": {"effort": "low"}, "tools": [{"type": "web_search", "search_context_size": "low"}],
                "tool_choice": {"type": "web_search"},
                "instructions": "Search for the user's public query. Return a brief factual summary with cited URLs."}
        try:
            response = self._cloud_response(body, None)
        except httpx.HTTPError as exc:
            raise ProviderFailure("web_search_connection_failed", True) from exc
        links, summaries = {}, []
        for item in response.get("output", []):
            if item.get("type") != "message":
                continue
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    summaries.append(part.get("text", ""))
                for citation in part.get("annotations", []):
                    url = citation.get("url", "")
                    if citation.get("type") == "url_citation" and url.startswith(("https://", "http://")):
                        links[url] = {"title": citation.get("title") or url, "url": url}
        if not links:
            raise ProviderFailure("web_search_no_sources")
        return {"success": True, "backend": "openai", "data": {"web": list(links.values())[:5]},
                "untrusted_summary": "\n".join(summaries), "usage": response.get("usage", {})}


def completion_events(result, *, text_already_sent=False):
    """Emit the validated tool calls, finish reason, token usage, and terminal marker."""
    base = {key: result[key] for key in ("id", "created", "model")}
    base["object"] = "chat.completion.chunk"
    choice = result["choices"][0]
    message = dict(choice["message"])
    if text_already_sent:
        message.pop("content", None)
        message.pop("role", None)
    for index, call in enumerate(message.get("tool_calls", [])):
        call["index"] = index
    frames = [(message, None)] if message else []
    frames.append(({}, choice["finish_reason"]))
    for delta, finish in frames:
        yield "data: " + json.dumps({**base, "choices": [{"index": 0, "delta": delta,
                                                          "finish_reason": finish}]}) + "\n\n"
    yield "data: " + json.dumps({**base, "choices": [], "usage": result.get("usage", {})}) + "\n\n"
    yield "data: [DONE]\n\n"
