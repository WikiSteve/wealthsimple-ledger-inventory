"""Read-only evidence for the account graph and currency-labelled cash rows."""

ACCOUNT_SUMMARY_SCRIPT = r"""
const money = t => /^\$[\d,]+\.\d{2}$/.test((t || '').trim());
const amountText = node => {
  if (!node) return '';
  const copy = node.cloneNode(true);
  copy.querySelectorAll('[aria-hidden="true"]').forEach(n => n.remove());
  return copy.textContent.trim();
};
const graph = document.querySelector('[data-testid="diy-account-history-graph"]');
let accountValue = null;
if (graph && graph.querySelector('button[aria-label="Hide account balance"]') &&
    Array.from(graph.querySelectorAll('[role="radio"][aria-checked="true"]'))
      .some(e => e.innerText.trim() === 'Account value') &&
    graph.querySelector('button[aria-label="Currency: CAD. Switch to USD"]')) {
  // Animated amounts repeat a screen-reader value in aria-hidden digits.
  const amounts = Array.from(graph.querySelectorAll('p')).map(amountText).filter(money);
  if (amounts.length === 1) accountValue = amounts[0] + ' CAD';
}
const labels = Array.from(document.querySelectorAll('span')).filter(e =>
  e.children.length === 0 && e.innerText.trim() === 'Total CAD cash available');
let totalCash = null, nativeCash = [];
if (labels.length === 1) {
  let card = labels[0].parentElement;
  for (let i = 0; card && i < 3; i++, card = card.parentElement) {
    const icons = Array.from(card.querySelectorAll('svg')).filter(e =>
      e.querySelector('clipPath[id^="mint-currency-cad-"],clipPath[id^="mint-currency-usd-"]'));
    if (!icons.length) continue;
    const first = amountText(labels[0].parentElement.nextElementSibling);
    if (money(first)) totalCash = first + ' CAD';
    nativeCash = icons.map(icon => {
      const currency = icon.querySelector('clipPath[id^="mint-currency-cad-"]') ? 'CAD' : 'USD';
      const values = icon.parentElement.innerText.split('\n').map(s => s.trim()).filter(money);
      return {currency, amount: values.length === 1 ? values[0] + ' ' + currency : null};
    });
    break;
  }
}
return {account: document.querySelector('h1')?.innerText.trim(),
  account_value: accountValue, total_cash_cad: totalCash, native_cash: nativeCash};
"""


def native_cash_value(snapshot, currency):
    """A native balance requires one unambiguous broker currency icon row."""
    rows = [r for r in snapshot.get("native_cash", []) if r.get("currency") == currency]
    return rows[0].get("amount") if len(rows) == 1 else None
