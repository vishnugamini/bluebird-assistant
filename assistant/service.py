from contextlib import asynccontextmanager
from hmac import compare_digest
import json
from pathlib import Path
import re
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
import httpx

from assistant.backup import Backup
from assistant.config import Settings
from assistant.email import Email
from assistant.gaps import conflicts, find_gaps
from assistant.google import Google
from assistant.jobs import Jobs, event_time, weather
from assistant.logging import event
from assistant.providers import ProviderFailure, Providers
from assistant.router import Router
from assistant.state import State
from assistant.telegram import Telegram


PUBLIC_FILE = Path(__file__).parent / "web" / "index.html"


def authorized(request, key):
    if not key or not compare_digest(request.headers.get("Authorization", "").encode(), f"Bearer {key}".encode()):
        raise HTTPException(401, "Invalid API key.")


def create_internal(settings=None, *, providers=None, google=None, telegram=None, schedule=True):
    settings = settings or Settings.from_env()
    settings.require_runtime_keys()
    state = State(settings.state_dir)
    providers = providers or Providers(settings)
    google = google or Google(settings)
    telegram = telegram or Telegram(settings)
    backup = Backup(settings)
    email = Email(settings, state, google, telegram, backup.snapshot)
    router = Router(settings, state, providers)
    jobs = Jobs(settings, state, google, telegram, providers, backup, router)

    @asynccontextmanager
    async def lifespan(app):
        recovered = state.recover_inflight()
        if any(recovered.values()):
            event("interrupted_work_recovered", **recovered)
        if schedule:
            jobs.start()
        event("assistant_started", scheduler=schedule)
        try:
            yield
        finally:
            if jobs.scheduler.running:
                jobs.scheduler.shutdown(wait=True)
            jobs.snapshot()
            if hasattr(providers, "close"):
                providers.close()
            event("assistant_stopped")

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.database = state

    @app.exception_handler(PermissionError)
    async def permission_error(request, exc):
        return JSONResponse({"error": {"message": str(exc)}}, status_code=403)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"error": {"message": str(exc)}}, status_code=400)

    @app.exception_handler(ProviderFailure)
    async def provider_error(request, exc):
        return JSONResponse({"error": {"message": "Inference is unavailable. Check Ollama, Tailscale, or OpenAI.",
                                       "code": exc.code}}, status_code=503)

    @app.exception_handler(RuntimeError)
    async def runtime_error(request, exc):
        event("operation_failed", errorType=type(exc).__name__)
        return JSONResponse({"error": {"message": "The operation failed. Check the integration configuration."}},
                            status_code=503)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/v1/models")
    def models(request: Request):
        authorized(request, settings.internal_key)
        return {"object": "list", "data": [{"id": "bluebird-router", "object": "model", "context_length": 65536}]}

    @app.post("/v1/chat/completions")
    def completions(request: Request, body: dict):
        authorized(request, settings.internal_key)
        if not isinstance(body.get("messages"), list) or not body["messages"]:
            raise ValueError("A nonempty messages array is required.")
        session = request.headers.get("X-A3-Session", "")
        # Every inference request, including auxiliary calls, requires trusted session context.
        if body.get("stream"):
            state.session(session)
            return StreamingResponse(router.stream(body, session), media_type="text/event-stream")
        return router.complete(body, session)

    @app.post("/internal/context")
    def context(request: Request, body: dict):
        authorized(request, settings.internal_key)
        session = body["session"]
        context = state.bind(session, body["platform"], body.get("sender", ""), body["message"])
        return {"private": bool(context["private"]), "memory": state.memories(session)}

    @app.post("/internal/tool")
    def tool(request: Request, body: dict):
        authorized(request, settings.internal_key)
        session, name, args = body["session"], body["tool"], body.get("args", {})
        context = state.session(session)
        if name == "remember":
            state.remember(session, args["key"], args["value"])
            backup.snapshot()
            return {"status": "remembered"}
        if name == "gap_finder":
            result = find_gaps(args)
            event("tool_completed", tool=name)
            return result
        if context["private"]:
            raise PermissionError("External tools are unavailable in private conversations.")
        if name == "email_read":
            return google.emails(args.get("query", "is:unread"))
        if name == "email_draft":
            return email.prepare(session, args["recipient"], args["subject"], args["body"],
                                 args.get("request_approval") is True)
        if name == "calendar":
            items = google.events(args.get("start"), args.get("end"))
            return {"untrusted_calendar_content": items[:30], "conflicts": conflicts(items, settings.timezone),
                    "busy": [{"start": event_time(item, "start"), "end": event_time(item, "end")}
                             for item in items[:30]]}
        if name == "weather":
            return {"forecast": weather()}
        if name == "web_search":
            result = providers.web_search(args["query"])
            event("tool_completed", tool=name, backend="openai", usage=result["usage"])
            return result
        raise PermissionError("Tool is outside the assistant allowlist.")

    @app.get("/internal/external")
    def external(request: Request, session: str):
        authorized(request, settings.internal_key)
        if state.session(session)["private"]:
            raise PermissionError("External tools are unavailable in private conversations.")
        return {"ok": True}

    @app.post("/internal/approval")
    def approval(request: Request, body: dict):
        authorized(request, settings.internal_key)
        return email.decide(body["token"], body["approve"] is True, body["sender"], body["platform"])

    @app.post("/internal/run")
    def run(request: Request, body: dict):
        authorized(request, settings.internal_key)
        return jobs.run(body["job"], manual=True)

    @app.post("/internal/simulate")
    def simulate(request: Request, body: dict):
        authorized(request, settings.internal_key)
        enabled = body.get("enabled") is True
        state.set_flag("simulate_429", time.time() + 300 if enabled else 0)
        event("simulation_changed", simulated429=enabled)
        return {"simulated_429": enabled}

    return app


