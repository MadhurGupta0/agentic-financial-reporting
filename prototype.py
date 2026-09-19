from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).parent
INPUTS = ROOT / "inputs"
OUTPUT = ROOT / "output"
TOLERANCE = Decimal("0.01")


def decimal(value: str | int | float) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid amount: {value!r}") from exc


def load_csv(name: str) -> list[dict[str, str]]:
    with (INPUTS / name).open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


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


def validate_adjustments(coa: list[dict[str, str]], batch: dict) -> tuple[list[dict], list[dict[str, str]]]:
    accounts = {row["account_code"]: row for row in coa}
    results: list[dict] = []
    summary_rows: list[dict[str, str]] = []
    entry_ids = [entry.get("id", "") for entry in batch["entries"]]
    duplicate_entry_ids = {entry_id for entry_id, count in Counter(entry_ids).items() if entry_id and count > 1}

    for entry in batch["entries"]:
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

        for line_number, line in enumerate(entry.get("lines", []), start=1):
            account = line.get("account", "")
            debit = decimal(line.get("debit", 0))
            credit = decimal(line.get("credit", 0))
            line_errors: list[dict[str, str]] = []
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
                account_name = accounts[account]["account_name"]
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
        result = {
            "id": entry_id,
            "status": status,
            "description": entry.get("description", ""),
            "source": entry.get("source", ""),
            "date": entry.get("date", ""),
            "debit_total": f"{debit_total:.2f}",
            "credit_total": f"{credit_total:.2f}",
            "difference": f"{difference:.2f}",
            "accounts": sorted(seen_accounts),
            "errors": errors,
            "warnings": warnings,
            "line_results": line_results,
        }
        results.append(result)
        summary_rows.append({
            "id": result["id"],
            "status": status,
            "reasons": " | ".join(issue["message"] for issue in (errors + warnings)),
        })

    return results, summary_rows


def source_controls(coa: list[dict[str, str]], tb: list[dict[str, str]], prior_tb: list[dict[str, str]], fx: list[dict[str, str]]) -> dict:
    coa_codes = {row["account_code"] for row in coa}
    tb_codes = Counter(row["account_code"] for row in tb)
    duplicate_keys = Counter((row["account_code"], row["currency"]) for row in tb)
    debits = sum((decimal(row["debit"]) for row in tb), Decimal("0"))
    credits = sum((decimal(row["credit"]) for row in tb), Decimal("0"))
    prior_codes = {row["account_code"] for row in prior_tb}
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


def main() -> None:
    coa = load_csv("chart_of_accounts.csv")
    tb = load_csv("trial_balance.csv")
    prior_tb = load_csv("prior_period_tb.csv")
    fx = load_csv("fx_rates.csv")
    batch = json.loads((INPUTS / "manual_adjustments.json").read_text(encoding="utf-8"))

    controls = source_controls(coa, tb, prior_tb, fx)
    results, summary_rows = validate_adjustments(coa, batch)
    accepted = sum(result["status"] == "ACCEPT" for result in results)
    escalated = sum(result["status"] == "ESCALATE" for result in results)
    rejected = len(results) - accepted

    audit = {
        "prototype": "manual-adjustment-validator",
        "run_metadata": {
            "run_at_utc": datetime.now(timezone.utc).isoformat(),
            "validation_version": "2.0",
        },
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
        "summary": {"entries": len(results), "accepted": accepted, "escalated": escalated, "rejected": rejected - escalated},
        "source_controls": controls,
        "entries": results,
    }
    write_json("adjustment_validation.json", audit)
    write_csv("adjustment_validation.csv", summary_rows)
    print(json.dumps(audit["summary"], indent=2))
    print(json.dumps(controls["trial_balance"], indent=2))


if __name__ == "__main__":
    main()
