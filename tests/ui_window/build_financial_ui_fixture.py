"""有界读取独立库生成前端合同夹具；不启动HTTP、不修改业务库。"""
import json
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from financial_data_service import FinancialDataService
from financial_data_service.presentation import FinancialPresentationService

service = FinancialDataService(ROOT / 'outputs/financial-data/navren-financial-v2.sqlite3')
presentation = FinancialPresentationService(service)
with sqlite3.connect(f'file:{service.database_path}?mode=ro', uri=True) as db:
    subjects = [dict(zip(('subject_id', 'workspace_id'), row)) for row in db.execute('SELECT subject_id,workspace_id FROM subjects')]
fixture = {'dataset_version': service.dataset_version, 'subjects': subjects, 'responses': {}}

def put(subject, operation, arguments):
    result = presentation.query(subject['subject_id'], arguments) if operation == 'presentation' else service.query(subject['subject_id'], operation, arguments)
    if result.get('error'):
        raise RuntimeError(result['error'])
    result.update(workspace_id=subject['workspace_id'], workspace_binding=subject['workspace_id'], privacy_revision=42, read_only=True, runtime_ledger_included=False)
    key = subject['subject_id'] + '/' + operation + '?' + urlencode(sorted(arguments.items()))
    fixture['responses'][key] = result
    return result

for subject in subjects:
    put(subject, 'presentation', {'view': 'overview'})

subject = next(subject for subject in subjects if subject['subject_id'] == 'working_landlady')
overview = fixture['responses'][subject['subject_id'] + '/presentation?view=overview']['data']
for account in overview['accounts']:
    put(subject, 'get_account_overview', {'account_id': account['account_id']})
for status in ['active', 'expired', 'cancelled', 'pending']:
    policies = put(subject, 'list_insurance', {'status': status})['data']['items']
    if policies:
        put(subject, 'get_policy', {'policy_id': policies[0]['policy_id']})
for month in ['2026-09', '2026-10']:
    report = put(subject, 'presentation', {'view': 'month_report', 'month': month})['data']
    period = report['period']
    from datetime import date, timedelta
    day, end = date.fromisoformat(period['start_date']), date.fromisoformat(period['end_date'])
    dates = []
    while day <= end:
        dates.append(day.isoformat()); day += timedelta(days=7)
    if dates[-1] != end.isoformat(): dates.append(end.isoformat())
    for as_of in dates:
        put(subject, 'get_financial_summary', {'as_of': as_of})
    import calendar
    ranges = {'start_date': month+'-01', 'end_date': month+'-'+str(calendar.monthrange(int(month[:4]), int(month[5:]))[1])}
    page = put(subject, 'list_transactions', {**ranges, 'limit': 1, 'offset': 0})['data']
    put(subject, 'list_transactions', {**ranges, 'limit': 8, 'offset': max(0, page['total']-8)})
    put(subject, 'list_transactions', {**ranges, 'limit': 50, 'offset': 0})
    if page['total'] > 50:
        put(subject, 'list_transactions', {**ranges, 'limit': 50, 'offset': 50})
    put(subject, 'presentation', {'view': 'calendar', **ranges})
account = overview['accounts'][0]['account_id']
put(subject, 'presentation', {'view': 'month_report', 'month': '2026-10', 'account_id': account})
put(subject, 'list_transactions', {'start_date': '2026-10-01', 'end_date': '2026-10-31', 'limit': 50, 'offset': 0, 'account_id': account})
put(subject, 'list_transactions', {'start_date': '2026-10-03', 'end_date': '2026-10-03', 'limit': 100, 'offset': 0})
target = ROOT / 'outputs/window-progress/ui/financial-data/fixture.json'
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(fixture, ensure_ascii=False))
print(json.dumps({'path': str(target), 'responses': len(fixture['responses']), 'database_read_only': True}))
