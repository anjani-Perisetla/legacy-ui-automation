"""
Command line.

  python -m cua discover --goal "..." --input member_number=12345 --secret password=env:CUCORE_PASSWORD ...
  python -m cua replay capabilities/member_savings_balance.json --input member_number=20001 ...
  python -m cua approve capabilities/member_savings_balance.json
  python -m cua operator list | show RUN | click RUN ... | fill RUN ... | resume RUN --note ...
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .artifact import Capability, Target
from .handoff import read_control, set_control
from .policy import Policy, mask_value
from .runlog import RunLog

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"


def load_profile(app: str) -> dict:
    return json.loads((ROOT / "targets" / app / "profile.json").read_text())


def parse_kv(items, secret=False):
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        if v.startswith("env:"):
            name = v[4:]
            v = os.environ.get(name, "")
            if not v:
                sys.exit("environment variable %s is not set" % name)
        out[k] = v
    return out


def make_surface(profile, args):
    from .surface import WebSurface
    return WebSurface(args.base_url, Policy.from_profile(profile), headless=not args.headed,
                      cdp_port=args.cdp_port).start()


def cmd_discover(args):
    from .agent import ClaudeDecider, Discovery, FileDecider
    profile = load_profile(args.app)
    inputs = parse_kv(args.input)
    secrets = parse_kv(args.secret)
    inputs.update(secrets)
    log = RunLog(RUNS, "discover", secrets=secrets.values())
    s = make_surface(profile, args)
    try:
        decider = FileDecider(log) if args.decider == "file" else ClaudeDecider(log)
        cap = Discovery(s, Policy.from_profile(profile), log, args.app, decider).run(
            args.goal, args.entry, inputs, list(secrets), args.capability_id)
        if cap is None:
            log.screenshot(s, "discovery-failed")
            print("discovery did not finish, see %s" % log.dir)
            return 1
        vm = profile.get("version_marker")
        if vm and s.has_text(vm["frame"], vm["text"]):
            cap.app_version = vm["text"]
        # every business outcome the app can show is a candidate; the reviewer trims
        cap.outcomes = [c["name"] for c in profile["conditions"] if c["kind"] == "business"]
        out = Path(args.out or ROOT / "capabilities" / ("%s.json" % args.capability_id))
        out.parent.mkdir(parents=True, exist_ok=True)
        cap.save(out)
        log.event("artifact_saved", path=str(out.relative_to(ROOT)))
        print("saved %s (run %s)" % (out, log.id))
        return 0
    finally:
        log.close()
        s.close()


def cmd_replay(args):
    from .replay import Replayer
    cap = Capability.load(args.artifact)
    profile = load_profile(cap.app)
    params = parse_kv(args.input)
    secret_names = [k for k, v in cap.inputs.items() if v.secret]
    params.update(parse_kv(args.secret))
    log = RunLog(RUNS, "replay", secrets=[params.get(k, "") for k in secret_names])
    if args.fault:
        # test hook: inject a runtime condition into the mock app
        import urllib.request
        for f in args.fault:
            urllib.request.urlopen(args.base_url + "/_test/fault?set=" + f).read()
        log.event("fault_injected", faults=args.fault)
    s = make_surface(profile, args)
    try:
        r = Replayer(cap, profile, s, log, escalate=args.escalate, approvals=args.approve or [],
                     allow_draft=args.allow_draft).run(params)
        saved = r.to_dict()  # what goes to disk has pii outputs masked
        saved["outputs"] = {k: (mask_value(v) if cap.outputs[k].pii else v) for k, v in r.outputs.items()}
        log.write_json("result.json", saved)
        print(json.dumps(r.to_dict(), indent=2))
        return 0 if r.status != "failure" else 2
    finally:
        log.close()
        s.close()


def cmd_approve(args):
    cap = Capability.load(args.artifact)
    cap.status = "approved"
    cap.save(args.artifact)
    print("%s v%d approved" % (cap.id, cap.version))


# --- operator console (mock) --------------------------------------------------
# A real console would stream the session to a browser. Here the operator
# attaches to the same live browser over CDP and acts by label, which is enough
# to prove the control transfer and the capture of what the human did.

def _run_dir(run):
    d = RUNS / run
    if not d.exists():
        sys.exit("no such run %s" % run)
    return d


def _attach(run_dir):
    from playwright.sync_api import sync_playwright
    req = json.loads((run_dir / "intervention.json").read_text())
    cdp = req["how_to_take_over"]["cdp"]
    if not cdp:
        sys.exit("session was not started with --cdp-port")
    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(cdp)
    page = browser.contexts[0].pages[0]
    return pw, page


def cmd_operator(args):
    if args.op == "list":
        for d in sorted(RUNS.glob("*")):
            ctl = read_control(d)
            if ctl.get("holder") == "human":
                print("%s  waiting since %s: %s" % (d.name, ctl.get("since"), ctl.get("reason")))
        return 0
    run_dir = _run_dir(args.run)
    if args.op == "show":
        print((run_dir / "intervention.json").read_text())
        print("control:", json.dumps(read_control(run_dir)))
        return 0
    if read_control(run_dir).get("holder") != "human":
        sys.exit("automation holds this session; nothing to do")
    if args.op == "resume":
        set_control(run_dir, "automation", note=args.note or "")
        print("control returned to automation")
        return 0
    from .surface import DOM_JS
    pw, page = _attach(run_dir)
    try:
        frame = page.frame(name=args.frame) if args.frame else page.main_frame
        t = Target(frame=args.frame or "", role=args.role, name=args.name or "", near=args.near or "")
        res = frame.evaluate(DOM_JS, {"op": "find", "target": t.model_dump()})
        if not res or res["ambiguous"]:
            sys.exit("control not found: %s" % res)
        h = frame.locator("[data-cua-hit]").element_handle()
        h.evaluate("e => e.removeAttribute('data-cua-hit')")
        if args.op == "click":
            h.click()
        else:
            h.fill(args.value)
        page.wait_for_timeout(500)
        print("done")
    finally:
        pw.stop()  # detach only; the browser belongs to the automation process
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cua")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def surface_args(p):
        p.add_argument("--base-url", default="http://127.0.0.1:8099")
        p.add_argument("--headed", action="store_true")
        p.add_argument("--cdp-port", type=int, default=9333,
                       help="exposes the live browser so an operator can take over")

    d = sub.add_parser("discover")
    d.add_argument("--goal", required=True)
    d.add_argument("--app", default="cucore")
    d.add_argument("--entry", default="/")
    d.add_argument("--capability-id", required=True)
    d.add_argument("--input", action="append")
    d.add_argument("--secret", action="append", help="name=env:VAR")
    d.add_argument("--out")
    d.add_argument("--decider", choices=["claude", "file"], default="claude",
                   help="claude: Claude API (needs ANTHROPIC_API_KEY); file: answers come from files")
    surface_args(d)

    r = sub.add_parser("replay")
    r.add_argument("artifact")
    r.add_argument("--input", action="append")
    r.add_argument("--secret", action="append", help="name=env:VAR")
    r.add_argument("--escalate", action="store_true", help="hand off to a human instead of failing when stuck")
    r.add_argument("--approve", action="append", help="step id of a risky step the caller approves")
    r.add_argument("--allow-draft", action="store_true")
    r.add_argument("--fault", action="append", help="mock app only: notice|timeout|slow|error|override")
    surface_args(r)

    a = sub.add_parser("approve")
    a.add_argument("artifact")

    o = sub.add_parser("operator")
    o.add_argument("op", choices=["list", "show", "click", "fill", "resume"])
    o.add_argument("run", nargs="?")
    o.add_argument("--frame", default="main")
    o.add_argument("--role", default="button")
    o.add_argument("--name")
    o.add_argument("--near")
    o.add_argument("--value")
    o.add_argument("--note")

    args = ap.parse_args(argv)
    return {"discover": cmd_discover, "replay": cmd_replay, "approve": cmd_approve,
            "operator": cmd_operator}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
