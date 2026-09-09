"""Local download provenance, not a broker signature or a file-mtime heuristic."""
from datetime import datetime
import hashlib
import json
from pathlib import Path

ACTIVITY_SOURCE = 'https://my.wealthsimple.com/app/activity'


def activity_download_proof(csv_path: Path) -> dict:
    data = csv_path.read_bytes()
    return {'receipt_version': 1, 'source_url': ACTIVITY_SOURCE,
            'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}


def validate_activity_receipt(receipt_path: Path, csv_path: Path, digest: str,
                              *, now=None, max_age_hours=24) -> dict:
    """Validate an explicitly supplied, locally generated receipt. Fail closed."""
    try:
        if receipt_path.stat().st_size > 1024*1024:
            raise ValueError('oversized receipt')
        r = json.loads(receipt_path.read_text(encoding='utf-8'))
        if not isinstance(r, dict):
            raise ValueError('receipt is not an object')
        expected = {'receipt_version': 1, 'kind': 'activities', 'period': 'last_12_months',
                    'account_scope': 'all_accounts', 'source_url': ACTIVITY_SOURCE}
        if any(r.get(k) != v for k, v in expected.items()) or r.get('analysis_only'):
            raise ValueError('receipt scope/source mismatch')
        if Path(r['csv_path']).resolve() != csv_path.resolve():
            raise ValueError('CSV path mismatch')
        if r.get('sha256') != digest or r.get('bytes') != csv_path.stat().st_size:
            raise ValueError('CSV content mismatch')
        timestamp = datetime.fromisoformat(r['downloaded_at'])
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError('download time lacks timezone')
        age = ((now or datetime.now().astimezone()) - timestamp).total_seconds()/3600
        if not 0 <= age <= max_age_hours:
            raise ValueError('download time is future or stale')
        return {'downloaded_at': timestamp.isoformat(), 'age_hours': age,
                'sha256': digest, 'bytes': r['bytes'], 'validated': True}
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        raise ValueError('Activity download receipt rejected: ' + type(exc).__name__) from exc
