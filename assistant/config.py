from dataclasses import dataclass
import os
from pathlib import Path


LOCAL_CONTEXT_TOKENS = 65_536
LOCAL_MAX_OUTPUT_TOKENS = 1024


@dataclass(frozen=True)
class Settings:
    state_dir: Path
    hermes_home: Path
    internal_key: str
    gateway_key: str
    openai_key: str
    cloud_model: str
    local_model: str
    ollama_url: str
    socks_proxy: str
    telegram_token: str
    telegram_user: str
    telegram_chat: str
    test_email: str
    calendar_id: str
    email_allowlist: tuple[str, ...]
    google_token_file: Path
    bucket: str
    timezone: str

    @classmethod
    def from_env(cls):
        state = Path(os.getenv("A3_STATE_DIR", "state")).resolve()
        return cls(
            state, Path(os.getenv("HERMES_HOME", str(state / "hermes"))).resolve(),
            os.getenv("A3_INTERNAL_KEY", ""), os.getenv("API_SERVER_KEY", ""),
            os.getenv("OPENAI_API_KEY", ""), os.getenv("A3_CLOUD_MODEL", "gpt-6-luna"),
            os.getenv("A3_LOCAL_MODEL", "gemma4:e2b-it-q4_K_M"), os.getenv("A3_OLLAMA_URL", ""),
            os.getenv("A3_SOCKS_PROXY", ""), os.getenv("TELEGRAM_BOT_TOKEN", ""),
            os.getenv("TELEGRAM_ALLOWED_USERS", ""), os.getenv("TELEGRAM_HOME_CHANNEL", ""),
            os.getenv("A3_TEST_EMAIL", ""), os.getenv("A3_CALENDAR_ID", ""),
            tuple(v.strip().lower() for v in os.getenv("A3_EMAIL_ALLOWLIST", "").split(",") if v.strip()),
            Path(os.getenv("A3_GOOGLE_TOKEN_FILE", "secrets/google-token.json")),
            os.getenv("A3_BUCKET", ""), os.getenv("A3_TIMEZONE", "America/Los_Angeles"),
        )

    def require_runtime_keys(self):
        if len(self.internal_key) < 32 or len(self.gateway_key) < 32:
            raise ValueError("Set A3_INTERNAL_KEY and API_SERVER_KEY to independent random 32+ character keys.")
