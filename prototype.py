from __future__ import annotations

import csv
import os
import json
import re
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from urllib import error, request

ROOT = Path(__file__).parent
INPUTS = ROOT / "inputs"
OUTPUT = ROOT / "output"
TOLERANCE = Decimal("0.01")
CSV_REQUIRED_COLUMNS = {
    "chart_of_accounts.csv": {"account_code", "account_name", "account_type"},
    "trial_balance.csv": {"account_code", "currency", "debit", "credit"},
    "prior_period_tb.csv": {"account_code"},
    "fx_rates.csv": {"currency", "rate_type"},
}
NUMERAL_PATTERN = re.compile(r"\d[\d,.-]*")


def fatal(message: str, *, code: str = "FATAL_VALIDATION") -> None:
    raise SystemExit(f"{code}: {message}")


def decimal(value: str | int | float) -> Decimal:
    try:
        text = str(value).strip().replace(",", "")
        if text == "":
            raise ValueError("blank amount")
        return Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid amount: {value!r}") from exc


def load_csv(name: str) -> list[dict[str, str]]:
    with (INPUTS / name).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = set(reader.fieldnames or [])
        required_columns = CSV_REQUIRED_COLUMNS.get(name, set())
        missing_columns = sorted(required_columns - fieldnames)
        if missing_columns:
            fatal(f"{name} is missing required columns: {', '.join(missing_columns)}", code="SCHEMA_CONTRACT_FAILED")
        return list(reader)


def write_json(name: str, payload: object) -> None:
    OUTPUT.mkdir(exist_ok=True)
    (OUTPUT / name).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_csv(name: str, rows: list[dict[str, str]]) -> None:
    OUTPUT.mkdir(exist_ok=True)
    with (OUTPUT / name).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["id", "status", "reasons"])
        writer.writeheader()
        writer.writerows(rows)


def file_metadata(name: str) -> dict[str, str]:
    path = INPUTS / name
    payload = path.read_bytes()
    return {
        "path": f"inputs/{name}",
        "sha256": sha256(payload).hexdigest(),
    }


def normalize_account_code(value: object) -> str:
    return str(value).strip()


def extract_numerals(text: str) -> set[str]:
    return {match.group(0) for match in NUMERAL_PATTERN.finditer(text)}


def deterministic_narration(status: str, issues: list[dict[str, str]]) -> str:
    if status == "ACCEPT":
        return "Accepted. The entry passed all deterministic validation checks for account validity, period, and balance."

    issue_messages = [issue["message"] for issue in issues]
    if status == "REJECT":
        return f"Rejected. Finance review is needed before posting because {'; '.join(issue_messages)}."
    return f"Escalated. The entry is balanced, but finance review is required because {'; '.join(issue_messages)}."


def guarded_narration(status: str, entry: dict, issues: list[dict[str, str]], *, fallback_text: str) -> dict[str, str]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or status == "ACCEPT" or not issues:
        return {
            "text": fallback_text,
            "authoritative_source": "structured_findings",
            "generation_mode": "deterministic_fallback",
        }

    numeral_allowlist = extract_numerals(json.dumps({
        "date": entry.get("date", ""),
        "description": entry.get("description", ""),
        "issues": issues,
        "debit_total": entry.get("debit_total", ""),
        "credit_total": entry.get("credit_total", ""),
        "difference": entry.get("difference", ""),
    }))
    prompt = {
        "model": "gpt-4o-mini",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Write one plain-English explanation for a finance user. "
                    "Do not change any number, do not do arithmetic, and use only numerals already provided."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "status": status,
                        "description": entry.get("description", ""),
                        "date": entry.get("date", ""),
                        "issues": issues,
                        "debit_total": entry.get("debit_total", ""),
                        "credit_total": entry.get("credit_total", ""),
                        "difference": entry.get("difference", ""),
                    }
                ),
            },
        ],
        "temperature": 0,
    }
    req = request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(prompt).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
        generated_text = payload["choices"][0]["message"]["content"].strip()
    except (error.URLError, TimeoutError, KeyError, json.JSONDecodeError):
        generated_text = fallback_text

    generated_numerals = extract_numerals(generated_text)
    if not generated_numerals.issubset(numeral_allowlist):
        generated_text = fallback_text
        generation_mode = "deterministic_fallback"
    else:
        generation_mode = "llm_guarded"

    return {
        "text": generated_text,
        "authoritative_source": "structured_findings",
        "generation_mode": generation_mode,
    }


