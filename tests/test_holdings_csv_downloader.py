from pathlib import Path

import pytest

from scripts.download_wealthsimple_holdings_csv import CSV_REQUIRED_COLUMNS, validate_holdings_csv


def test_validate_holdings_csv_requires_holdings_schema_and_position(tmp_path: Path):
    good = tmp_path / "holdings-report.csv"
    good.write_text(
        'Account Type,Symbol,Quantity,Market Value\n'
        'TFSA,GOOG,10,531.00\n'
        '"As of 2026-08-06 12:00 GMT-03:00"\n',
        encoding="utf-8",
    )
    assert set(validate_holdings_csv(good)) >= CSV_REQUIRED_COLUMNS

    wrong = tmp_path / "activities-export.csv"
    wrong.write_text(
        'transaction_date,account_type,activity_type,description\n'
        '2026-08-06,TFSA,Trade,Bought GOOG\n',
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="not a Holdings report"):
        validate_holdings_csv(wrong)

    empty = tmp_path / "empty-holdings.csv"
    empty.write_text('Account Type,Symbol,Quantity,Market Value\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="no position rows"):
        validate_holdings_csv(empty)
