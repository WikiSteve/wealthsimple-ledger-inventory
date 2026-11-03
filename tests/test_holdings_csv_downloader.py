from pathlib import Path

import pytest

from scripts.download_wealthsimple_holdings_csv import CSV_REQUIRED_COLUMNS, validate_holdings_csv
from scripts.download_wealthsimple_activity_csv import (
    DOCUMENTS_DIALOG_TESTID, click_all_accounts, click_export_option,
    dismiss_modal_with_escape, visible_modal_heading,
)


@pytest.mark.parametrize("modal_attribute", ["", 'aria-modal="true"'])
def test_documents_wizard_without_aria_modal_uses_named_dialog(fixture_browser, tmp_path, modal_attribute):
    page = tmp_path / "wizard.html"
    page.write_text(f'''<!doctype html><html><body>
      <div role="dialog" aria-modal="false"><h2>Support</h2>
        <span role="checkbox" aria-labelledby="support-label" aria-checked="false"
          onclick="window.wrongClick=true"></span><span id="support-label">All accounts</span></div>
      <div role="dialog" {modal_attribute} data-testid="button-generate-documents-modal">
        <h2>Generate document</h2>
        <button data-testid="button-generate-documents-type-selector" aria-controls="types">Document type</button>
        <span role="checkbox" aria-labelledby="all-label" aria-checked="false"
          style="display:inline-block;width:20px;height:20px"
          onclick="this.setAttribute('aria-checked','true')"></span><span id="all-label">All accounts</span>
      </div>
      <div role="listbox" id="unrelated"><div role="option" onclick="window.wrongClick=true">Holdings report (CSV)</div></div>
      <div role="listbox" id="types"><div role="option" onclick="window.selected='holdings'">Holdings report (CSV)</div></div>
      <script>document.addEventListener('keydown',e=>{{if(e.key==='Escape')
        document.querySelector('[data-testid=button-generate-documents-modal]').style.display='none'}})</script>
    </body></html>''', encoding="utf-8")
    driver = fixture_browser
    driver.get(page.as_uri())
    assert visible_modal_heading(driver) == "Generate document"
    click_export_option(driver, "Holdings report (CSV)", DOCUMENTS_DIALOG_TESTID,
                        "button-generate-documents-type-selector")
    assert driver.execute_script("return window.selected") == "holdings"
    click_all_accounts(driver, DOCUMENTS_DIALOG_TESTID)
    assert not driver.execute_script("return !!window.wrongClick")
    with pytest.raises(RuntimeError, match="option missing"):
        click_export_option(driver, "Other report", DOCUMENTS_DIALOG_TESTID,
                            "button-generate-documents-type-selector")
    assert dismiss_modal_with_escape(driver)
    assert visible_modal_heading(driver) is None


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