def duplicate_entry_fingerprint(entry: dict) -> str:
    normalized_lines = []
    for line in entry.get("lines", []):
        normalized_lines.append(
            (
                normalize_account_code(line.get("account", "")),
                f"{decimal(line.get('debit', 0)):.2f}",
                f"{decimal(line.get('credit', 0)):.2f}",
            )
        )
    payload = {
        "date": entry.get("date", ""),
        "lines": sorted(normalized_lines),
    }
    return sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def validate_adjustments(coa: list[dict[str, str]], batch: dict) -> tuple[list[dict], list[dict[str, str]]]:
    accounts = {normalize_account_code(row["account_code"]): row for row in coa}
    results: list[dict] = []
    summary_rows: list[dict[str, str]] = []
    entry_ids = [entry.get("id", "") for entry in batch["entries"]]
    duplicate_entry_ids = {entry_id for entry_id, count in Counter(entry_ids).items() if entry_id and count > 1}
    fingerprint_counts = Counter()
    fingerprint_by_entry: dict[int, str | None] = {}
    for index, entry in enumerate(batch["entries"]):
        try:
            fingerprint = duplicate_entry_fingerprint(entry)
        except ValueError:
            fingerprint = None
        fingerprint_by_entry[index] = fingerprint
        if fingerprint:
            fingerprint_counts[fingerprint] += 1

    for entry_index, entry in enumerate(batch["entries"]):
        errors: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        debit_total = Decimal("0")
        credit_total = Decimal("0")
        seen_accounts: set[str] = set()
        debit_accounts: set[str] = set()
        credit_accounts: set[str] = set()
        line_results: list[dict] = []

        def add_issue(bucket: list[dict[str, str]], code: str, message: str) -> None:
            bucket.append({"code": code, "message": message})

        entry_id = entry.get("id", "")
        if not entry_id:
            add_issue(errors, "MISSING_ENTRY_ID", "journal entry id is missing")
        elif entry_id in duplicate_entry_ids:
            add_issue(errors, "DUPLICATE_ENTRY_ID", f"journal entry id {entry_id} is duplicated in the batch")
        fingerprint = fingerprint_by_entry[entry_index]
        if fingerprint and fingerprint_counts[fingerprint] > 1:
            add_issue(warnings, "POSSIBLE_DUPLICATE_ENTRY", "entry matches another journal entry by date, accounts, and amounts; finance review required")

        for line_number, line in enumerate(entry.get("lines", []), start=1):
            account = normalize_account_code(line.get("account", ""))
            line_errors: list[dict[str, str]] = []
            try:
                debit = decimal(line.get("debit", 0))
                credit = decimal(line.get("credit", 0))
            except ValueError:
                debit = Decimal("0")
                credit = Decimal("0")
                line_errors.append({"code": "MALFORMED_AMOUNT", "message": f"line {line_number}: debit or credit is not a valid amount"})
            debit_total += debit
            credit_total += credit

            if debit < 0 or credit < 0:
                line_errors.append({"code": "NEGATIVE_AMOUNT", "message": f"line {line_number}: debit and credit must be non-negative"})
            if debit > 0 and credit > 0:
                line_errors.append({"code": "DOUBLE_SIDED_LINE", "message": f"line {line_number}: debit and credit cannot both be populated"})
            if debit == 0 and credit == 0:
                line_errors.append({"code": "ZERO_VALUE_LINE", "message": f"line {line_number}: debit and credit cannot both be zero"})
            if account not in accounts:
                line_errors.append({"code": "MISSING_COA_ACCOUNT", "message": f"line {line_number}: account {account} is missing from chart of accounts"})
            else:
                seen_accounts.add(account)
                if accounts[account]["account_type"] == "Header":
                    line_errors.append({"code": "HEADER_ACCOUNT", "message": f"line {line_number}: account {account} is a header, not a posting account"})
                if debit > 0:
                    debit_accounts.add(account)
                if credit > 0:
                    credit_accounts.add(account)
            errors.extend(line_errors)
            line_results.append({
                "line_number": line_number,
                "account": account,
                "debit": f"{debit:.2f}",
                "credit": f"{credit:.2f}",
                "memo": line.get("memo", ""),
                "errors": line_errors,
            })

        if len(entry.get("lines", [])) < 2:
            add_issue(errors, "INSUFFICIENT_LINES", "journal entry must contain at least two lines")
        difference = debit_total - credit_total
        if abs(difference) > TOLERANCE:
            add_issue(errors, "OUT_OF_BALANCE", f"debits and credits differ by {difference:.2f}")
        if not entry.get("date"):
            add_issue(errors, "MISSING_DATE", "journal entry date is missing")
        else:
            try:
                entry_date = date.fromisoformat(entry["date"])
                if entry_date.year != 2024 or entry_date.month not in (10, 11, 12):
                    add_issue(errors, "DATE_OUT_OF_PERIOD", "journal entry date is outside 2024-Q4")
            except ValueError:
                add_issue(errors, "INVALID_DATE", "journal entry date is not a valid ISO date")

        same_side_accounts = sorted(debit_accounts & credit_accounts)
        for account in same_side_accounts:
            account_row = accounts.get(account)
            if not account_row:
                continue
            account_name = account_row["account_name"]
            if "intercompany" in account_name.lower():
                add_issue(
                    warnings,
                    "CIRCULAR_INTERCOMPANY",
                    f"entry uses intercompany account {account} on both debit and credit sides; finance review required",
                )
            else:
                add_issue(
                    warnings,
                    "SAME_ACCOUNT_BOTH_SIDES",
                    f"entry uses account {account} on both debit and credit sides; finance review required",
                )

        if errors:
            status = "REJECT"
        elif warnings:
            status = "ESCALATE"
        else:
            status = "ACCEPT"
        base_result = {
            "id": entry_id,
            "status": status,
            "description": entry.get("description", ""),
            "source": entry.get("source", ""),
            "date": entry.get("date", ""),
            "debit_total": f"{debit_total:.2f}",
            "credit_total": f"{credit_total:.2f}",
            "difference": f"{difference:.2f}",
        }
        fallback_narration = deterministic_narration(status, errors + warnings)
        narration = guarded_narration(status, base_result, errors + warnings, fallback_text=fallback_narration)
        result = {
            **base_result,
            "accounts": sorted(seen_accounts),
            "errors": errors,
            "warnings": warnings,
            "narration": narration,
            "line_results": line_results,
        }
        results.append(result)
        summary_rows.append({
            "id": result["id"],
            "status": status,
            "reasons": narration["text"],
        })

    return results, summary_rows


