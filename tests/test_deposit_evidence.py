from src.deposit_evidence import parse_deposit_availability, render_deposits

TEXT = """Deposit
Electronic funds transfer
TFSA
In progress
$200.00 CAD
To
TFSA
Date
September 3, 2026
Estimated completion
September 10, 2026
Available to trade instantly
Can’t be withdrawn or transferred until your deposit is complete.
$150.00
Amount
$200.00 CAD
View status updates
"""


def test_explicit_availability_and_handoff():
    r = parse_deposit_availability(TEXT)
    assert r['amount_not_instantly_available_cad'] == '50.00'
    assert r['availability_evidence_valid'] is True
    assert r['estimated_completion'] == 'September 10, 2026'
    assert 'not a verified current hold' in '\n'.join(render_deposits([r]))


def test_missing_or_conflicting_evidence_never_defaults_to_zero():
    for text in [TEXT.replace('$150.00', ''), TEXT.replace('$150.00', '$250.00'),
                 TEXT.replace('$200.00 CAD\nView', '$200.00 USD\nView'),
                 TEXT + '\nAmount\n$200.00 CAD',
                 TEXT.replace('$150.00', '$150.00\n$140.00')]:
        assert parse_deposit_availability(text)['amount_not_instantly_available_cad'] is None


def test_zero_is_valid_when_explicit():
    assert parse_deposit_availability(TEXT.replace('$150.00', '$0.00'))['amount_not_instantly_available_cad'] == '200.00'


def test_actual_chatgpt_handoff_includes_deposit_evidence():
    from src.full_account_inventory import render_next_message
    from src.deposit_evidence import compare_deposit_residuals
    r = parse_deposit_availability(TEXT)
    r.update(observed_at='2026-09-09T13:00:00-03:00', evidence_file='synthetic/deposit.txt')
    text = render_next_message({'zip_path':'synthetic.zip','status':'WARN','accounts_seen':[]},
                               {'accounts':[], 'holdings':[], 'open_orders':[], 'recent_activity':[],
                                'paired_exit_checks':[], 'duplicate_checks':[], 'warnings':[], 'blockers':[],
                                'deposit_availability':[r],
                                'deposit_residual_comparison':compare_deposit_residuals(
                                    {'TFSA':{'components_captured':True,'residual_cad':'50.00'}},
                                    [r],inventory_complete=True)})
    assert 'instant availability CAD 150.00' in text
    assert 'synthetic/deposit.txt' in text
    assert 'Do not silently subtract' in text
    assert 'Numerically reconciled under the pending-deposit interpretation' in text
    assert 'not broker confirmation' in text


def test_comparison_exact_and_difference_without_mutating_original():
    from copy import deepcopy
    from src.deposit_evidence import compare_deposit_residuals, render_deposit_comparisons
    d = parse_deposit_availability(TEXT)
    d.update(observed_at='2026-09-09T13:00:00-03:00', evidence_file='synthetic.txt')
    for residual, assessment, difference in [('50.00','exact_numeric_match','0.00'),
                                              ('49.85','remaining_difference','-0.15')]:
        rec = {'TFSA':{'components_captured':True,'residual_cad':residual,'status':'residual_unexplained'}}
        original = deepcopy(rec)
        r = compare_deposit_residuals(rec,[d],inventory_complete=True)[0]
        assert rec == original
        assert r['assessment'] == assessment
        assert r['residual_after_interpretation_cad'] == difference
        assert r['broker_confirmed_current_hold'] is False
        assert 'Original residual and warning remain unchanged' in '\n'.join(render_deposit_comparisons([r]))


def test_comparison_refuses_incomplete_wrong_currency_and_duplicates():
    from src.deposit_evidence import compare_deposit_residuals
    d = parse_deposit_availability(TEXT)
    d.update(observed_at='2026-09-09T13:00:00-03:00', evidence_file='synthetic.txt')
    rec={'TFSA':{'components_captured':True,'residual_cad':'50.00'}}
    for patch in [{'currency':'USD'}, {'status':'Completed'}, {'observed_at':None},
                  {'availability_evidence_valid':False}, {'instant_available_cad':'NaN'},
                  {'amount_not_instantly_available_cad':'49'}, {'deposit_amount_cad':None}]:
        r=compare_deposit_residuals(rec,[d|patch],inventory_complete=True)[0]
        assert r['assessment']=='comparison_unavailable'
        assert r['residual_after_interpretation_cad'] is None
    assert compare_deposit_residuals(rec,[d])[0]['assessment']=='comparison_unavailable'
    assert compare_deposit_residuals(rec,[d,d],inventory_complete=True)[0]['assessment']=='comparison_unavailable'
    assert compare_deposit_residuals(rec,[d|{'account':'RRSP'}],inventory_complete=True)==[]
