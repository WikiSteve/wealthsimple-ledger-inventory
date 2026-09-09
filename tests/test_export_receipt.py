from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import pytest

from src.export_receipt import activity_download_proof, validate_activity_receipt
from src.full_account_inventory import RunState, ensure_dirs, import_exports, has_fresh_canonical_activity_export, build_parser
from scripts.download_wealthsimple_activity_csv import audit_command_for_activity_export


def fixture(tmp_path):
    csv = tmp_path/'activity.csv'
    csv.write_text('transaction_date,account_type,activity_type,description\n2026-09-09,TFSA,Trade,Bought GOOG\n')
    now = datetime.now(timezone.utc)
    r = dict(activity_download_proof(csv), csv_path=str(csv), kind='activities',
             period='last_12_months', account_scope='all_accounts', downloaded_at=now.isoformat())
    path = tmp_path/'receipt.json'
    path.write_text(json.dumps(r))
    return csv, path, r, now


def test_real_command_parser_import_selects_fast_path_without_footer(tmp_path):
    csv, receipt, r, now = fixture(tmp_path)
    command = audit_command_for_activity_export(csv, activity_receipt=receipt)
    args = build_parser().parse_args(command[2:])
    out = tmp_path/'bundle'
    ensure_dirs(out)
    state = RunState(out_dir=out, mode='FULL')
    control = import_exports(state, csv, None)
    assert not has_fresh_canonical_activity_export(control)
    treatment = import_exports(state, Path(args.activity_export), None, Path(args.activity_download_receipt))
    assert has_fresh_canonical_activity_export(treatment)
    assert treatment['activity_export']['as_of'] is None
    assert treatment['activity_export']['freshness_basis'] == 'hash_bound_download_receipt'
    assert treatment['activity_export']['download_receipt']['sha256'] == r['sha256']


@pytest.mark.parametrize('change', [
    {'sha256':'0'*64}, {'bytes':1}, {'account_scope':'one_account'},
    {'kind':'holdings'}, {'period':'last_month'}, {'analysis_only':True},
    {'source_url':'https://example.com'}, {'receipt_version':0}, {'csv_path':'/wrong.csv'},
    {'downloaded_at':'2026-09-09T00:00:00'}, {'downloaded_at':'garbage'},
])
def test_invalid_receipts_fail_closed(tmp_path, change):
    csv, path, r, now = fixture(tmp_path)
    digest = r['sha256']
    r.update(change);path.write_text(json.dumps(r))
    with pytest.raises(ValueError):
        validate_activity_receipt(path,csv,digest,now=now)


@pytest.mark.parametrize('hours', [-1,25])
def test_future_and_stale_receipts_rejected(tmp_path,hours):
    csv,path,r,now=fixture(tmp_path)
    r['downloaded_at']=(now-timedelta(hours=hours)).isoformat()
    path.write_text(json.dumps(r))
    with pytest.raises(ValueError): validate_activity_receipt(path,csv,r['sha256'],now=now)


def test_changed_csv_does_not_get_fast_path(tmp_path):
    csv,path,r,now=fixture(tmp_path)
    csv.write_text(csv.read_text().replace('GOOG','MSFT'))
    out=tmp_path/'bundle';ensure_dirs(out)
    result=import_exports(RunState(out_dir=out,mode='FULL'),csv,None,path)
    assert not has_fresh_canonical_activity_export(result)


def test_bad_json_receipt_falls_back_not_crash(tmp_path):
    csv,path,r,now=fixture(tmp_path)
    path.write_text('{')
    out=tmp_path/'bundle';ensure_dirs(out)
    result=import_exports(RunState(out_dir=out,mode='FULL'),csv,None,path)
    assert not has_fresh_canonical_activity_export(result)
