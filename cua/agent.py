"""
Discovery: an LLM works out how to do a goal on the live app, and we record
what it did as a Capability.

The model sees a redacted text rendering of each frame and a numbered list of
controls (role, visible name, label next to it). It never sees raw HTML and
never sees secret values: it types "{{password}}" and we substitute.
Every action passes the policy check before it touches the browser.
"""
from __future__ import annotations

import json
import re
import time
from typing import Dict, List, Optional

import anthropic

from .artifact import Capability, Checkpoint, InputSpec, OutputSpec, Read, Step, Target
from .handoff import assert_automation, request_human, set_control
from .policy import PolicyViolation, mask_value, redact
from .surface import TargetNotFound

MODEL = "claude-opus-5-5"
MAX_STEPS = 25

SYSTEM = """You operate a back-office banking application through its screen, the way a teller would.
Each turn you get the current screen: the text of every frame and a numbered list of controls.
Do one action per turn using the tools.

Rules:
- Refer to controls by frame and index exactly as listed.
- When typing a task input, type the placeholder, e.g. {{member_number}}, never the literal value.
  Secret inputs (like passwords) are only available as placeholders.
- Do not guess. If the screen is not what you expect or you cannot continue safely, call ask_human.
- Some actions (posting, approving, deleting) are blocked by policy. If you need one, call ask_human.
- When the goal is met, call finish with a short text that proves you are on the right screen and
  how to read each requested value from it."""

TOOLS = [
    {
        "name": "act",
        "description": "Click a control, or type into a text box.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["click", "fill"]},
                "frame": {"type": "string"},
                "index": {"type": "integer"},
                "value": {"type": "string", "description": "for fill; use {{input}} placeholders"},
                "reason": {"type": "string", "description": "one line, why"},
            },
            "required": ["action", "frame", "index", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "finish",
        "description": "The goal is met. Describe the checkpoint and where each output is read from.",
        "input_schema": {
            "type": "object",
            "properties": {
                "checkpoint_frame": {"type": "string"},
                "checkpoint_text": {"type": "string", "description": "short text that only appears on the goal screen"},
                "outputs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "type": {"type": "string", "enum": ["string", "money"]},
                            "frame": {"type": "string"},
                            "label": {"type": "string", "description": "cell text left of the value"},
                            "row": {"type": "string", "description": "a cell text in the table row"},
                            "column": {"type": "string", "description": "the header text of the column"},
                            "description": {"type": "string"},
                        },
                        "required": ["name", "type", "frame", "description"],
                        "additionalProperties": False,
                    },
                },
                "input_descriptions": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            "required": ["checkpoint_frame", "checkpoint_text", "outputs"],
            "additionalProperties": False,
        },
    },
    {
        "name": "ask_human",
        "description": "Stop and hand the live session to a human operator.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
            "additionalProperties": False,
        },
    },
]


def render_screen(frames: List[dict], secrets) -> str:
    parts = []
    for f in frames:
        if not f["text"] and not f["controls"]:
            continue
        parts.append("=== frame %r  %s" % (f["frame"], f["url"].split("?")[0]))
        parts.append(redact(f["text"][:1500], secrets))
        for i, c in enumerate(f["controls"]):
            parts.append("  [%d] %s %r%s" % (i, c["role"], c["name"],
                                             (" label=%r" % c["near"]) if c["near"] else ""))
    return "\n".join(parts)


