from src.account_summary import ACCOUNT_SUMMARY_SCRIPT, native_cash_value


def test_currency_rows_and_animated_account_value(fixture_browser, tmp_path):
    page = tmp_path / "account.html"
    page.write_text('''<!doctype html><h1>TFSA</h1>
      <div data-testid="diy-account-history-graph">
        <p><span>$1,234.56</span><span aria-hidden="true">$1,234.56</span></p>
        <button aria-label="Hide account balance"></button>
        <button aria-label="Currency: CAD. Switch to USD"></button>
        <button role="radio" aria-checked="true">Account value</button>
      </div>
      <div><div><span>Total CAD cash available</span></div><div><span>$100.00</span><span aria-hidden="true">$100.00</span></div>
        <div><svg><defs><clipPath id="mint-currency-cad-fixture"></clipPath></defs></svg><span>$80.00</span></div>
        <div><svg><defs><clipPath id="mint-currency-usd-fixture"></clipPath></defs></svg><span>$14.00</span></div>
      </div><p>$9,999.00</p>''', encoding="utf-8")
    driver = fixture_browser
    driver.get(page.as_uri())
    got = driver.execute_script(ACCOUNT_SUMMARY_SCRIPT)
    assert got["account_value"] == "$1,234.56 CAD"
    assert got["total_cash_cad"] == "$100.00 CAD"
    assert native_cash_value(got, "CAD") == "$80.00 CAD"
    assert native_cash_value(got, "USD") == "$14.00 USD"
    driver.execute_script("document.querySelector('[role=radio]').innerText='Returns'")
    assert driver.execute_script(ACCOUNT_SUMMARY_SCRIPT)["account_value"] is None
    driver.execute_script("document.querySelector('clipPath[id^=mint-currency-usd]').closest('div').remove()")
    assert native_cash_value(driver.execute_script(ACCOUNT_SUMMARY_SCRIPT), "USD") is None


def test_conflicting_native_currency_rows_are_not_a_balance():
    assert native_cash_value({"native_cash": [
        {"currency": "CAD", "amount": "$10.00 CAD"},
        {"currency": "CAD", "amount": "$20.00 CAD"},
    ]}, "CAD") is None
