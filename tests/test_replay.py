"""Replay against the mock app, no LLM. Covers each branch of the result contract."""
import json
import threading
from pathlib import Path

import pytest

from cua.artifact import Capability
from cua.policy import Policy
from cua.replay import Replayer
from cua.runlog import RunLog
from cua.surface import WebSurface
from mockapp import server

PORT = 8198
BASE = "http://127.0.0.1:%d" % PORT
ROOT = Path(__file__).resolve().parent.parent
PARAMS = {"member_number": "12345", "username": "teller1", "password": "demo-pass-1"}


@pytest.fixture(scope="module")
def app():
    httpd = server.serve(PORT)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield
    httpd.shutdown()


@pytest.fixture
def profile():
    p = json.loads((ROOT / "targets/cucore/profile.json").read_text())
    p["policy"]["allowed_origins"] = [BASE]
    return p


def run(profile, tmp_path, params=PARAMS, faults=(), cap=None):
    server.FAULTS.clear()
    server.FAULTS.update(faults)
    cap = cap or Capability.load(ROOT / "tests/fixture_capability.json")
    log = RunLog(tmp_path, "replay", secrets=[params.get("password", "")])
    s = WebSurface(BASE, Policy.from_profile(profile)).start()
    try:
        return Replayer(cap, profile, s, log).run(dict(params)), log
    finally:
        log.close()
        s.close()


def test_success(app, profile, tmp_path):
    r, log = run(profile, tmp_path)
    assert r.status == "success"
    assert r.outputs == {"savings_balance": "1234.56", "member_name": "JANE Q SAMPLE"}
    text = (log.dir / "log.jsonl").read_text()
    assert "demo-pass-1" not in text          # secret never logged
    assert "JANE Q SAMPLE" not in text        # pii output masked in logs


def test_not_found_is_business_outcome(app, profile, tmp_path):
    r, _ = run(profile, tmp_path, dict(PARAMS, member_number="99999"))
    assert r.status == "business_outcome" and r.outcome == "member_not_found"


def test_interstitial_recovered(app, profile, tmp_path):
    r, _ = run(profile, tmp_path, faults={"notice"})
    assert r.status == "success" and r.recoveries == ["system_notice: clicked Continue"]


def test_session_expiry_restarts(app, profile, tmp_path):
    r, _ = run(profile, tmp_path, faults={"timeout"})
    assert r.status == "success" and "session_expired: restarted flow" in r.recoveries


def test_system_error_is_hard_failure(app, profile, tmp_path):
    r, log = run(profile, tmp_path, faults={"error"})
    assert r.status == "failure" and r.error["kind"] == "system_error"
    assert (log.dir / r.error["screenshot"]).exists()


def test_unknown_screen_fails_when_not_escalating(app, profile, tmp_path):
    r, _ = run(profile, tmp_path, faults={"override"})
    assert r.status == "failure" and r.error["kind"] == "stuck"


def test_wrong_password_is_failure(app, profile, tmp_path):
    r, _ = run(profile, tmp_path, dict(PARAMS, password="nope"))
    assert r.status == "failure" and r.error["kind"] == "bad_signon"


def test_draft_not_replayed_unattended(app, profile, tmp_path):
    cap = Capability.load(ROOT / "tests/fixture_capability.json")
    cap.status = "draft"
    r, _ = run(profile, tmp_path, cap=cap)
    assert r.error["kind"] == "not_approved"


def test_risky_step_needs_approval(app, profile, tmp_path):
    cap = Capability.load(ROOT / "tests/fixture_capability.json")
    cap.steps[-1].risky = True
    r, _ = run(profile, tmp_path, cap=cap)
    assert r.error["kind"] == "approval_required"
