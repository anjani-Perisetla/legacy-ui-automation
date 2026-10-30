"""
Deterministic replay: run a saved capability with inputs, no model involved.

Result contract (what the calling agent gets back):
  status = "success"          outputs filled in
  status = "business_outcome" outcome = one of the capability's declared outcomes
                              (e.g. member_not_found). Not an error.
  status = "failure"          error = {kind, step, expected, observed, screenshot}
Recoverable conditions (interstitials, session expiry, slow loads) never reach
the caller; they are handled here and listed under `recoveries`.
"""
from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from .artifact import Capability, Step
from .handoff import assert_automation, request_human, set_control
from .policy import PolicyViolation, mask_value
from .surface import TargetNotFound

MAX_RECOVERIES = 3
CHECKPOINT_WAIT_S = 6.0
_TEMPLATE = re.compile(r"\{\{(\w+)\}\}")
_MONEY = re.compile(r"^-?\$?[\d,]+\.\d{2}$")


@dataclass
class Result:
    status: str
    capability: str
    version: int
    outputs: Dict[str, str] = field(default_factory=dict)
    outcome: Optional[str] = None
    error: Optional[dict] = None
    recoveries: List[str] = field(default_factory=list)
    handoffs: List[dict] = field(default_factory=list)
    run: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class _Stop(Exception):
    def __init__(self, result_kwargs: dict):
        self.kw = result_kwargs


def validate_inputs(cap: Capability, params: Dict[str, str]) -> Optional[str]:
    for name, spec in cap.inputs.items():
        if name not in params or params[name] == "":
            return "missing input: %s" % name
        v = params[name]
        if spec.type == "integer" and not v.isdigit():
            return "input %s must be an integer" % name
        if spec.pattern and not re.fullmatch(spec.pattern, v):
            return "input %s does not match %s" % (name, spec.pattern)
    extra = set(params) - set(cap.inputs)
    if extra:
        return "unknown inputs: %s" % ", ".join(sorted(extra))
    return None


def render(value: str, params: Dict[str, str]) -> str:
    return _TEMPLATE.sub(lambda m: params[m.group(1)], value)


