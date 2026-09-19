# Accounting Agent Prototype

This workspace contains a validation-first prototype for the supplied messy accounting data. The implemented slice is the **manual-adjustment validator**.

## Environment

- Tested with **Python 3.13**
- Implementation uses the **Python standard library only**

## Submission contents

- `architecture.md` — full-system design, agent topology, controls, and audit model
- `prototype.py` — working prototype for the chosen slice
- `reflection.md` — tradeoffs, scale limits, AI-tool usage, and underestimated risk
- `clarifying_questions.md` — accounting questions I would raise before starting
- `output/` — generated prototype artifacts

## Run

From the workspace root:

```powershell
python .\prototype.py
```

```bash
python prototype.py
```

The command reads every file under `inputs/` and writes:

- `output/adjustment_validation.json`: full audit trace and source controls
- `output/adjustment_validation.csv`: one status row per journal entry

The validator returns one of three outcomes per entry:

- `ACCEPT` for clean entries that satisfy every deterministic rule
- `REJECT` for hard validation failures
- `ESCALATE` for balanced but suspicious entries that require finance review

The validator accepts only entries with known posting accounts, valid non-negative single-sided non-zero lines, a 2024-Q4 date, and balanced debits and credits within USD 0.01. Suspicious same-account patterns are escalated; failed entries are rejected with plain-English finance-facing explanations. Raw input is never rewritten.

The prototype also validates the input schema before processing and converts malformed amounts into controlled validation findings instead of a Python traceback. Account codes are trimmed for whitespace, but leading-zero normalization is intentionally not applied.

After posting only accepted entries into the functional-currency TB, the prototype recomputes post-adjustment TB controls and exposes a release-readiness flag. For the supplied data, that control still fails because the underlying TB remains out of balance by USD 4,800, so the output is an audited validation artifact rather than a releasable downstream statement package.

## Expected result for supplied data

- 10 adjustment entries evaluated
- 7 accepted
- 1 escalated: `JE-008` uses the same intercompany account on both debit and credit sides
- 2 rejected: `JE-002` is out of balance; `JE-005` uses missing account `6315`
- Current TB control is out of balance by USD 4,800
- TB account `9999` is unmapped
- USD account/currency key `6310` appears twice
- GBP has no period-end FX rate

The JSON audit file separates deterministic `decision_payload` content from `run_metadata`, so the decision block stays stable across repeated runs on the same inputs. Each entry stores structured findings plus a non-authoritative narration field for the finance-facing explanation.

See [architecture.md](architecture.md) for the agent workflow, deterministic/LLM boundary, failure handling, and audit model. See [clarifying_questions.md](clarifying_questions.md) for the accounting questions I would raise before starting. See [reflection.md](reflection.md) for tradeoffs, scale limits, and AI-tool usage.
