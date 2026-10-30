# Evidence

All runs are against the mock app in `mockapp/`, with fake data. Every folder is a
copy of `runs/<run-id>/` as the tool wrote it.

| folder | what happened | result |
|---|---|---|
| `member_savings_balance.json` | the capability recorded by run 01, after review (member_name marked pii, pattern added) and approval | |
| `01-discovery` | discovery on the live app, 7 decisions: sign on, open Member Inquiry, look up 12345, finish | capability saved |
| `02-replay-success` | replay with a different member (20001) | `success`, balance 88.02 |
| `03-replay-member-not-found` | member 99999 | `business_outcome: member_not_found` |
| `04-replay-bad-input` | member number `12a45` | `failure: bad_input`, rejected before the app is touched |
| `05-replay-notice-and-session-timeout` | session killed mid-flow, then a system notice on the inquiry | `success`, both listed under `recoveries` |
| `06-replay-system-error` | host error page | `failure: system_error` with step, observed text, screenshot |
| `07-replay-handoff` | unknown supervisor-override screen, `--escalate` on | handed to an operator on the same browser, code entered, resumed, `success` |

## How the discovery decisions were made

No model API key was available on the machine this was built on, so run 01 used the
`--decider file` mode instead of `--decider claude`. In that mode the loop writes each
turn's observation to `decider/turn-NN.prompt.txt` and reads the next action from
`decider/turn-NN.action.json`. The actions were written by an LLM (Claude, working
through a coding-assistant session) reading only those prompt files, i.e. the same
redacted view and the same tools the API path sends. Everything else (perception,
policy checks, recording, the artifact) is the normal code path. With
`ANTHROPIC_API_KEY` set, the same command with `--decider claude` calls the API instead.

## What the handoff shows

`07-replay-handoff/intervention.json` is the request an operator gets. The operator
then ran, from a second terminal, against the same live browser:

```
python -m cua operator fill <run> --role textbox --near Code --value ****
python -m cua operator click <run> --name Approve
python -m cua operator resume <run> --note "supervisor override entered by branch ops"
```

The captured human actions are in `log.jsonl` (`handoff_returned`) and in
`result.json` under `handoffs`; the code itself appears only as `[SECRET]`.

## Redaction notes

- The operator password and the supervisor code never appear in any file.
- SSN and date of birth are redacted in prompts and logs and blacked out in screenshots.
- `member_name` is marked pii, so it is masked in `result.json` and logs. It does appear
  in the discovery prompt for turn 07 and in the after-handoff screenshot, because names
  are not caught by pattern redaction. That limit is discussed in REPORT.md, Safety.