class Replayer:
    def __init__(self, cap: Capability, profile: dict, surface, log,
                 escalate: bool = False, approvals=(), allow_draft: bool = False):
        self.cap = cap
        self.profile = profile
        self.s = surface
        self.log = log
        self.escalate = escalate
        self.approvals = set(approvals)
        self.allow_draft = allow_draft
        self.recoveries: List[str] = []
        self.handoffs: List[dict] = []
        self.did_risky = False

    # --- entry point ---------------------------------------------------------
    def run(self, params: Dict[str, str]) -> Result:
        cap = self.cap
        base = dict(capability=cap.id, version=cap.version, run=self.log.id)
        self.log.event("replay_start", capability=cap.id, version=cap.version,
                       inputs={k: ("[SECRET]" if cap.inputs[k].secret else v)
                               for k, v in params.items() if k in cap.inputs})
        set_control(self.log.dir, "automation")

        if cap.status != "approved" and not self.allow_draft:
            return self._finish(Result(status="failure", error={"kind": "not_approved",
                                "observed": "capability status is %s" % cap.status}, **base))
        bad = validate_inputs(cap, params)
        if bad:
            # caught before touching the app; the caller sent a bad request
            return self._finish(Result(status="failure", error={"kind": "bad_input", "observed": bad}, **base))
        for st in cap.steps:
            if st.risky and st.id not in self.approvals:
                return self._finish(Result(status="failure", error={
                    "kind": "approval_required", "step": st.id,
                    "observed": "step is marked risky; pass --approve %s" % st.id}, **base))

        try:
            self._run_steps(params)
            self._await_checkpoint()
            outputs = self._read_outputs()
        except _Stop as stop:
            return self._finish(Result(**base, **stop.kw))
        return self._finish(Result(status="success", outputs=outputs, **base))

    def _finish(self, r: Result) -> Result:
        r.recoveries = self.recoveries
        r.handoffs = self.handoffs
        logged = r.to_dict()
        logged["outputs"] = {k: (mask_value(v) if self.cap.outputs[k].pii else v) for k, v in r.outputs.items()}
        self.log.event("replay_result", **logged)
        return r

    # --- steps -----------------------------------------------------------------
    def _run_steps(self, params: Dict[str, str]) -> None:
        i = 0
        while i < len(self.cap.steps):
            step = self.cap.steps[i]
            if self._handle_conditions(step) is True:
                i = self._restart(params)
                continue
            try:
                self._do(step, params)
                i += 1
            except TargetNotFound as e:
                # Before calling it stuck, see whether a known screen explains it.
                r = self._handle_conditions(step)
                if r is True:
                    i = self._restart(params)
                elif r is False:
                    pass  # recovered in place, try the same step again
                elif self._retry_once(step, params):
                    i += 1
                else:
                    self._stuck(step, "target not found", str(e))
                    if self._checkpoint_met():
                        return  # the human finished the flow
            except PolicyViolation as e:
                self._fail("policy_violation", step, "allowed action", str(e))

    def _restart(self, params) -> int:
        # first step is always the entry goto (the recorder guarantees it)
        self._do(self.cap.steps[0], params)
        return 1

    def _retry_once(self, step: Step, params) -> bool:
        self.s.page.wait_for_timeout(1500)
        try:
            self._do(step, params)
            self.recoveries.append("retried %s after slow screen" % step.id)
            return True
        except TargetNotFound:
            return False

    def _do(self, step: Step, params: Dict[str, str]) -> None:
        assert_automation(self.log.dir)
        self.s.policy.check_action(step.action)
        t0 = time.time()
        strategy = None
        if step.action == "goto":
            self.s.goto(render(step.value or self.cap.entry, params))
            self._check_version()
        elif step.action == "click":
            strategy = self.s.click(step.target)
            if step.risky:
                self.did_risky = True
        elif step.action == "fill":
            strategy = self.s.fill(step.target, render(step.value or "", params))
        elif step.action == "wait_text":
            if not self._wait(lambda: self.s.has_text(step.frame, step.value), CHECKPOINT_WAIT_S):
                raise TargetNotFound("text %r never appeared" % step.value)
        self.log.event("step", id=step.id, action=step.action, strategy=strategy,
                       ms=int((time.time() - t0) * 1000))
        if strategy and strategy != "role+name" and step.target and step.target.name:
            # matched, but not on the primary strategy: the screen has drifted
            self.log.event("drift", step=step.id, matched_by=strategy)

    def _check_version(self) -> None:
        vm = self.profile.get("version_marker")
        if vm and not self.s.has_text(vm["frame"], vm["text"]):
            self.log.event("drift", note="app version marker %r not found" % vm["text"])

    # --- conditions --------------------------------------------------------------
    def _match_condition(self) -> Optional[dict]:
        for c in self.profile.get("conditions", []):
            if self.s.has_text(c.get("frame", ""), c["contains"]):
                return c
        return None

    def _handle_conditions(self, step: Optional[Step]) -> Optional[bool]:
        """Returns True if the flow must restart, None if nothing matched,
        False if a condition was recovered in place. Raises _Stop otherwise."""
        c = self._match_condition()
        if not c:
            return None
        sid = step.id if step else "checkpoint"
        self.log.event("condition", name=c["name"], kind=c["kind"], step=sid)
        if c["kind"] == "business":
            if c["name"] not in self.cap.outcomes:
                # a business result this capability never declared: surface it as a failure
                self._fail("undeclared_outcome", step, "declared outcome", c["name"])
            raise _Stop({"status": "business_outcome", "outcome": c["name"]})
        if c["kind"] == "failure":
            if self.escalate:
                self._stuck(step, c["name"], self.s.text(c.get("frame", ""))[:300])
                return False
            self._fail(c["name"], step, "no %s screen" % c["name"], self.s.text(c.get("frame", ""))[:300])
        # recoverable
        if len(self.recoveries) >= MAX_RECOVERIES:
            self._fail("recovery_exhausted", step, "recover from %s" % c["name"], "tried %d times" % MAX_RECOVERIES)
        rec = c.get("recover", {})
        if rec.get("restart"):
            if self.did_risky:
                # never replay a flow that already committed something
                self._fail("unsafe_restart", step, "restart after %s" % c["name"], "a risky step already ran")
            self.recoveries.append("%s: restarted flow" % c["name"])
            self.log.event("recover", name=c["name"], action="restart")
            return True
        if "click" in rec:
            from .artifact import Target
            self.s.click(Target(**rec["click"]))
            self.recoveries.append("%s: clicked %s" % (c["name"], rec["click"]["name"]))
            self.log.event("recover", name=c["name"], action="click")
            return False
        self._fail("no_recovery", step, "recovery for %s" % c["name"], "profile has none")

    # --- checkpoint and outputs -------------------------------------------------
    def _checkpoint_met(self) -> bool:
        cp = self.cap.checkpoint
        return self.s.has_text(cp.frame, cp.text)

    def _await_checkpoint(self) -> None:
        deadline = time.time() + CHECKPOINT_WAIT_S
        while True:
            if self._checkpoint_met():
                self.log.event("checkpoint", ok=True, text=self.cap.checkpoint.text)
                return
            if self._handle_conditions(None) is True:
                raise _Stop({"status": "failure", "error": {"kind": "restart_at_checkpoint",
                             "observed": "session lost after last step"}})
            if time.time() > deadline:
                break
            self.s.page.wait_for_timeout(300)
        self._stuck(None, "checkpoint %r not reached" % self.cap.checkpoint.text,
                    self.s.text(self.cap.checkpoint.frame)[:300])
        if not self._checkpoint_met():
            self._fail("checkpoint_not_met", None, self.cap.checkpoint.text,
                       self.s.text(self.cap.checkpoint.frame)[:300])
        self.log.event("checkpoint", ok=True, text=self.cap.checkpoint.text, after_handoff=True)

    def _read_outputs(self) -> Dict[str, str]:
        out = {}
        for name, spec in self.cap.outputs.items():
            v = self.s.read(spec.read)
            if v is None:
                self._fail("output_missing", None, "value for %s" % name, "nothing at %s" % spec.read.model_dump(exclude_none=True))
            if spec.type == "money":
                if not _MONEY.match(v):
                    self._fail("output_malformed", None, "money for %s" % name, v)
                v = v.replace("$", "").replace(",", "")
            out[name] = v
        return out

    # --- stuck / fail --------------------------------------------------------------
    def _stuck(self, step: Optional[Step], why: str, observed: str) -> None:
        if not self.escalate or len(self.handoffs) >= 2:
            self._fail("stuck", step, why, observed)
        ctx = {"capability": self.cap.id, "version": self.cap.version,
               "step": step.id if step else "checkpoint",
               "step_note": step.note if step else "", "expected": why}
        h = request_human(self.s, self.log, why, ctx)
        self.handoffs.append(h)
        if h["timed_out"]:
            self._fail("handoff_timeout", step, why, observed)

    def _fail(self, kind: str, step: Optional[Step], expected: str, observed: str) -> None:
        shot = self.log.screenshot(self.s, "failure")
        raise _Stop({"status": "failure", "error": {
            "kind": kind, "step": step.id if step else "checkpoint",
            "expected": expected, "observed": observed, "screenshot": shot}})

    @staticmethod
    def _wait(pred, timeout: float) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if pred():
                return True
            time.sleep(0.25)
        return False
