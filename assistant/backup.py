from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import sqlite3
import tempfile
import threading
import uuid
import zipfile

from google.api_core.exceptions import NotFound
from google.cloud import storage

from assistant.logging import event


def include_file(path):
    name = path.name
    if name in {"assistant.db", "tailscale.state"}:
        return len(path.parts) == 1
    if not path.parts or path.parts[0] != "hermes":
        return False
    if name.endswith(("-wal", "-shm", ".lock")):
        return False
    return (name in {"state.db", "gateway_state.json", "sessions.json", "MEMORY.md", "USER.md"}
            or len(path.parts) > 2 and path.parts[1] in {"sessions", "memories", "cron"}
            and name.endswith((".db", ".json", ".jsonl", ".md")))


class Backup:
    def __init__(self, settings):
        self.settings = settings
        self.lock = threading.RLock()
        self.bucket = None

    def remote(self):
        if self.bucket is None:
            self.bucket = storage.Client().bucket(self.settings.bucket)
        return self.bucket

    def snapshot(self):
        if not self.settings.bucket:
            return
        with self.lock:
            content = io.BytesIO()
            with zipfile.ZipFile(content, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(self.settings.state_dir.rglob("*")):
                    relative = path.relative_to(self.settings.state_dir)
                    if not path.is_file() or path.is_symlink() or not include_file(relative):
                        continue
                    if path.suffix == ".db":
                        with tempfile.TemporaryDirectory(dir=self.settings.state_dir) as folder:
                            copied = Path(folder) / "snapshot.db"
                            src = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
                            dst = sqlite3.connect(copied)
                            try:
                                src.backup(dst)
                            finally:
                                src.close()
                                dst.close()
                            archive.write(copied, relative.as_posix())
                    else:
                        try:
                            archive.write(path, relative.as_posix())
                        except FileNotFoundError:
                            continue
            data = content.getvalue()
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            name = f"snapshots/{stamp}-{uuid.uuid4().hex}.zip"
            bucket = self.remote()
            pointer = bucket.blob("snapshots/latest.json")
            try:
                pointer.reload(timeout=15)
                generation = int(pointer.generation)
            except NotFound:
                generation = 0
            bucket.blob(name).upload_from_string(data, content_type="application/zip", if_generation_match=0,
                                                  timeout=30)
            manifest = {"name": name, "sha256": hashlib.sha256(data).hexdigest()}
            pointer.upload_from_string(json.dumps(manifest), content_type="application/json",
                                       if_generation_match=generation, timeout=15)
            event("snapshot_completed", object=name, bytes=len(data))

    def restore(self):
        if not self.settings.bucket:
            return False
        with self.lock:
            bucket = self.remote()
            try:
                manifest = json.loads(bucket.blob("snapshots/latest.json").download_as_bytes(timeout=20))
            except NotFound:
                event("snapshot_empty")
                return False
            if not manifest["name"].startswith("snapshots/") or not manifest["name"].endswith(".zip"):
                raise ValueError("Invalid snapshot manifest.")
            data = bucket.blob(manifest["name"]).download_as_bytes(timeout=45)
            if hashlib.sha256(data).hexdigest() != manifest["sha256"]:
                raise ValueError("Snapshot integrity check failed.")
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                for name in archive.namelist():
                    path = PurePosixPath(name)
                    if path.is_absolute() or ".." in path.parts or not include_file(path):
                        raise ValueError("Snapshot includes an unexpected path.")
                    target = self.settings.state_dir / name
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    target.write_bytes(archive.read(name))
                    target.chmod(0o600)
            event("snapshot_restored", object=manifest["name"])
            return True
