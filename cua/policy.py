"""Guardrails: what the automation may touch, what counts as risky, and redaction."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List
from urllib.parse import urlparse


class PolicyViolation(Exception):
    pass


@dataclass
class Policy:
    allowed_origins: List[str]
    allowed_actions: List[str]
    risky_controls: List[str] = field(default_factory=list)
    blocked_paths: List[str] = field(default_factory=list)

    @classmethod
    def from_profile(cls, profile: dict) -> "Policy":
        return cls(**profile["policy"])

    def check_url(self, url: str) -> None:
        u = urlparse(url)
        origin = "%s://%s" % (u.scheme, u.netloc)
        if origin not in self.allowed_origins:
            raise PolicyViolation("origin not allowed: %s" % origin)
        for p in self.blocked_paths:
            if u.path.startswith(p):
                raise PolicyViolation("path blocked: %s" % u.path)

    def check_action(self, action: str) -> None:
        if action not in self.allowed_actions:
            raise PolicyViolation("action not allowed: %s" % action)

    def is_risky(self, control_name: str) -> bool:
        # Matching on the visible label is crude but it is what an operator would
        # go by too. Anything that commits money or changes a record is listed in
        # the app profile; unknown buttons default to safe, which is why replay
        # also refuses steps that were never reviewed (see REPORT.md, Safety).
        n = (control_name or "").lower()
        return any(r.lower() in n for r in self.risky_controls)


# --- redaction ---------------------------------------------------------------

_SSN = re.compile(r"\b\d{3}-\d{2}-(\d{4})\b")
_LONG_NUM = re.compile(r"\b\d{9,19}\b")          # account / card numbers
_DOB = re.compile(r"\b(0[1-9]|1[0-2])/\d{2}/(\d{4})\b")


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    if not text:
        return text
    for s in secrets:
        if s:
            text = text.replace(s, "[SECRET]")
    text = _SSN.sub(r"***-**-\1", text)
    text = _LONG_NUM.sub(lambda m: "*" * (len(m.group()) - 4) + m.group()[-4:], text)
    text = _DOB.sub(r"**/**/\2", text)
    return text


def mask_value(v: str) -> str:
    """For logging a PII output: keep shape, hide content."""
    if not v:
        return v
    return v[0] + "*" * max(len(v) - 2, 1) + v[-1]
