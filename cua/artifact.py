"""
The capability artifact: what a discovery run leaves behind and what replay runs.

Design notes (longer version in REPORT.md):
- A capability has a contract (inputs, outputs, outcomes) and a body (steps).
  Callers only need the contract; reviewers read both.
- Targets describe a control the way an operator sees it (frame, role, visible
  name, label next to it), with a CSS path only as the last fallback. Nothing in
  here is tied to Playwright, so a desktop surface can resolve the same target
  against a UIA/AX tree.
- Values in steps are templates ("{{member_number}}"). Secrets are inputs marked
  secret=True and are never stored, only referenced.
- App-wide runtime conditions (session expired, interstitials, system errors)
  live in the app profile, not here, because every capability on that app
  shares them. The artifact only declares which business outcomes it can return.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1"


class Target(BaseModel):
    frame: str = Field("", description="frame name, '' for the top document")
    role: str
    name: str = ""
    near: str = Field("", description="label text a person sees next to the control")
    css: str = ""


class Read(BaseModel):
    """Where to read a value from. One of label, or row+column."""
    frame: str = ""
    label: Optional[str] = None
    row: Optional[str] = None
    column: Optional[str] = None


class Step(BaseModel):
    id: str
    action: Literal["goto", "click", "fill", "wait_text"]
    target: Optional[Target] = None
    value: Optional[str] = None  # template for fill, path for goto, text for wait_text
    frame: str = ""             # used by wait_text
    risky: bool = False
    note: str = ""              # why the agent did this (from discovery), for reviewers


class InputSpec(BaseModel):
    type: Literal["string", "integer"] = "string"
    description: str = ""
    pattern: Optional[str] = None
    secret: bool = False


class OutputSpec(BaseModel):
    type: Literal["string", "money"] = "string"
    description: str = ""
    read: Read
    pii: bool = False  # returned to the caller, masked in logs


class Checkpoint(BaseModel):
    frame: str = ""
    text: str


class Capability(BaseModel):
    schema_version: str = SCHEMA_VERSION
    id: str
    version: int = 1
    status: Literal["draft", "approved"] = "draft"
    app: str                     # app profile id, e.g. "cucore"
    app_version: str = ""        # vendor version seen while recording
    description: str
    entry: str = "/"             # path relative to the tenant base url
    inputs: Dict[str, InputSpec] = {}
    outputs: Dict[str, OutputSpec] = {}
    outcomes: List[str] = []     # business outcomes this capability may return
    steps: List[Step]
    checkpoint: Checkpoint
    recorded_from: str = ""      # discovery run id, for traceability

    def save(self, path: Union[str, Path]) -> None:
        Path(path).write_text(self.model_dump_json(indent=2, exclude_none=True) + "\n")

    @classmethod
    def load(cls, path: Union[str, Path]) -> "Capability":
        return cls.model_validate(json.loads(Path(path).read_text()))
