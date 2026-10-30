"""
Human-in-the-loop handoff on the live session.

Control model: exactly one holder at a time, recorded in <run>/control.json.
  automation -> human   : automation calls request_human(); it stops acting and
                          only polls. The browser stays open on the same page.
  human -> automation   : the operator runs `cua operator resume <run>`.
The operator works the same browser: either the headed window (--headed) or by
attaching over CDP (`cua operator click/fill`, a stand-in for a real console).
While a human holds control, every click/change in the page is captured and
written to the run log.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List

from .policy import redact

HANDOFF_TIMEOUT_S = 15 * 60


def control_path(run_dir: Path) -> Path:
    return Path(run_dir) / "control.json"


def read_control(run_dir: Path) -> dict:
    p = control_path(run_dir)
    if not p.exists():
        return {"holder": "automation"}
    return json.loads(p.read_text())


def set_control(run_dir: Path, holder: str, **extra) -> None:
    rec = {"holder": holder, "since": time.strftime("%Y-%m-%dT%H:%M:%S")}
    rec.update(extra)
    control_path(run_dir).write_text(json.dumps(rec, indent=2) + "\n")


class NotInControl(Exception):
    pass


def assert_automation(run_dir: Path) -> None:
    if read_control(run_dir).get("holder") != "automation":
        raise NotInControl("a human holds this session")


def request_human(surface, log, reason: str, context: dict) -> dict:
    """Pause automation, hand the live session to an operator, wait for it back.
    Returns {"note": ..., "actions": [...], "timed_out": bool}."""
    shot = log.screenshot(surface, "handoff")
    main_text = surface.text("main")
    request = {
        "run": log.id,
        "reason": reason,
        "context": context,
        "url": surface.page.url,
        "screen_text": main_text[:800],
        "screenshot": shot,
        "how_to_take_over": {
            "headed": "use the open browser window" if not surface.headless else None,
            "cdp": "http://127.0.0.1:%s" % surface.cdp_port if surface.cdp_port else None,
            "resume": "python -m cua operator resume %s --note '...'" % log.id,
        },
    }
    log.write_json("intervention.json", request)
    log.event("handoff_requested", reason=reason, context=context, screenshot=shot)

    actions: List[dict] = []
    surface.on_human_event = lambda e: actions.append(e)
    set_control(log.dir, "human", reason=reason)
    print("\n*** needs a human: %s" % reason)
    print("    run: %s" % log.id)
    print("    intervention: %s" % (log.dir / "intervention.json"))
    print("    resume with: python -m cua operator resume %s --note '...'\n" % log.id)

    started = time.time()
    timed_out = False
    # Polling through page.wait_for_timeout keeps Playwright's event loop
    # turning, which is what delivers the human's events to us.
    while read_control(log.dir).get("holder") != "automation":
        if time.time() - started > HANDOFF_TIMEOUT_S:
            timed_out = True
            set_control(log.dir, "automation", note="handoff timed out")
            break
        surface.page.wait_for_timeout(500)
    surface.on_human_event = None

    ctl = read_control(log.dir)
    clean = [{k: redact(v, log.secrets) if isinstance(v, str) else v for k, v in a.items()} for a in actions]
    log.event("handoff_returned", note=ctl.get("note", ""), human_actions=clean,
              seconds=round(time.time() - started, 1), timed_out=timed_out)
    log.screenshot(surface, "after-handoff")
    return {"note": ctl.get("note", ""), "actions": clean, "timed_out": timed_out}
