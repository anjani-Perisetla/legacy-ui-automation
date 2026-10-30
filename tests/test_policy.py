import pytest

from cua.policy import Policy, PolicyViolation, mask_value, redact
from cua.replay import render, validate_inputs
from cua.artifact import Capability
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixture_capability.json"


def policy():
    return Policy(allowed_origins=["http://127.0.0.1:8099"], allowed_actions=["click", "fill"],
                  risky_controls=["Post Transfer", "Approve"], blocked_paths=["/_test/"])


def test_origin_allowlist():
    p = policy()
    p.check_url("http://127.0.0.1:8099/inquiry")
    with pytest.raises(PolicyViolation):
        p.check_url("http://evil.example.com/")
    with pytest.raises(PolicyViolation):
        p.check_url("http://127.0.0.1:8099/_test/fault?set=error")


def test_action_allowlist():
    with pytest.raises(PolicyViolation):
        policy().check_action("download")


def test_risky_controls():
    p = policy()
    assert p.is_risky("Post Transfer")
    assert p.is_risky("approve")
    assert not p.is_risky("Go")


def test_redaction():
    s = redact("SSN 123-45-6789 acct 000123456789 dob 04/12/1981 pw hunter2", secrets=["hunter2"])
    assert "123-45" not in s and "6789" in s
    assert "000123" not in s
    assert "04/12" not in s
    assert "hunter2" not in s and "[SECRET]" in s
    assert mask_value("JANE") == "J**E"


def test_inputs_validated_before_touching_ui():
    cap = Capability.load(FIXTURE)
    ok = {"member_number": "12345", "username": "u", "password": "p"}
    assert validate_inputs(cap, ok) is None
    assert "does not match" in validate_inputs(cap, dict(ok, member_number="12a"))
    assert "missing" in validate_inputs(cap, {"member_number": "1"})
    assert "unknown" in validate_inputs(cap, dict(ok, extra="x"))


def test_render_templates():
    assert render("{{a}}-{{b}}", {"a": "1", "b": "2"}) == "1-2"
