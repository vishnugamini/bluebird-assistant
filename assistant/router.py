import json
import queue
import re
import threading
import time
import uuid

from assistant.config import LOCAL_CONTEXT_TOKENS
from assistant.logging import event
from assistant.providers import ProviderFailure, completion_events, local_output_tokens
from assistant.state import PRIVATE_PREFIX


PRIVATE_OUTAGE_NOTICE = (
    "This private request is held. Start Ollama and reconnect Tailscale, then send the request again. "
    "This conversation stays local-only."
)


class Router:
    def __init__(self, settings, state, providers):
        self.settings, self.state, self.providers = settings, state, providers
        self.locks = {}
        self.lock = threading.Lock()

    @staticmethod
    def fits_local(payload):
        # Estimate input tokens from the serialized input and leave room for Ollama's template.
        local_input = {key: payload[key] for key in ("messages", "tools", "tool_choice") if key in payload}
        estimated_input = len(json.dumps(local_input, ensure_ascii=False).encode()) // 2
        return estimated_input + local_output_tokens(payload) + 512 <= LOCAL_CONTEXT_TOKENS

    def complete(self, payload, session, on_delta=None):
        with self.lock:
            serial = self.locks.setdefault(session, threading.Lock())
        with serial:
            return self._complete(payload, self.state.session(session), on_delta)

    def _complete(self, payload, context, on_delta):
        request_id, started = uuid.uuid4().hex, time.monotonic()
        text = context["user_message"]
        heavy = bool(re.search(r"\b(explain|step.by.step|analy[sz]e|compare|plan|research|synthesi\w*)\b", text, re.I))
        simple = len(text) <= 500 and not heavy
        route, reason = "cloud", "reasoning_or_general_task"
        if context["private"]:
            route, reason = "local", "private_conversation"
            allowed = {"a3_gap_finder", "a3_remember"}
            tools = [t for t in payload.get("tools", []) if t.get("function", {}).get("name") in allowed]
            payload = {key: value for key, value in payload.items() if key not in {"tool_choice", "tools"}}
            payload["messages"] = self.private_messages(payload["messages"])
            if tools:
                payload["tools"] = tools
        elif simple:
            route, reason = "local", "short_simple_task"
        fits = self.fits_local(payload)
        if context["private"] and not fits:
            return self._held(request_id, started, "private_context_limit",
                              "This private conversation exceeds the local context. Start a new private chat "
                              "with a shorter request.")
        if route == "local" and (not fits or not self.providers.local_available()):
            if context["private"]:
                return self._held(request_id, started, "private_local_unavailable")
            route, reason = "cloud", "local_unavailable_or_context_limit"
        event("route_decision", requestId=request_id, target=route, reason=reason)
        text_started = False

        def emit(content, model):
            nonlocal text_started
            if content:
                text_started = True
                on_delta(content, model)

        stream = {"on_delta": emit} if on_delta else {}
        try:
            result = self._call(route, payload, context["id"], **stream)
        except ProviderFailure as exc:
            event("model_failure", requestId=request_id, target=route, code=exc.code)
            if text_started:
                self._record(request_id, started, route, reason, False, {}, "")
                raise
            if context["private"]:
                return self._held(request_id, started, "private_local_failed")
            if route == "cloud" and exc.retryable and fits and self.providers.local_available():
                route, reason = "local", "cloud_transient_fallback"
            elif route == "local":
                route, reason = "cloud", "local_failure_fallback"
            else:
                self._record(request_id, started, route, reason, False, {}, "")
                raise
            event("route_decision", requestId=request_id, target=route, reason=reason)
            try:
                result = self._call(route, payload, context["id"], **stream)
            except ProviderFailure:
                self._record(request_id, started, route, reason, False, {}, "")
                raise
        self._record(request_id, started, route, reason, True, result.get("usage", {}), result["model"])
        return result

    @staticmethod
    def private_messages(messages):
        cleaned = []
        for message in messages:
            content = message.get("content")
            if message["role"] == "user" and isinstance(content, str):
                content = PRIVATE_PREFIX.sub("", content, count=1) or content
            elif message["role"] == "assistant" and content == PRIVATE_OUTAGE_NOTICE:
                content = "The earlier request was held during a temporary connection outage."
            cleaned.append({**message, "content": content} if content != message.get("content") else message)
        return cleaned

    def _call(self, route, payload, session, **stream):
        if route == "local":
            return self.providers.ollama(payload, **stream)
        if float(self.state.flag("simulate_429") or 0) > time.time():
            raise ProviderFailure("simulated_http_429", True)
        return self.providers.openai(payload, session, **stream)

    def stream(self, payload, session):
        frames = queue.SimpleQueue()
        identifier, created = f"chatcmpl-{uuid.uuid4().hex}", int(time.time())
        sent_text = False

        def emit(content, model):
            nonlocal sent_text
            delta = {"content": content}
            if not sent_text:
                delta["role"] = "assistant"
            sent_text = True
            chunk = {"id": identifier, "created": created, "model": model, "object": "chat.completion.chunk",
                     "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
            frames.put("data: " + json.dumps(chunk) + "\n\n")

        def produce():
            try:
                result = self.complete(payload, session, emit)
                result.update(id=identifier, created=created)
                for frame in completion_events(result, text_already_sent=sent_text):
                    frames.put(frame)
            except Exception as exc:
                event("inference_stream_failed", errorType=type(exc).__name__)
                frames.put('data: {"error":{"message":"Inference stream interrupted. Retry the request."}}\n\n')
            finally:
                frames.put(None)

        threading.Thread(target=produce, daemon=True).start()
        while (frame := frames.get()) is not None:
            yield frame

    def _record(self, request_id, started, route, reason, ok, usage, served):
        latency = (time.monotonic() - started) * 1000
        model = served or (self.settings.local_model if route == "local" else self.settings.cloud_model)
        self.state.record_call(request_id, route, model, reason, ok, latency, usage)
        event("route_result", requestId=request_id, target=route, reason=reason, servedModel=model,
              succeeded=ok, latencyMs=round(latency), usage=usage)

    def _held(self, request_id, started, reason, message=None):
        self._record(request_id, started, "held", reason, False, {}, "none")
        return {"id": "chatcmpl-" + request_id, "object": "chat.completion", "created": int(time.time()),
                "model": "held", "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": message or PRIVATE_OUTAGE_NOTICE}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
