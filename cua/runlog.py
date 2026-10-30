"""Per-run evidence: a JSONL event log plus screenshots, everything redacted."""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Iterable

from .policy import redact


def _scrub(obj, secrets):
    if isinstance(obj, str):
        return redact(obj, secrets)
    if isinstance(obj, dict):
        return {k: _scrub(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v, secrets) for v in obj]
    return obj


class RunLog:
    def __init__(self, root: Path, kind: str, secrets: Iterable[str] = ()):
        self.id = "%s-%s-%s" % (kind, time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:4])
        self.dir = Path(root) / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.secrets = [s for s in secrets if s]
        self._f = open(self.dir / "log.jsonl", "a")
        self._shots = 0

    def event(self, type_: str, **data) -> None:
        rec = {"ts": round(time.time(), 3), "type": type_}
        rec.update(_scrub(data, self.secrets))
        self._f.write(json.dumps(rec) + "\n")
        self._f.flush()

    def screenshot(self, surface, label: str) -> str:
        self._shots += 1
        name = "%02d-%s.png" % (self._shots, label)
        try:
            surface.screenshot(self.dir / name)
        except Exception as e:
            self.event("screenshot_failed", error=str(e))
            return ""
        return name

    def write_json(self, name: str, obj) -> None:
        (self.dir / name).write_text(json.dumps(_scrub(obj, self.secrets), indent=2) + "\n")

    def close(self) -> None:
        self._f.close()
