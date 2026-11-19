"""Run the pinned Hermes gateway with synthetic Ollama and optional real local inference."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4

import httpx


class OllamaFixture(BaseHTTPRequestHandler):
    available = True

    def log_message(self, *args):
        pass

    def respond(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        if self.available:
            self.respond({"models": [{"name": "gemma4:e2b"}]})
        else:
            self.respond({"error": "offline"}, 503)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not self.available:
            self.respond({"error": "offline"}, 503)
            return
        messages = body["messages"]
        latest = messages[-1]
        answer = {"role": "assistant", "content": "Synthetic Hermes response."}
        finish = "stop"
        if latest["role"] == "user" and "Remember" in latest["content"]:
            tool_names = {tool["function"]["name"] for tool in body["tools"]}
            if "a3_remember" not in tool_names or "terminal" in tool_names:
                self.respond({"id": "chatcmpl-fixture", "model": "gemma4:e2b", "created": int(time.time()),
                              "choices": [{"index": 0, "finish_reason": "stop", "message": {
                                  "role": "assistant", "content": f"Unexpected tools: {sorted(tool_names)}"}}]})
                return
            answer = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_remember", "type": "function", "function": {"name": "a3_remember",
                "arguments": '{"key":"codename","value":"Synthetic Nebula"}'}}]}
            finish = "tool_calls"
        elif latest["role"] == "tool" and "remembered" in latest["content"]:
            answer["content"] = "Remembered Synthetic Nebula."
        elif latest["role"] == "user" and "Synthetic Nebula" in latest["content"]:
            answer["content"] = "Your codename is Synthetic Nebula."
        self.respond({"id": "chatcmpl-fixture", "object": "chat.completion", "created": int(time.time()),
                      "model": "gemma4:e2b", "choices": [{"index": 0, "message": answer, "finish_reason": finish}],
                      "usage": {"prompt_tokens": 64, "completion_tokens": 4, "total_tokens": 68}})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-ollama", action="store_true")
    parser.add_argument("--live-cloud", action="store_true")
    parser.add_argument("--model", default="gemma4:e2b")
    parser.add_argument("--container", action="store_true")
    args = parser.parse_args()
    if args.live_ollama and args.container:
        raise SystemExit("Run real Ollama on the host with --live-ollama; use --container for the mock test.")
    if args.live_cloud and not args.live_ollama:
        raise SystemExit("Use --live-cloud with --live-ollama for the hybrid gateway test.")
    root = Path(__file__).resolve().parents[1]
    owner_key, internal_key = uuid4().hex * 2, uuid4().hex * 2
    fixture = None
    if not args.live_ollama:
        fixture = ThreadingHTTPServer(("0.0.0.0" if args.container else "127.0.0.1", 11438), OllamaFixture)
        threading.Thread(target=fixture.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="bluebird-gateway-") as directory:
        port = 18080 if args.container else 8080
        env = {"API_SERVER_KEY": owner_key, "A3_INTERNAL_KEY": internal_key, "A3_LOCAL_MODEL": args.model,
               "TELEGRAM_BOT_TOKEN": "", "OPENAI_API_KEY": "", "A3_BUCKET": "", "A3_RUN_TAILSCALE": "0",
               "A3_SOCKS_PROXY": "", "A3_OLLAMA_URL": ("http://host.docker.internal:11438" if args.container else
               os.getenv("A3_OLLAMA_URL", "http://127.0.0.1:11434") if args.live_ollama else "http://127.0.0.1:11438")}
        if args.live_cloud:
            env.pop("OPENAI_API_KEY")
        name = "bluebird-a3-smoke-" + uuid4().hex[:8]
        if args.container:
            # Avoid placing generated keys in command arguments or host logs.
            command = ["docker", "run", "--platform=linux/amd64", "--rm", "--name", name, "--memory=1g", "--cpus=1", "--cap-drop=ALL",
                       "--security-opt=no-new-privileges", "-p", f"127.0.0.1:{port}:8080"]
            for key in env:
                command.extend(["-e", key])
            command.append("bluebird-assistant:local")
        else:
            env.update(A3_STATE_DIR=directory, HERMES_HOME=f"{directory}/hermes")
            command = [sys.executable, "-m", "assistant.cli", "start"]
        with tempfile.TemporaryFile(mode="w+") as log:
            proc = subprocess.Popen(command, cwd=root, env={**os.environ, **env}, stdout=log, stderr=log)
            try:
                base = f"http://127.0.0.1:{port}"
                with httpx.Client(base_url=base, timeout=300) as client:
                    deadline = time.monotonic() + (150 if args.container else 60)
                    while time.monotonic() < deadline:
                        if proc.poll() is not None:
                            raise RuntimeError("Assistant exited before its startup probe passed.")
                        try:
                            if client.get("/health", timeout=3).status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(0.25)
                    else:
                        raise RuntimeError("Assistant did not become ready before the startup deadline.")

                    chat_id = str(uuid4())

                    def chat(message):
                        reply = client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {owner_key}",
                                            "X-A3-Chat": chat_id}, json={"messages": [{"role": "user", "content": message}]})
                        reply.raise_for_status()
                        return reply.json()["choices"][0]["message"]["content"]

                    if args.live_ollama:
                        answer = chat("What is 12 times 9?")
                        print(f"Live local calculation ({args.model}): {answer}")
                        reply = chat("Remember that my synthetic project codename is Nebula. Call a3_remember with key codename and value Nebula.")
                        print(f"Live local memory turn: {reply}")
                        with sqlite3.connect(Path(directory) / "assistant.db") as database:
                            row = database.execute("SELECT value FROM memory WHERE key='codename'").fetchone()
                        assert row and row[0] == "Nebula", "Gemma did not successfully execute the Hermes memory tool."
                        if args.live_cloud:
                            answer = chat("Explain step by step why the sky is blue in two sentences.")
                            assert answer and "scatter" in answer.lower(), answer
                            print("Live Luna reasoning passed through Hermes.")
                            answer = chat("Plan a 60-minute meeting. Use a3_gap_finder for 2026-11-16, "
                                          "working hours 09:00 to 11:00 Los Angeles, and an event from "
                                          "2026-11-16T09:00:00-08:00 to 2026-11-16T10:00:00-08:00.")
                            assert "10:00" in answer, answer
                            print("Live Luna gap-finder tool turn passed through Hermes.")
                            with httpx.Client(base_url="http://127.0.0.1:8090", timeout=120,
                                              headers={"Authorization": f"Bearer {internal_key}",
                                                       "X-A3-Session": "stream-probe"}) as internal:
                                for prompt in ("Count from 1 to 10.", "Explain how a rainbow forms."):
                                    internal.post("/internal/context", json={"session": "stream-probe",
                                                  "platform": "probe", "message": prompt}).raise_for_status()
                                    with internal.stream("POST", "/v1/chat/completions", json={"stream": True,
                                                         "messages": [{"role": "user", "content": prompt}]}) as result:
                                        result.raise_for_status()
                                        frames = [line[6:] for line in result.iter_lines() if line.startswith("data: ")]
                                        assert frames[-1] == "[DONE]", frames[-1]
                                        parsed = [json.loads(frame) for frame in frames[:-1]]
                                        assert parsed[-1]["usage"]["prompt_tokens"] > 0
                                        assert len([frame for frame in parsed if frame.get("choices")
                                                    and frame["choices"][0]["delta"].get("content")]) > 1
                            print("Live Ollama and Luna SSE deltas and usage passed.")
                    else:
                        answer = chat("Remember the synthetic codename Synthetic Nebula.")
                        assert "Remembered Synthetic Nebula" in answer, answer
                        chat_id = str(uuid4())
                        answer = chat("What is my codename?")
                        assert "Your codename is Synthetic Nebula" in answer, answer
                        OllamaFixture.available = False
                        answer = chat("This is confidential: TEST-CANARY")
                        assert "private request is held" in answer, answer
                        answer = chat("What about the previous topic?")
                        assert "private request is held" in answer, answer
                        OllamaFixture.available = True
                        assert "Synthetic Hermes response" in chat("What is 6 times 7?")
                        print("Hermes gateway, tool call, fresh-chat memory, private hold and recovery passed.")
            finally:
                if args.container:
                    subprocess.run(["docker", "stop", "--time", "8", name], check=False, stdout=subprocess.DEVNULL)
                else:
                    proc.terminate()
                proc.wait(timeout=25)
                log.seek(0)
                print(log.read())
    if fixture:
        fixture.shutdown()


if __name__ == "__main__":
    main()