class ClaudeDecider:
    """Calls the Claude API. Keeps the conversation, one tool call per turn."""
    name = MODEL

    def __init__(self, log, client: Optional[anthropic.Anthropic] = None):
        self.log = log
        self.client = client or anthropic.Anthropic()
        self.messages: list = []
        self._pending = None

    def decide(self, observation: str, last_result: Optional[str]):
        if self._pending is None:
            self.messages.append({"role": "user", "content": observation})
        else:
            self.messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": self._pending, "content": last_result},
                {"type": "text", "text": observation},
            ]})
        for _ in range(3):
            resp = self.client.messages.create(
                model=MODEL, max_tokens=16000, system=SYSTEM, tools=TOOLS,
                tool_choice={"type": "auto", "disable_parallel_tool_use": True},
                output_config={"effort": "medium"},
                messages=self.messages,
            )
            self.log.event("llm", stop_reason=resp.stop_reason,
                           input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens,
                           text=" ".join(b.text for b in resp.content if b.type == "text")[:500])
            self.messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "refusal":
                return None
            call = next((b for b in resp.content if b.type == "tool_use"), None)
            if call:
                self._pending = call.id
                return call.name, call.input
            self.messages.append({"role": "user", "content": "Use one of the tools."})
        return None


class FileDecider:
    """Decisions come from outside the process through files, for when there is
    no API access from this machine. Each turn writes turn-NN.prompt.txt (system
    prompt, tools, observation) and waits for turn-NN.action.json containing
    {"tool": ..., "input": {...}}. Whoever answers sees only what an API model
    would see, so the run exercises the same loop, policy and recorder."""
    name = "file"

    def __init__(self, log, timeout_s: int = 1800):
        self.log = log
        self.turn = 0
        self.timeout_s = timeout_s
        (log.dir / "decider").mkdir(exist_ok=True)
        (log.dir / "decider" / "system.txt").write_text(SYSTEM + "\n\nTOOLS:\n" + json.dumps(TOOLS, indent=2))

    def decide(self, observation: str, last_result: Optional[str]):
        self.turn += 1
        d = self.log.dir / "decider"
        body = observation if last_result is None else "Result of your last action: %s\n\n%s" % (last_result, observation)
        (d / ("turn-%02d.prompt.txt" % self.turn)).write_text(body + "\n")
        answer = d / ("turn-%02d.action.json" % self.turn)
        print("waiting for %s" % answer, flush=True)
        end = time.time() + self.timeout_s
        while not answer.exists():
            if time.time() > end:
                return None
            time.sleep(0.5)
        time.sleep(0.2)
        a = json.loads(answer.read_text())
        return a["tool"], a["input"]


