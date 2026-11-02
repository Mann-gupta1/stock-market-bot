# Security review: stockbot (paper mode)

Date: 2026-11-02. Reviewer: security-reviewer agent (read-only review).
Scope: `trader/*.py`, `config/*.json`, `bot.py`, `intraday.py`, `markets.py`. Python 3.14.6, `.venv`.
Source files were edited while this review ran (`trader/risk.py`, `trader/engine.py`). Line numbers below were re-taken from the files as they stood at the end of the review, and both probes were run again against that version.

Status labels: CONFIRMED means traced end to end or reproduced with a local probe. PLAUSIBLE means reasoned from the code but not reproduced.

## Findings

| # | Severity | Finding | Evidence | Remediation | Residual limitation |
|---|---|---|---|---|---|
| 1 | HIGH | **NaN/Infinity prices fail open through the quality gate and risk engine.** In Python, every comparison with NaN returns False. So a quote with `bid=ask=NaN` counts as valid, and its NaN mark or equity turns off the position, strategy, gross, daily-loss and drawdown limits. The source is any quote whose JSON payload holds `NaN` or `Infinity`: `json.loads` accepts both by default. That covers the recorded `data/md.db` (mode 644), a future broker feed, or `yahoo()` `last`. CONFIRMED at component level. Not driven through a full `Engine` replay. | `trader/quality.py:39` (no finite check); `trader/model.py:75` `Quote(**json.loads(s))`; `trader/engine.py:160,181` (`if q.bid and q.ask`, and NaN is truthy); `trader/risk.py:166,168,179` and the exposure checks after them; `trader/risk.py:204`. Probe: NaN quote gives `QualityGate.check -> (True, '')`, and BUY 400 gives `Decision(ok=True, reasons=())`. A view with an existing 10,000-share position: finite mark gets `max_position_value, max_strategy_exposure, max_gross_exposure`; NaN mark and equity get only `no quote`. | Reject non-finite values at the boundary: add `math.isfinite` on bid, ask, last and sizes in `QualityGate.check`, and pass `parse_constant` to `json.loads` in `Quote.from_json` so NaN and Infinity raise. As defence in depth, write risk comparisons fail-closed (`not (x <= limit)`), or assert `math.isfinite(view.equity)` and every mark before the checks. | Restart persistence makes it worse: `day_start` and `peak` are re-read from audit JSON (`engine.py:61-66`). A NaN equity written there would disable the loss checks again after a restart. |
| 2 | MEDIUM | **Limits validation accepts NaN, Infinity, floats for int fields, and any type for string fields.** For example, `max_daily_loss: NaN` passes validation and silently disables the check, yet a "valid" hash and version still go into the audit. Two paths reach it. First, startup `load_config` with no auth: anyone who can write `config/` can do this. Second, the authenticated `Controller.set_limits`. CONFIRMED by probe. | `trader/risk.py:43,50,52`. Probe accepted `max_daily_loss=nan`, `max_gross_exposure=inf`, `max_drawdown=nan`, `max_order_qty=1e12`, `version=123`, `base_currency={"x":1}`. | Use `json.loads(raw, parse_constant=_reject)`. Require `math.isfinite`, `type(v) is int` for int fields and `isinstance(v, str)` for str fields. Add sane upper bounds, or a maximum relative change against the current limits. Require `base_currency` and `base_tz` to stay unchanged at runtime. | A limits file is trusted config. Validation narrows mistakes but cannot judge whether a value is wise. |
| 3 | MEDIUM | **The kill switch does not survive a restart.** `Engine.__init__` sets `kill_level = 0` and restores only `day_start` and `peak` from the audit. After a kill, a crash or restart resumes entries with no `resume` call, no auth and no reconcile. CONFIRMED by reading the code. | `trader/engine.py:51`, `trader/engine.py:61-66` (no `kill` branch); `trader/control.py:49,59`. | Rebuild `kill_level` from the audit rows on start: take the last `kill` and `resume` rows, and ignore `DENIED` rows. Only an authenticated `resume` should clear it. | The restore path trusts audit rows that anyone with file write can insert (see #6). |
| 4 | MEDIUM | **Operator auth is not a security boundary.** No CLI or network entry point calls `Controller`: it is reached only from tests. Any code in the process can set `engine.kill_level` or `engine.risk.limits` directly. This auth guards against operator mistakes, not against an attacker. CONFIRMED by grep: no `Controller` use outside `trader/control.py` and `tests/`. | `trader/control.py:27-77`; `trader/engine.py:51`; `trader/risk.py:126` (plain mutable attributes). | Accept this for a single-user paper tool and write the limitation down. Before any remote or multi-user control surface, run controls in a separate process with an authenticated IPC or HTTP endpoint and rate limiting. | A local OS user who can run Python as the owner gets full control in every design. |
| 5 | MEDIUM | **Lockout can block the emergency kill.** After 5 bad tokens there is a 60 s lockout that applies to `kill` as well. Bad attempts made during the lockout trigger another lockout. A typo storm, or another local caller, can therefore stop the operator from halting. Lockout state lives in memory, is per instance, and resets on restart. The lockout itself is not audited as a separate event. CONFIRMED by reading the code. | `trader/control.py:33-43` | Exempt safety-increasing actions (`kill`) from the lockout, or give them a separate lockout. Keep the lockout on `resume` and `set_limits`. Write an audit row `LOCKOUT` with the end time. | None, beyond the in-process caveat in #4. |
| 6 | MEDIUM | **The audit log is not tamper-resistant.** The append-only triggers stop only accidental UPDATE or DELETE through SQL. A probe ran `DROP TRIGGER audit_no_delete` followed by `DELETE FROM audit`, and 0 rows remained. The file can also be deleted, replaced or edited with the sqlite3 CLI. There is no hash chain and no external anchor. Files are mode 644. CONFIRMED by probe. | `trader/store.py:44-47`; probe output `audit rows after trigger drop: (0,)` | Add a hash chain: each row stores `sha256(prev_hash + row)`, checked on start. Ship a copy off-host periodically, or at least to a separate append-only file. Set the DB file to mode 600. | A local owner can always rewrite history. A hash chain only detects edits made after the last exported checkpoint. |
| 7 | LOW | `set_limits` changes `risk.limits` but not `OrbParams.risk_per_trade`, which is fixed at build time. The audited limits version then does not match the sizing the strategy uses. | `trader/cli.py:111`; `trader/control.py:73-75` | Propagate the change to the strategy, or reject a change to `risk_per_trade` at runtime. | none |
| 8 | LOW | `set_limits` reads any path the caller supplies, with no size cap: `/dev/zero` exhausts memory. Its `ValueError` echoes key names from the file. Validation failures after successful auth are not audited, because the exception is raised before the audit write. | `trader/control.py:73`; `trader/risk.py:42-46` | Restrict the path to `config/`, use `read_bytes` with a size cap, and audit `set_limits FAILED`. | Needs a valid token anyway. |
| 9 | LOW | Token storage uses unsalted SHA-256 with no minimum-entropy check. The compare is constant-time (`hmac.compare_digest`). An unset or empty env var fails closed: every action is denied (CONFIRMED by probe). An uppercase hex value in the env var also fails closed. | `trader/control.py:30,35-36` | Document that the token must be 32 or more random bytes, for example from `secrets.token_urlsafe(32)`. Normalise the configured hash with `.strip().lower()` and check it is 64 hex characters at start-up. | Fine for a high-entropy token. |
| 10 | LOW | `actor` is a free-text string the caller supplies, so attribution in the audit is self-declared. | `trader/control.py:41,59,68,76` | Bind the actor to the token: one token per operator, with `actor` taken from a mapping. | none |
| 11 | LOW | The live-mode guard is a config flag only. It works: if `live_enabled` is truthy or `mode != "paper"`, startup refuses, and a missing key fails closed. But no approval artifact exists, and `Engine` or `OMS` can be built directly with any `Broker`, which bypasses `load_config`. Today no live adapter exists. | `trader/cli.py:36-38`; `trader/broker.py:3` | When a live adapter is added, put the guard in the adapter's constructor, for example an env plus a signed approval file, not only in `load_config`. | none today |
| 12 | LOW | `bot.py` labels a market "LIVE-ELIGIBLE" from `results.json`, a writable file. Editing it flips the label. Only the advisory label is affected: `bot.py` places no orders. | `bot.py:131-137` | Accept this, or record a hash of the backtest inputs. | none |
| 13 | INFO | SQL injection: none found. All data values are bound with `?` parameters. The one f-string SQL statement interpolates only the constant `APPEND_ONLY` tuple and literal operation names. | `trader/store.py:46` | Keep it that way. Never interpolate caller input into DDL. | none |
| 14 | INFO | Deserialization: no `pickle`, `yaml`, `eval`, `exec`, `subprocess` or `shell=True` anywhere in scope. JSON only. `Quote(**json.loads(...))` does not type-check values (see #1). | grep over the scope found none; `trader/model.py:75` | Validate types in `Quote.from_json`. | none |
| 15 | INFO | Secrets: none found in source or config. No `.env` file. The token is never logged: `_auth` audits only the action. The only token-like string is the test fixture `tests/qa/test_control.py:13` (`"s3cret-token"`), a test value. | grep for keys, tokens, passwords, AWS and GitHub patterns | none | none |
| 16 | INFO | Logging: no `logging` module. Output goes through `print`. `trader/cli.py:96` prints yfinance exception text (URLs only, no credentials). `holdings.json`, `trades.csv` and `journal.csv` hold position and P&L data in plaintext. | `trader/cli.py:96`; `bot.py:173,188,256` | Use mode 600 for state files if the machine is shared. | none |
| 17 | INFO | Trust boundary for market data and future AI or filings output ("Jev"): no code consumes AI or filings text today. The only paths that change limits are `Limits.from_file` (startup config, or authenticated `set_limits`). The only paths that reach `OMS.place` are `Engine._act` with signals from the strategy, and `Engine.flatten` from the kill switch. External text has no route to risk settings or to order placement. External numbers do (see #1). | `trader/engine.py` (`_act`, `flatten`); `trader/cli.py:41`; `trader/control.py:73` | When AI or filings arrive, their output may only produce `NO_TRADE` or block flags, or advisory signals that still pass `RiskEngine.check`. Never let it produce `Limits` or call `Controller`. Pass the output through a typed schema, never through free text. | Re-review when that integration lands. |
| 18 | INFO | Dependencies: there is no `requirements.txt` or lock file, and nothing is hash-pinned. Dev tools (pytest, hypothesis) share the runtime environment. yfinance is an unofficial scraper: its terms of service and breakage are a risk, and it pulls in `curl_cffi`, a native browser-impersonation library. | `pip freeze` below | Commit a pinned `requirements.txt` with hashes (`pip-compile --generate-hashes`). Run pip-audit in CI. Split dev dependencies into their own file. | none |

## Scanner summaries

- **bandit 1.9.4** (`bandit -r trader bot.py intraday.py markets.py`, 1679 lines of code): 1 issue, Low severity. `B311` `random` in `trader/cli.py:50` is the synthetic-data generator, not crypto. False positive. 0 Medium, 0 High. Bandit did not flag the DDL f-string at `store.py:46`.
- **pip-audit** (`-r` the freeze below, `--no-deps --disable-pip`): `No known vulnerabilities found`. It warned that the requirements are not hash-pinned.
- **Secret grep:** no hits outside the controller docstring, parameter names and the test fixture.
- **Probes** (in memory, scratch directory only): they reproduced #1, #2, #6 and the fail-closed result for an empty token.
- Scanners ran in an isolated scratch venv, so the project `.venv` was not modified.

## Dependency inventory (`.venv/bin/pip freeze`, Python 3.14.6)

```
beautifulsoup4==4.15.0
certifi==2026.10.23
cffi==2.1.1
charset-normalizer==3.5.2
curl_cffi==0.17.0
hypothesis==6.170.14
idna==3.20
iniconfig==2.3.1
lxml==6.1.3
multitasking==0.0.13
numpy==2.5.4
packaging==26.3
pandas==3.1.0
peewee==4.5.3
platformdirs==4.14.3
pluggy==1.7.0
protobuf==7.36.2
pycparser==3.12
Pygments==2.21.0
pytest==9.2.0
python-dateutil==4.0.0
pytz==2026.6
requests==2.34.2
sortedcontainers==2.4.0
soupsieve==3.0.4
typing_extensions==4.16.0
tzdata==2026.6
urllib3==2.8.0
websockets==17.4
yfinance==1.7.0
```

## Not reviewed

- `tests/`: only grepped for secrets.
- `docs/REGULATORY.md`.
- The `trader/strategy_orb.py` logic beyond its signal and qty paths.
- yfinance internals, and network TLS settings inside curl_cffi.
- Contents of `data/md.db` (469 MB): not scanned for NaN payloads.
- OS-level file permissions and backups beyond `stat` of the config files and the DB.
- Passing tests were not taken as evidence of security.
