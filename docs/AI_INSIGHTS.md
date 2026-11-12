# AI insights: architecture, safeguards and costs

**Status (12 Nov 2026): implemented and tested offline. One approved live call has been made, and its response was rejected by validation.** Everything AI-related on the dashboard is produced by an offline mock provider or a deterministic fallback, and is labelled that way. See [Live test record](#live-test-record). Each further paid call needs the project owner's approval.

## What it does

GrowthPulse sends Claude an **evidence package** built from one analytics report and asks for three kinds of content, kept strictly apart:

| Kind | Meaning | On screen |
|---|---|---|
| Observed facts | What the evidence shows directly. No causes | "What the evidence shows. No causes." |
| Hypotheses | Possible explanations, untested | "Possible explanations, untested." |
| Experiment proposals | Tests a person may choose to run | "Proposal · requires human approval · not executed" |

Every item cites the finding IDs it rests on. A response is shown only after it passes 15 validation checks against the evidence. Otherwise a deterministic fallback is shown, with the reason.

## Pipeline

```
report ─► evidence package ─► provider (mock | Claude) ─► JSON ─► validation ─► insights artifact ─► dashboard builder (re-validates) ─► static dashboard JSON
                                    ▲                                    │ any failure
                     budget reserve ┘ reconcile after                    ▼
                     (live only)                                 deterministic fallback
```

| Module (`src/growthpulse_ai/ai_analysis/`) | Role |
|---|---|
| `evidence.py` | Copies finding IDs, values, periods, conversion bases, currencies and limitations from the report, verbatim. The one derived value is each comparison window's length in days, counted from the report's own dates. It never modifies the report. Shared values (conversion bases, comparison windows, extra limitations) are stated once in lookup tables (schema 1.1). The package ID is the SHA-256 of its canonical JSON |
| `prompt.py` | The versioned system prompt (`PROMPT_VERSION`). Its rules mirror the validator's checks one for one |
| `schema.py` | Pydantic models (strict, `extra="forbid"`, length and count limits), plus the structured-output JSON schema. The schema's enums are built from the package, so the model can only name finding IDs, metrics, bases and currencies that exist |
| `validation.py` | The 15 checks. All or nothing |
| `fallback.py` | Deterministic, number-free templates filled only with evidence values, and validated by the same checks |
| `providers.py` | `MockProvider` (offline) and `AnthropicProvider` (official SDK) |
| `pricing.py` | Versioned price table, maximum-cost bound, and cost from reported usage |
| `ledger.py` | Local spend ledger with the $1.00 cap |
| `audit.py` | Optional local, owner-only copies of rejected responses for review. Write-only for the application |
| `service.py` | The orchestration, the artifact format, re-validation and the dashboard block |
| `__main__.py` | The CLI |

The package respects the architecture test. `ai_analysis` imports no connector, persistence, pipeline, dashboard or credentials code. The dashboard builder (`frontend/tools/`, outside the package) is what connects reports to insights.

## Validation design

The checks are listed in `CHECKS`. Any violation rejects the whole response, records the check, the path and a short reason (never the rejected text), and shows the fallback.

| Check | Rule |
|---|---|
| `schema` | Strict types, required fields, no extra fields, length and count limits, `duration_days` 7–42, `requires_human_approval` exactly `true` |
| `finding_ids_exist` | Every cited ID is in the package. Each item cites one to six |
| `numbers_supported` | Every number in prose is a value from the evidence of the findings *that item cites*, rounded no further than written. That includes their comparison window lengths and attribution windows. One significant figure must match exactly. Rates may be written as percentages. Derived figures (differences, totals, projections) are rejected |
| `dates_supported` | ISO dates must come from the cited findings' periods or evidence |
| `identifiers_known` | No hex IDs or `synthetic-…` identifiers outside the package |
| `campaigns_known_and_cited` | Campaign names must exist in the package and belong to a cited finding |
| `single_currency` | An item never cites findings in more than one currency |
| `currency_symbols_match` | No currency symbol or code other than the cited findings' |
| `experiment_baseline_cited` | The baseline finding is among the experiment's citations |
| `experiment_target_metric_matches` | The target metric is the baseline finding's metric |
| `experiment_attribution_matches` | Every cited finding shares the experiment's conversion basis |
| `experiment_currency_matches` | Every cited finding shares the experiment's currency |
| `facts_state_no_causes` | Facts contain no causal language ("due to", "driven by", and similar) |
| `no_execution_claims` | Nothing is described as applied, launched, paused, approved or live |
| `no_duplicates` | No repeated statement, proposal or citation |

**Diagnostics.** Each rejected number records:
- the cited finding IDs and the JSON path;
- the number as written;
- up to five nearest cited evidence values, with their finding and field.

Each rejection also gets one classification:

| Classification | Meaning |
|---|---|
| `missing_evidence` | The item cites no known finding |
| `incorrect_finding_linkage` | The value is in an uncited finding's evidence. Those findings are listed |
| `numerical_transformation` | The value can be derived from the cited evidence but isn't stated in it. The derivation is named (a date fragment, rounding to one significant figure, ×100 or ÷100, a difference, sum, ratio or percent change) and its operands listed. A derived match must be exact or written to three significant figures, so small integers aren't misattributed by chance |
| `unsupported_invention` | Nothing matches |

Diagnostics run only after a number has been rejected, so they never change what is accepted. They contain numbers and evidence references, never the statement's text, so they're safe to print and to store in an artifact.

**Rejection audit.** Every artifact discards a rejected response. With the audit enabled, the response is first written to `data/ai_rejections/` (gitignored) for review. Its protections:
- **Owner-only:** the directory is 0700, and each file is 0600, created exclusively and never overwritten.
- **Minimal:** it holds the response, the violations, provenance IDs and the stop reason. It never holds the key, headers or prompt, and key-shaped strings are redacted.
- **Pruned** after `AI_REJECTION_AUDIT_RETENTION_DAYS`.
- **Default:** `auto` enables it only for `APP_ENV=local` outside CI. The CI workflow sets `APP_ENV=ci`.
- **Never read back:** nothing in the application reads it. The dashboard builder reads only `docs/samples/ai_insights/` and re-validates what it finds, so an audited response can't bypass validation. The CLI prints diagnostics, never the rejected text.

The model never states the experiment baseline as a number. It names a finding and either `current_value` or `baseline_value`, and the value shown comes from the evidence. Durations live in a structured field, not in prose. Attribution and platform limits come from the package, not from the model, so they're always shown.

The dashboard builder **re-validates** every stored artifact against a freshly built package. An artifact is rejected if it's for another report, built from different evidence, edited after generation, or of an unknown version. In each case the fallback is shown.

## Cost and budget design

**Pricing table** (`pricing.py`, version `2026-11-12.1`). `claude-opus-5-5` costs, per million tokens:
- input $4;
- output $20;
- 5-minute cache write $5;
- 1-hour cache write $8;
- cache read $0.20.

These are standard tier with global inference, as documented on 12 Nov 2026. **Check them against <https://www.anthropic.com/pricing> before a live run.** A price change needs a new `PRICING_VERSION`. Every ledger entry and artifact records the version that priced it.

**Maximum cost per request**, computed before the call with no API request:

```
max input tokens  = UTF-8 bytes of (system prompt + user message + output schema) + 2,048
max cost          = (max input × $5/MTok + max_tokens × $20/MTok) × 1.10
```

- A token is at least one byte, so the byte count bounds the input from above.
- $5 is the highest rate an input token of this request can incur, because no `cache_control` is set.
- Thinking is billed as output and counts toward `max_tokens`.
- The 1.10 factor allows for price modifiers such as regional inference.

For the sample report the maximum is 31,411 input + 10,000 output tokens = **$0.392761**. It was 37,189 tokens and $0.424540 before the package de-duplication in schema 1.1.

**Ledger** (`data/ai_spend_ledger.jsonl`, gitignored). This is an append-only JSON Lines file, read and written under an exclusive `flock`, so concurrent threads and processes see each other.

1. **Reserve.** The maximum cost is reserved before the call. If committed spend plus the reservation would exceed the budget, the call never happens.
2. **Resolve,** exactly once:
   - **Settle:** the response reported usage, which is priced at list rates and recorded with token counts.
   - **Release:** a 4xx rejection (429, 401, 403, 400). The request was refused before any work, so the cost is $0.
   - **Hold:** the cost is unknowable, so the full reservation stays counted. This covers a timeout, a dropped connection, a 5xx, missing usage, an unpriced response model, or a reported price modifier (`service_tier`, `speed`, `inference_geo` other than global).
3. **Committed spend** = settled costs + open reservations + held reservations. A process that crashes mid-call leaves its reservation open, so it stays counted.

**The ledger fails closed:**
- An unreadable, inconsistent or truncated ledger blocks every reservation.
- A request that settles above its reservation sets `needs_review`, which blocks every further call until a person looks.
- The budget is `AI_BUDGET_USD`, at most **$1.00** (`AI_BUDGET_CEILING_USD`). Settings can lower it. Raising it needs a code change.
- **No retries:** the SDK client uses `max_retries=0`, and the pipeline makes one request per run.

**The ledger is not the account balance.** It's GrowthPulse's own estimate at list prices. GrowthPulse doesn't read the Anthropic Console, call billing or Admin APIs, or need an Admin API key. The dashboard shows the account balance only as a **dated manual snapshot** ($2.84 on 12 Nov 2026, reported by the account owner). That snapshot is never reconciled with the ledger.

## Credentials

- `ANTHROPIC_API_KEY` is read from the environment, or a local `.env`, into a `SecretStr`. `AnthropicProvider` unwraps it only when it constructs the SDK client.
- `repr()` masks the key, and the key is never logged, printed, written to an artifact or the ledger, or sent to the browser.
- SDK errors become fixed codes such as `timeout`, `rate_limited` and `authentication`. The SDK's own message text is never surfaced.
- A test injects a fake key and a fake SDK client, including an error whose message contains the key. It then checks that the key appears in none of: the artifact, the ledger, stdout, stderr or logs.
- All Anthropic calls are server-side, from the CLI. The static dashboard and its JSON contain no credentials and no API endpoint. The browser never calls Anthropic.

## Mock versus live

| | Mock (default) | Live |
|---|---|---|
| Provider | `MockProvider`: deterministic template content | `AnthropicProvider`: official `anthropic` SDK 2.8 |
| Network | None | One streamed `messages` request |
| Cost | $0, ledger untouched | Reserved, then reconciled |
| Requires | Nothing | **Every gate:**<br>• the `--live` flag<br>• `AI_LIVE_CALLS_ENABLED=true`<br>• `ANTHROPIC_API_KEY` in the environment<br>• `--confirm-max-cost` ≥ the computed maximum<br>• a readable ledger that doesn't need review and has room for the reservation |
| Dashboard label | "Offline mock provider: deterministic template content, not Claude output" | "Claude (Anthropic API), pre-generated and validated" |

The gates are checked in order before a provider object exists. Live request parameters:
- `model=claude-opus-5-5`;
- `max_tokens=10000`;
- `output_config={"effort": "medium", "format": {"type": "json_schema", ...}}`;
- streaming, so long thinking can't trip an HTTP timeout;
- a 300-second timeout.

Three things are deliberately left unset:
- **`thinking`.** It's always on for this model, so effort is the only control.
- **`tool_choice`.** Forced tool choice returns a 400 on this model.
- **Server-side model fallbacks.** A fallback model would bill at a price the ledger doesn't hold.

The dashboard never runs inference. It shows a pre-generated artifact from `docs/samples/ai_insights/<report_id>.json` after re-validating it, or the fallback. Today:
- the sample client (`feb1e176…`) shows a **mock** artifact;
- the other two clients show the **fallback**.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | blank | Live only. Environment or local `.env`, never committed |
| `ANTHROPIC_MODEL` | `claude-opus-5-5` | Must be in the price table, or every live request is held at its maximum |
| `ANTHROPIC_EFFORT` | `medium` | `low`, `medium` or `high` |
| `AI_LIVE_CALLS_ENABLED` | `false` | Opt-in for live calls |
| `AI_BUDGET_USD` | `1.00` | Cap, at most 1.00 |
| `AI_LEDGER_PATH` | `data/ai_spend_ledger.jsonl` | Gitignored |
| `AI_MAX_OUTPUT_TOKENS` | `10000` | 1,024–16,000. Bounds the maximum cost |
| `AI_REQUEST_TIMEOUT_SECONDS` | `300` | 10–600 |
| `AI_REJECTION_AUDIT` | `auto` | `auto` means local development outside CI only. `enabled` or `disabled` override it |
| `AI_REJECTION_AUDIT_DIR` | `data/ai_rejections/` | Gitignored |
| `AI_REJECTION_AUDIT_RETENTION_DAYS` | `14` | 1–90 |

## Commands

```bash
# Maximum cost of one request for a report (no API call)
uv run python -m growthpulse_ai.ai_analysis estimate --report docs/samples/synthetic_analytics_report.json

# Offline mock artifact (no API call, no charge, ledger untouched)
uv run python -m growthpulse_ai.ai_analysis generate \
  --report docs/samples/synthetic_analytics_report.json \
  --out docs/samples/ai_insights/feb1e17675c362408a69b029c0f932a347e2c28618880f63ec33bbbd82e4e601.json

# Ledger status (local estimate, not the account balance)
uv run python -m growthpulse_ai.ai_analysis ledger

# Rebuild the dashboard data, which re-validates stored artifacts
uv run python frontend/tools/build_dashboard_data.py
```

Exit codes: `0` means validated, `2` means blocked before any call, and `3` means the fallback was used.

## A controlled live test

**Each run needs the project owner's approval.** It makes one request, with no retries, at a maximum of **$0.392761**. The first run cost an estimated $0.202444. Most of that was output: 6,984 tokens including thinking, against 15,691 input tokens.

1. Check the prices in `pricing.py` against the pricing page.
2. Put the key in the environment of one shell only, typed at a hidden prompt so it stays out of shell history and logs:
   ```bash
   read -rs ANTHROPIC_API_KEY && export ANTHROPIC_API_KEY
   export AI_LIVE_CALLS_ENABLED=true
   ```
3. Confirm that the maximum and the ledger are as expected:
   ```bash
   uv run python -m growthpulse_ai.ai_analysis estimate --report docs/samples/synthetic_analytics_report.json
   uv run python -m growthpulse_ai.ai_analysis ledger
   ```
4. Run once, writing to a scratch file first:
   ```bash
   uv run python -m growthpulse_ai.ai_analysis generate \
     --report docs/samples/synthetic_analytics_report.json \
     --out tmp/live_artifact.json --live --confirm-max-cost 0.40
   ```
5. Run `unset ANTHROPIC_API_KEY AI_LIVE_CALLS_ENABLED`. Then compare the ledger's estimate with the Anthropic Console's usage page: they're expected to differ slightly, and neither is assumed to equal the other.
6. Review the artifact.
   - If the outcome is `validated` and the content is acceptable, move it to `docs/samples/ai_insights/<report_id>.json`, then regenerate the dashboard data.
   - If the outcome is `fallback`, the reasons are listed, and nothing reaches the dashboard.

## Live test record

| | First live test, 12 Nov 2026 |
|---|---|
| Request | One `claude-opus-5-5` request on the synthetic sample report, no retries. Message `msg_011CgyJbJd92Q9pR8AwZXToL` |
| Prices | Checked against Anthropic's pricing and models pages on the day. They matched `2026-11-12.1` |
| Usage | 15,691 input + 6,984 output tokens. No cache tokens and no price modifiers |
| Estimated cost | $0.202444 (reserved $0.424540). The ledger holds $0.202444 committed of $1.00 |
| Validation | Rejected. `numbers_supported` failed twice at `observed_facts[5].statement`, and the other 14 checks passed. The fallback was used and nothing was published |
| Raw response | Discarded. The rejection audit didn't exist yet |
| Likely cause | Found offline: the comparison window lengths ("the latest 10 days versus the first 10 days") weren't in the evidence, so they failed twice in one statement. They are evidence from schema 1.1 on. `tests/test_ai_diagnostics.py` reproduces this. It's a hypothesis, because the response itself wasn't kept |

## Limitations

- **Heuristic checks.**
  - The number, causal-language and execution-claim checks work on text patterns.
  - Spelled-out numbers ("two weeks") aren't caught.
  - A causal claim phrased without the listed words could pass as a fact.
  - Campaign detection relies on the `Platform | Product | Market` naming convention.
- **One live response so far, and it was rejected.** The fix for its likely cause has been verified only offline. Prompt and validator changes need a new `PROMPT_VERSION`.
- **Loose input bound.** The byte-count bound makes reservations about three times the likely input cost. That is safe, but uses budget headroom: two held requests would exhaust $1.00.
- **Stale reservations.** A reservation left open by a crash stays counted forever. There's no tool to resolve it, by design, and editing the ledger is a manual, reviewed act.
- **POSIX only.** The ledger is local to one machine and needs POSIX `flock`. It isn't shared between machines or CI.
- **Unpersisted approvals.** Proposals carry `approval: "not_requested"`. Approving one in the dashboard demo only changes page state.
- **Synthetic data only.**
