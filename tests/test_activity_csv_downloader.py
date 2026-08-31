from pathlib import Path

import pytest

from scripts.download_wealthsimple_activity_csv import (
    AUDIT_RUNNER,
    CSV_REQUIRED_COLUMNS,
    audit_command_for_activity_export,
    new_csv,
    validate_activity_csv,
    write_activity_analysis,
)
from src.full_account_inventory import read_wealthsimple_export


def test_validate_activity_csv_requires_the_activity_schema(tmp_path: Path):
    good = tmp_path / "activities-export.csv"
    good.write_text(
        '"transaction_date","account_type","activity_type","description"\n'
        '2026-07-24,TFSA,Trade,Bought 10 GOOG\n',
        encoding="utf-8",
    )
    assert set(validate_activity_csv(good)) >= CSV_REQUIRED_COLUMNS

    bad = tmp_path / "not-activities.csv"
    bad.write_text('"date","amount"\n2026-01-01,5\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="not an Activities export"):
        validate_activity_csv(bad)


def test_effective_at_schema_is_accepted_and_normalized_to_utc_date(tmp_path: Path):
    export = tmp_path / "activities-export-new-schema.csv"
    export.write_text(
        'effective_at,account_type,activity_type,description\n'
        '2026-08-08T21:00:00-03:00,TFSA,MoneyMovement,Withdrawal\n',
        encoding="utf-8",
    )
    columns = validate_activity_csv(export)
    assert "effective_at" in columns
    rows, _ = read_wealthsimple_export(export)
    assert rows[0]["effective_at"] == "2026-08-08T21:00:00-03:00"
    assert rows[0]["transaction_date"] == "2026-08-09"


def test_new_csv_only_returns_a_file_created_after_the_download_starts(tmp_path: Path):
    old = tmp_path / "activities-export-old.csv"
    old.write_text("old\n", encoding="utf-8")
    before = {old}
    assert new_csv(tmp_path, before) is None
    fresh = tmp_path / "activities-export-new.csv"
    fresh.write_text("new\n", encoding="utf-8")
    assert new_csv(tmp_path, before) == fresh


def test_audit_handoff_uses_the_preserved_csv_path_not_standard_input(tmp_path: Path):
    export = tmp_path / "activities-export.csv"
    command = audit_command_for_activity_export(export)
    assert command[-4:] == ["--mode", "FULL", "--activity-export", str(export)]
    assert str(AUDIT_RUNNER) in command


def test_write_activity_analysis_creates_a_chatgpt_readable_report(tmp_path: Path):
    export = tmp_path / "activities-export.csv"
    export.write_text(
        'transaction_date,account_type,activity_type,activity_sub_type,symbol,currency,quantity,unit_price,net_cash_amount\n'
        '2026-07-24,TFSA,Trade,BUY,GOOG,CAD,10,51.57,-515.70\n',
        encoding="utf-8",
    )
    outputs = write_activity_analysis(export, tmp_path)
    assert Path(outputs["analysis_json"]).is_file()
    assert "GOOG" in Path(outputs["analysis_markdown"]).read_text(encoding="utf-8")