def create_public(settings=None):
    settings = settings or Settings.from_env()
    settings.require_runtime_keys()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/")
    def index():
        return FileResponse(PUBLIC_FILE, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                                "Referrer-Policy": "no-referrer"})

    @app.get("/privacy")
    def privacy():
        return FileResponse(PUBLIC_FILE.with_name("privacy.html"),
                            headers={"X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})

    @app.get("/health")
    async def health():
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                reply = await client.get("http://127.0.0.1:8081/health")
                if reply.status_code == 200:
                    return {"status": "ok"}
        except httpx.HTTPError:
            pass
        return JSONResponse({"status": "starting"}, status_code=503)

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        authorized(request, settings.gateway_key)
        chat_id = request.headers.get("X-A3-Chat", "")
        if not re.fullmatch(r"[0-9a-f-]{36}", chat_id):
            raise HTTPException(400, "Start a new chat and try again.")
        raw = await request.body()
        if len(raw) > 70000:
            raise HTTPException(413, "Use a shorter message.")
        try:
            messages = json.loads(raw)["messages"]
            text = messages[0]["content"]
            if len(messages) != 1 or messages[0]["role"] != "user" or not isinstance(text, str) or not text.strip():
                raise ValueError
        except (ValueError, KeyError, IndexError, TypeError):
            raise HTTPException(400, "One text message is required.") from None
        try:
            async with httpx.AsyncClient(timeout=420, follow_redirects=False) as client:
                result = await client.post("http://127.0.0.1:8081/v1/chat/completions",
                                           headers={"Authorization": f"Bearer {settings.gateway_key}",
                                                    "X-Hermes-Session-Id": f"a3-web-{chat_id}"},
                                           json={"model": "hermes-agent", "messages": [{"role": "user", "content": text}]})
            if result.status_code < 400:
                return JSONResponse(result.json(), headers={"Cache-Control": "no-store"})
        except (httpx.HTTPError, ValueError):
            pass
        raise HTTPException(502, "Hermes could not answer. Check the deployment and model connectivity.")

    @app.post("/ops/{operation}")
    async def operator(request: Request, operation: str):
        authorized(request, settings.internal_key)
        if operation not in {"run", "simulate"}:
            raise HTTPException(404)
        data = await request.body()
        if len(data) > 1024:
            raise HTTPException(413)
        async with httpx.AsyncClient(timeout=180, follow_redirects=False) as client:
            response = await client.post(f"http://127.0.0.1:8090/internal/{operation}", content=data,
                                         headers={"Authorization": f"Bearer {settings.internal_key}",
                                                  "Content-Type": "application/json"})
        return JSONResponse(response.json(), status_code=response.status_code)

    return app