def source_controls(coa: list[dict[str, str]], tb: list[dict[str, str]], prior_tb: list[dict[str, str]], fx: list[dict[str, str]]) -> dict:
    coa_codes = {normalize_account_code(row["account_code"]) for row in coa}
    tb_codes = Counter(normalize_account_code(row["account_code"]) for row in tb)
    duplicate_keys = Counter((normalize_account_code(row["account_code"]), row["currency"]) for row in tb)
    debits = sum((decimal(row["debit"]) for row in tb), Decimal("0"))
    credits = sum((decimal(row["credit"]) for row in tb), Decimal("0"))
    prior_codes = {normalize_account_code(row["account_code"]) for row in prior_tb}
    period_end = {row["currency"] for row in fx if row["rate_type"] == "period_end"}
    currencies = {row["currency"] for row in tb}

    return {
        "trial_balance": {
            "row_count": len(tb),
            "debit_total": f"{debits:.2f}",
            "credit_total": f"{credits:.2f}",
            "difference": f"{debits - credits:.2f}",
            "is_balanced": abs(debits - credits) <= TOLERANCE,
            "unmapped_accounts": sorted(set(tb_codes) - coa_codes),
            "duplicate_account_currency_keys": [
                {"account_code": code, "currency": currency, "count": count}
                for (code, currency), count in sorted(duplicate_keys.items())
                if count > 1
            ],
        },
        "prior_period": {
            "row_count": len(prior_tb),
            "accounts_not_in_current_tb": sorted(prior_codes - set(tb_codes)),
        },
        "fx": {
            "tb_currencies": sorted(currencies),
            "period_end_currencies": sorted(period_end),
            "missing_period_end_rates": sorted(currencies - {"USD"} - period_end),
        },
    }


