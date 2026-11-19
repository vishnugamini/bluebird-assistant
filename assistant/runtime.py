import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import httpx

from assistant.backup import Backup
from assistant.logging import event


def configure_hermes(settings, project):
    home = settings.hermes_home
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    plugin = home / "plugins" / "bluebird"
    shutil.copytree(project / "hermes" / "bluebird", plugin, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(project / "hermes" / "SOUL.md", home / "SOUL.md")
    tools = ["bluebird"]
    config = {
        "model": {"provider": "custom", "default": "bluebird-router", "base_url": "http://127.0.0.1:8090/v1",
                  "context_length": 65536, "max_tokens": 4096},
        "providers": {"bluebird-router": {"api": "http://127.0.0.1:8090/v1", "transport": "chat_completions",
                                          "default_model": "bluebird-router", "context_length": 65536,
                                          "discover_models": False, "key_env": "A3_INTERNAL_KEY",
                                          "session_affinity_header": "X-A3-Session"}},
        "plugins": {"enabled": ["bluebird"]},
        "tools": {"tool_search": {"enabled": "off"}},
        "platform_toolsets": {"telegram": tools, "api_server": tools, "cli": tools},
        "platforms": {"telegram": {"extra": {"drop_pending_on_cold_boot": False}},
                      "api_server": {"enabled": True, "extra": {"host": "127.0.0.1", "port": 8081}}},
        "agent": {"max_turns": 8},
        "web": {"backend": "ddgs", "keyless_fallback": False},
        "compression": {"enabled": False},
        "memory": {"memory_enabled": False, "user_profile_enabled": False, "nudge_interval": 0},
        "skills": {"creation_nudge_interval": 0},
        "auxiliary": {"background_review": {"enabled": False}, "title_generation": {"enabled": False}},
        "terminal": {"cwd": str(home)},
        "logging": {"level": "WARNING"},
    }
    (home / "config.yaml").write_text(json.dumps(config, indent=2))


def wait_http(url, process, seconds=45):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and process.poll() is None:
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"Process did not become ready at {url}.")


def run_services(settings):
    settings.require_runtime_keys()
    project = Path(os.environ.get("A3_PROJECT_ROOT", Path.cwd())).resolve()
    hermes = Path(os.environ.get("A3_HERMES_BIN", project / ".runtime/hermes/.venv/bin/hermes"))
    if not hermes.is_file():
        raise SystemExit("Install the pinned Hermes runtime with scripts/install-hermes.sh.")
    settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    mounted_token = Path("/secrets/google/token.json")
    if mounted_token.is_file():
        settings.google_token_file.write_bytes(mounted_token.read_bytes())
        settings.google_token_file.chmod(0o600)
    Backup(settings).restore()
    configure_hermes(settings, project)
    event("container_isolation", uid=os.getuid(), gid=os.getgid(),
          runtime="linux" if sys.platform == "linux" else sys.platform,
          capabilities=_effective_caps())
    children = []
    stop = False

    def stop_handler(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)

    def launch(args, **kwargs):
        process = subprocess.Popen([str(x) for x in args], **kwargs)
        children.append(process)
        return process

    def web(factory, host, port):
        return launch([sys.executable, "-m", "uvicorn", f"assistant.service:{factory}", "--factory",
                       "--host", host, "--port", port, "--no-access-log", "--log-level", "warning"])

    try:
        if os.getenv("A3_RUN_TAILSCALE") == "1":
            tailscale = _start_tailnet(settings, launch)
        else:
            tailscale = None
        internal = web("create_internal", "127.0.0.1", 8090)
        wait_http("http://127.0.0.1:8090/health", internal)
        gateway_env = {k: v for k, v in os.environ.items() if k not in {
            "OPENAI_API_KEY", "TAILSCALE_AUTHKEY", "GOOGLE_APPLICATION_CREDENTIALS", "A3_GOOGLE_TOKEN_FILE"}}
        gateway_env.update(HERMES_HOME=str(settings.hermes_home), API_SERVER_ENABLED="true", API_SERVER_PORT="8081",
                           API_SERVER_HOST="127.0.0.1", API_SERVER_MODEL_NAME="hermes-agent",
                           OPENAI_BASE_URL="http://127.0.0.1:8090/v1", OPENAI_API_KEY=settings.internal_key,
                           GATEWAY_ALLOW_ALL_USERS="false", HERMES_DISABLE_BROWSER_VAULT="1")
        gateway = launch([hermes, "gateway", "run"], env=gateway_env)
        public = web("create_public", os.getenv("A3_PUBLIC_HOST", "127.0.0.1"), os.getenv("PORT", "8080"))
        wait_http("http://127.0.0.1:8081/health", gateway, seconds=180)
        event("gateway_ready")
        essential = [internal, gateway, public]
        if tailscale:
            essential.append(tailscale)
        while not stop and all(process.poll() is None for process in essential):
            time.sleep(0.5)
        if not stop:
            raise RuntimeError("A required assistant process exited.")
    finally:
        for process in reversed(children):
            if process.poll() is None:
                process.terminate()
        for process in reversed(children):
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def _effective_caps():
    if sys.platform != "linux":
        return "unavailable"
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("CapEff:"):
            return line.split()[1]
    return "unavailable"


def _start_tailnet(settings, launch):
    # Adapted from https://tailscale.com/docs/install/cloud/cloudrun for a non-root process.
    socket = settings.state_dir / "tailscaled.sock"
    state_file = settings.state_dir / "tailscale.state"
    daemon = launch(["tailscaled", "--tun=userspace-networking", "--socks5-server=127.0.0.1:1055",
                     f"--socket={socket}", f"--state={state_file}"])
    for _ in range(80):
        if socket.exists():
            break
        if daemon.poll() is not None:
            raise RuntimeError("Tailscale daemon exited during startup.")
        time.sleep(0.25)
    # Cloud inference must remain available when Tailscale cannot authenticate.
    cmd = ["tailscale", f"--socket={socket}", "up", "--hostname=bluebird-a3", "--accept-dns=false", "--timeout=12s"]
    token_path = Path(os.getenv("A3_TAILSCALE_KEY_FILE", "/secrets/tailscale/key"))
    if token_path.is_file():
        cmd.append(f"--auth-key=file:{token_path}")
    try:
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, check=False)
        connected = result.returncode == 0
    except subprocess.TimeoutExpired:
        connected = False
    event("tailnet_start", succeeded=connected)
    return daemon