class Discovery:
    def __init__(self, surface, policy, log, app_id: str, decider=None):
        self.s = surface
        self.policy = policy
        self.log = log
        self.app_id = app_id
        self.decider = decider or ClaudeDecider(log)
        self.steps: List[Step] = []

    def run(self, goal: str, entry: str, inputs: Dict[str, str], secret_inputs: List[str],
            capability_id: str) -> Optional[Capability]:
        secrets = [inputs[k] for k in secret_inputs]
        set_control(self.log.dir, "automation")
        self.log.event("discovery_start", goal=goal, entry=entry, decider=self.decider.name,
                       inputs={k: ("[SECRET]" if k in secret_inputs else v) for k, v in inputs.items()})

        self.s.goto(entry)
        self.steps.append(Step(id="s1", action="goto", value=entry, note="open the app"))

        input_list = ", ".join("{{%s}}%s" % (k, " (secret)" if k in secret_inputs else "") for k in inputs)
        observation = "Goal: %s\nAvailable inputs: %s\n\nCurrent screen:\n%s" % (
            goal, input_list, render_screen(self.s.observe(), secrets))
        result = None

        for turn in range(MAX_STEPS):
            decision = self.decider.decide(observation, result)
            if decision is None:
                self.log.event("discovery_end", ok=False, reason="no decision (refused or timed out)")
                return None
            tool, args = decision
            self.log.event("decision", turn=turn, tool=tool, input=args)
            if tool == "finish":
                cap, problem = self._build(args, goal, entry, inputs, secret_inputs, capability_id)
                if cap:
                    self.log.event("discovery_end", ok=True, steps=len(cap.steps))
                    return cap
                result = "finish rejected: %s. Fix it or keep going." % problem
            elif tool == "ask_human":
                h = request_human(self.s, self.log, args["reason"], {"goal": goal, "step": len(self.steps)})
                result = "A human operated the screen and handed back. Note: %r. Actions: %s" % (
                    h["note"], h["actions"])
                # what the human did is not part of the capability; if it was
                # needed, the capability needs another discovery or a review.
            elif tool == "act":
                result = self._act(args, inputs, secrets)
            else:
                result = "error: unknown tool %s" % tool
            self.log.event("result", turn=turn, result=result)
            observation = "Current screen:\n" + render_screen(self.s.observe(), secrets)

        self.log.event("discovery_end", ok=False, reason="max steps")
        return None

    def _act(self, a: dict, inputs: Dict[str, str], secrets) -> str:
        frames = {f["frame"]: f for f in self.s.observe()}
        f = frames.get(a["frame"])
        if not f or not (0 <= a["index"] < len(f["controls"])):
            return "error: no control %s#%s" % (a["frame"], a["index"])
        c = f["controls"][a["index"]]
        target = Target(frame=a["frame"], role=c["role"], name=c["name"], near=c["near"], css=c["css"])
        try:
            assert_automation(self.log.dir)
            self.policy.check_action(a["action"])
            risky = a["action"] == "click" and self.policy.is_risky(c["name"])
            if risky:
                self.log.event("blocked", reason="risky control", control=c["name"])
                return "blocked by policy: %r is a risky action. Use ask_human if it is required." % c["name"]
            if a["action"] == "fill":
                template = self._templatize(a.get("value", ""), inputs)
                if template is None:
                    return "error: type a placeholder like {{name}}, not a literal input value"
                strategy = self.s.fill(target, re.sub(r"\{\{(\w+)\}\}", lambda m: inputs[m.group(1)], template))
                step = Step(id="s%d" % (len(self.steps) + 1), action="fill", target=target,
                            value=template, note=a["reason"])
            else:
                strategy = self.s.click(target)
                step = Step(id="s%d" % (len(self.steps) + 1), action="click", target=target, note=a["reason"])
        except PolicyViolation as e:
            self.log.event("blocked", reason=str(e))
            return "blocked by policy: %s" % e
        except (TargetNotFound, KeyError) as e:
            return "error: %s" % e
        self.steps.append(step)
        self.log.event("action", step=step.id, action=step.action, target=target.model_dump(),
                       value=step.value, strategy=strategy, reason=a["reason"])
        return "ok"

    @staticmethod
    def _templatize(value: str, inputs: Dict[str, str]) -> Optional[str]:
        if re.search(r"\{\{(\w+)\}\}", value):
            names = re.findall(r"\{\{(\w+)\}\}", value)
            return value if all(n in inputs for n in names) else None
        # model typed a literal; accept only if it is not an input value (a fixed constant)
        for k, v in inputs.items():
            if v and v in value:
                return value.replace(v, "{{%s}}" % k)
        return value

    def _build(self, fin: dict, goal, entry, inputs, secret_inputs, capability_id):
        if not self.s.has_text(fin["checkpoint_frame"], fin["checkpoint_text"]):
            return None, "checkpoint text not on screen"
        outputs = {}
        for o in fin["outputs"]:
            q = Read(frame=o["frame"], label=o.get("label"), row=o.get("row"), column=o.get("column"))
            v = self.s.read(q)
            if v is None:
                return None, "could not read %s with %s" % (o["name"], q.model_dump(exclude_none=True))
            self.log.event("output_check", name=o["name"], found=True, value=mask_value(v))
            outputs[o["name"]] = OutputSpec(type=o["type"], description=o.get("description", ""), read=q)
        descs = fin.get("input_descriptions") or {}
        cap = Capability(
            id=capability_id,
            app=self.app_id,
            description=goal,
            entry=entry,
            inputs={k: InputSpec(description=descs.get(k, ""), secret=k in secret_inputs) for k in inputs},
            outputs=outputs,
            steps=self.steps,
            checkpoint=Checkpoint(frame=fin["checkpoint_frame"], text=fin["checkpoint_text"]),
            recorded_from=self.log.id,
        )
        return cap, None