def apply_accepted_adjustments(tb: list[dict[str, str]], results: list[dict], batch: dict) -> dict:
    adjusted_rows = [dict(row) for row in tb]
    tb_index = {normalize_account_code(row["account_code"]): row for row in adjusted_rows if row["currency"] == batch["functional_currency"]}
    accepted_entries = {result["id"] for result in results if result["status"] == "ACCEPT"}

    for entry in batch["entries"]:
        if entry.get("id", "") not in accepted_entries:
            continue
        for line in entry.get("lines", []):
            account = normalize_account_code(line.get("account", ""))
            if account not in tb_index:
                tb_index[account] = {
                    "account_code": account,
                    "account_name": "ADJUSTMENT_ONLY_ACCOUNT",
                    "currency": batch["functional_currency"],
                    "debit": "0.00",
                    "credit": "0.00",
                }
                adjusted_rows.append(tb_index[account])
            row = tb_index[account]
            row["debit"] = f"{decimal(row['debit']) + decimal(line.get('debit', 0)):.2f}"
            row["credit"] = f"{decimal(row['credit']) + decimal(line.get('credit', 0)):.2f}"

    adjusted_debits = sum((decimal(row["debit"]) for row in adjusted_rows), Decimal("0"))
    adjusted_credits = sum((decimal(row["credit"]) for row in adjusted_rows), Decimal("0"))
    difference = adjusted_debits - adjusted_credits
    is_balanced = abs(difference) <= TOLERANCE
    return {
        "accepted_entry_count": len(accepted_entries),
        "debit_total": f"{adjusted_debits:.2f}",
        "credit_total": f"{adjusted_credits:.2f}",
        "difference": f"{difference:.2f}",
        "is_balanced": is_balanced,
        "release_ready": is_balanced,
        "release_blockers": [] if is_balanced else ["post-adjustment trial balance is not balanced within 0.01 USD"],
    }


def main() -> None:
    coa = load_csv("chart_of_accounts.csv")
    tb = load_csv("trial_balance.csv")
    prior_tb = load_csv("prior_period_tb.csv")
    fx = load_csv("fx_rates.csv")
    batch = json.loads((INPUTS / "manual_adjustments.json").read_text(encoding="utf-8"))
    if "period" not in batch or "functional_currency" not in batch or "entries" not in batch:
        fatal("manual_adjustments.json must contain period, functional_currency, and entries", code="SCHEMA_CONTRACT_FAILED")

    controls = source_controls(coa, tb, prior_tb, fx)
    results, summary_rows = validate_adjustments(coa, batch)
    post_adjustment_controls = apply_accepted_adjustments(tb, results, batch)
    accepted = sum(result["status"] == "ACCEPT" for result in results)
    escalated = sum(result["status"] == "ESCALATE" for result in results)
    rejected = sum(result["status"] == "REJECT" for result in results)

    decision_payload = {
        "prototype": "manual-adjustment-validator",
        "period": batch["period"],
        "functional_currency": batch["functional_currency"],
        "source_files": [
            file_metadata("chart_of_accounts.csv"),
            file_metadata("trial_balance.csv"),
            file_metadata("prior_period_tb.csv"),
            file_metadata("fx_rates.csv"),
            file_metadata("manual_adjustments.json"),
        ],
        "decision_policy": {
            "accept_only_when": [
                "all accounts exist in the COA and are posting accounts",
                "all lines have non-negative, single-sided amounts",
                "all lines have a non-zero posting amount",
                "debits equal credits within 0.01 USD",
                "entry date is inside 2024-Q4",
            ],
            "escalate_when": [
                "an entry is balanced but suspicious or ambiguous",
                "the same account appears on both debit and credit sides",
                "an intercompany account appears on both sides and needs finance review",
            ],
            "ambiguous_or_failed_entries": "reject or escalate for finance review; do not auto-post",
        },
        "summary": {"entries": len(results), "accepted": accepted, "escalated": escalated, "rejected": rejected},
        "source_controls": controls,
        "post_adjustment_controls": {"trial_balance": post_adjustment_controls},
        "entries": results,
    }

    audit = {
        "decision_payload": decision_payload,
        "run_metadata": {
            "run_at_utc": datetime.now(timezone.utc).isoformat(),
            "validation_version": "2.0",
        },
    }
    write_json("adjustment_validation.json", audit)
    write_csv("adjustment_validation.csv", summary_rows)
    print(json.dumps(decision_payload["summary"], indent=2))
    print(json.dumps(controls["trial_balance"], indent=2))


if __name__ == "__main__":
    main()
