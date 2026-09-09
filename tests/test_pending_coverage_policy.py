import pytest
from src.sell_coverage import coverage_quantity, quantity_evidence, sell_coverage


def order(status='Pending', **extras):
    return {'account': 'RRSP', 'ticker': 'XYZ', 'side': 'sell', 'status': status,
            'security_quote_currency': 'CAD', 'order_id': 'a',
            **quantity_evidence('2 shares', None, None), **extras}


@pytest.mark.parametrize('status', ['Partially filled', 'Pending cancellation', 'Pending replacement', 'Open', 'Unknown'])
def test_only_plain_pending_uses_entered(status):
    assert coverage_quantity(order(status))[0] is None


@pytest.mark.parametrize('record', [{'fillQuantity': '1'}, {'fillQuantity': 'invalid'},
    {'status': 'PARTIALLY_FILLED'}, {'lastFilledAtUtc': '2026-09-09'}, {'submittedQuantity': '3'}])
def test_partial_or_conflicting_metadata_blocks_fallback(record):
    assert coverage_quantity(order(order_metadata={'record': record}))[0] is None


def test_null_not_converted_to_zero_and_explicit_quantity_wins():
    o = order(order_metadata={'record': {'fillQuantity': None, 'status': 'SUBMITTED'}})
    assert str(coverage_quantity(o)[0]) == '2'
    assert o['remaining_quantity'] is None
    assert o['order_metadata']['record']['fillQuantity'] is None
    o.update(quantity_evidence('2 shares', '1 shares', None))
    assert str(coverage_quantity(o)[0]) == '1'


def test_accounts_separate_and_scope_still_required():
    hs = [{'account': a, 'ticker': 'XYZ', 'quantity': '12', 'security_quote_currency': 'CAD'} for a in ['RRSP', 'TFSA']]
    os = [order(), order(order_id='b', **quantity_evidence('3 shares', None, None))]
    results = sell_coverage(hs, os, lambda s:s, inventory_complete=True)
    rrsp = next(r for r in results if r['account']=='RRSP')
    tfsa = next(r for r in results if r['account']=='TFSA')
    assert (rrsp['open_sell_remaining'], rrsp['uncovered_quantity']) == ('5', '7')
    assert tfsa['open_sell_remaining'] == '0'
    incomplete = sell_coverage(hs, os, lambda s:s, inventory_complete=False)
    assert all(r['uncovered_quantity'] is None for r in incomplete)
