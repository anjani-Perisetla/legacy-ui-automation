# cua: record-once / replay-many automation for legacy banking screens

An LLM works out how to do a task on a back-office app once. The run is saved as a
typed capability (JSON). After that the capability is replayed deterministically,
with inputs, outputs, error handling and a human handoff, and no model in the loop.

The target is a small mock "core banking" app in `mockapp/` that is deliberately
hostile to automation: framesets, table layouts, inputs named `f1`/`f2`, no ids, no
labels, no test ids. It also has a test hook to inject runtime conditions
(interstitial notice, session expiry, slow page, system error, an unexpected
supervisor-override screen).

Design write-up: [REPORT.md](REPORT.md). Evidence of real runs: [evidence/](evidence/).

## Setup

Python 3.9+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

Discovery needs an Anthropic API key (`ANTHROPIC_API_KEY`). Replay, the tests and the
handoff demo do not.

The mock app's test operator is `teller1` / `demo-pass-1`. The password is passed by
environment variable so it never appears on a command line, in a log or in an artifact:

```bash
export CUCORE_PASSWORD=demo-pass-1
```

Start the mock app (leave it running in its own terminal):

```bash
python -m mockapp.server --port 8099
```

## Demo path

1. Discovery (LLM drives the app, artifact is written to `capabilities/`):

```bash
python -m cua discover --capability-id member_savings_balance --goal "Sign on, look up member {{member_number}} and read their share savings balance and name" --input username=teller1 --input member_number=12345 --secret password=env:CUCORE_PASSWORD
```

2. Review the artifact, then approve it. Draft capabilities refuse unattended replay.

```bash
python -m cua approve capabilities/member_savings_balance.json
```

3. Replay with different inputs:

```bash
python -m cua replay capabilities/member_savings_balance.json --input username=teller1 --input member_number=20001 --secret password=env:CUCORE_PASSWORD
```

4. Replay that hits a business outcome (no such member), and runtime conditions:

```bash
python -m cua replay capabilities/member_savings_balance.json --input username=teller1 --input member_number=99999 --secret password=env:CUCORE_PASSWORD
```

```bash
python -m cua replay capabilities/member_savings_balance.json --input username=teller1 --input member_number=12345 --secret password=env:CUCORE_PASSWORD --fault notice --fault timeout
```

```bash
python -m cua replay capabilities/member_savings_balance.json --input username=teller1 --input member_number=12345 --secret password=env:CUCORE_PASSWORD --fault error
```

5. Human handoff. The replay hits a screen it has never seen and, with `--escalate`,
hands the live browser to an operator instead of failing:

```bash
python -m cua replay capabilities/member_savings_balance.json --input username=teller1 --input member_number=12345 --secret password=env:CUCORE_PASSWORD --fault override --escalate
```

In a second terminal, act as the operator on the same session:

```bash
python -m cua operator list
```

```bash
python -m cua operator fill <run-id> --role textbox --near Code --value 4321
```

```bash
python -m cua operator click <run-id> --name Approve
```

```bash
python -m cua operator resume <run-id> --note "supervisor override entered"
```

Add `--headed` to the replay to get a visible browser window; a person can then just
use that window and run `resume` when done.

Every run writes `runs/<run-id>/` with `log.jsonl`, screenshots and `result.json`.

## Without live services

```bash
python -m pytest -q
```

The tests start the mock app in-process and replay a hand-written fixture
(`tests/fixture_capability.json`), so they need neither an API key nor a running server.

## Layout

```
cua/surface.py    perceive/act on a web surface (Playwright + dom.js)
cua/dom.js        the "operator view" of a screen: controls by role, name, nearby label
cua/artifact.py   capability schema (pydantic)
cua/agent.py      LLM discovery loop and recorder
cua/replay.py     deterministic replay and result contract
cua/handoff.py    control transfer to a human on the live session
cua/policy.py     allowlist, risky actions, redaction
cua/runlog.py     redacted JSONL evidence
targets/cucore/profile.json   per-app knowledge: policy, known screens and how to handle them
mockapp/server.py the legacy app stand-in
```
