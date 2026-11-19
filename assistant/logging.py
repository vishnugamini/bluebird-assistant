import json
import sys
from datetime import datetime, timezone


def event(kind, **fields):
    """Write metadata only. Callers must never include provider exception text or prompts."""
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields}
    print(json.dumps(record, separators=(",", ":")), file=sys.stdout, flush=True)
