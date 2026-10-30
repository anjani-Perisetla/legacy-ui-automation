"""
The web surface: how we perceive and act on a browser-hosted app.

Everything above this module (agent, replay, handoff) talks in Targets and
Reads, never in Playwright calls. A desktop surface would implement the same
methods against the Windows UIA / macOS AX tree. See REPORT.md section 4.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable, List, Optional

from playwright.sync_api import Frame, sync_playwright

from .artifact import Read, Target
from .policy import Policy, PolicyViolation

DOM_JS = (Path(__file__).parent / "dom.js").read_text()
_SENSITIVE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b|\b\d{2}/\d{2}/\d{4}\b|\b\d{9,19}\b")

# Recorder for human actions. Listens in every frame, reports through a binding;
# Python decides whether to keep the events (only while a human holds control).
CAPTURE_JS = """
(() => {
  const describe = %s;
  const send = (type, el, value) => {
    try {
      const d = describe({op: "describe", el});
      window.cuaHumanEvent({type, frame: window.name || "", value: d.secret ? "[SECRET]" : value, ...d});
    } catch (e) {}
  };
  document.addEventListener("click", (e) => send("click", e.target, null), true);
  document.addEventListener("change", (e) => send("fill", e.target, e.target.value), true);
})();
""" % DOM_JS


class TargetNotFound(Exception):
    pass


class WebSurface:
    def __init__(self, base_url: str, policy: Policy, headless: bool = True,
                 cdp_port: Optional[int] = None):
        self.base_url = base_url.rstrip("/")
        self.policy = policy
        self.headless = headless
        self.cdp_port = cdp_port
        self.on_human_event: Optional[Callable[[dict], None]] = None
        self.blocked: List[str] = []

    # --- lifecycle ---------------------------------------------------------
    def start(self) -> "WebSurface":
        self._pw = sync_playwright().start()
        args = ["--remote-debugging-port=%d" % self.cdp_port] if self.cdp_port else []
        self.browser = self._pw.chromium.launch(headless=self.headless, args=args)
        self.context = self.browser.new_context(viewport={"width": 1100, "height": 700})
        self.context.expose_binding("cuaHumanEvent", self._human_event)
        self.context.add_init_script(CAPTURE_JS)
        # Network-level allowlist: requests outside the allowed origins never
        # leave the browser, whoever triggers them (agent, replay or a link).
        self.context.route("**/*", self._route)
        self.page = self.context.new_page()
        return self

    def close(self) -> None:
        try:
            self.browser.close()
        finally:
            self._pw.stop()

    def _route(self, route, request):
        try:
            self.policy.check_url(request.url)
        except PolicyViolation as e:
            self.blocked.append(str(e))
            return route.abort()
        return route.continue_()

    def _human_event(self, source, event):
        if self.on_human_event:
            self.on_human_event(event)

    # --- frames ------------------------------------------------------------
    def frame(self, name: str) -> Frame:
        if not name:
            return self.page.main_frame
        f = self.page.frame(name=name)
        if f is None:
            raise TargetNotFound("frame %r not present" % name)
        return f

    def frames(self) -> List[Frame]:
        return [f for f in self.page.frames if f.name or f == self.page.main_frame]

    def settle(self, ms: int = 300) -> None:
        for f in self.page.frames:
            try:
                f.wait_for_load_state("load", timeout=10000)
            except Exception:
                pass
        self.page.wait_for_timeout(ms)

    # --- perception --------------------------------------------------------
    def observe(self) -> List[dict]:
        """One entry per frame: url, visible text and the controls on it."""
        out = []
        for f in self.frames():
            try:
                scan = f.evaluate(DOM_JS, {"op": "scan"})
            except Exception:
                continue
            scan["frame"] = f.name
            scan["url"] = f.url
            out.append(scan)
        return out

    def text(self, frame: str) -> str:
        try:
            return self.frame(frame).evaluate(DOM_JS, {"op": "scan"})["text"]
        except Exception:
            return ""

    def has_text(self, frame: str, text: str) -> bool:
        return text in self.text(frame)

    def read(self, q: Read) -> Optional[str]:
        return self.frame(q.frame).evaluate(DOM_JS, {"op": "read", "query": q.model_dump()})

    def screenshot(self, path: Path) -> None:
        # evidence screenshots get the same treatment as logs: cells holding an
        # SSN, date of birth or long account number are blacked out
        masks = [f.locator("td", has_text=_SENSITIVE) for f in self.page.frames]
        self.page.screenshot(path=str(path), mask=masks, mask_color="#000000")

    # --- actions -----------------------------------------------------------
    def goto(self, path: str) -> None:
        url = self.base_url + path
        self.policy.check_url(url)
        self.page.goto(url)
        self.settle()

    def locate(self, t: Target, timeout: float = 5.0) -> str:
        """Find exactly one control matching the target. Waits for slow screens.
        Returns the strategy that matched (logged, it is our drift signal)."""
        deadline = time.time() + timeout
        last = "not found"
        while True:
            try:
                res = self.frame(t.frame).evaluate(DOM_JS, {"op": "find", "target": t.model_dump()})
                if res and not res["ambiguous"]:
                    return res["strategy"]
                if res:
                    last = "ambiguous on %s" % res["strategy"]
            except TargetNotFound as e:
                last = str(e)
            except Exception as e:  # frame navigating mid-evaluate
                last = str(e).splitlines()[0]
            if time.time() > deadline:
                raise TargetNotFound("%s %r (near %r) in frame %r: %s" % (t.role, t.name, t.near, t.frame, last))
            self.page.wait_for_timeout(250)

    def click(self, t: Target) -> str:
        strategy = self.locate(t)
        el = self._take_hit(t.frame)
        el.click()
        self.settle()
        return strategy

    def fill(self, t: Target, value: str) -> str:
        strategy = self.locate(t)
        el = self._take_hit(t.frame)
        el.fill(value)
        return strategy

    def _take_hit(self, frame: str):
        h = self.frame(frame).locator("[data-cua-hit]").element_handle()
        h.evaluate("e => e.removeAttribute('data-cua-hit')")
        return h
