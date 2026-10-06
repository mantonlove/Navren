"""为已发布热点生成版本绑定的短卡文案；不修改采集缓存或周报。"""
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from market_editor import MarketEditor, EDITOR_SYSTEM, REVIEW_SYSTEM, numbers, require_text
from market_sources import now_iso


def build():
    with urllib.request.urlopen('http://127.0.0.1:18767/api/market/hotspots', timeout=10) as response:
        data = json.load(response)
    events = data['events']
    materials = [{k: e[k] for k in ('id', 'headline', 'summary', 'facts', 'analysis', 'uncertainty', 'published_at', 'articles')} for e in events]
    schema = {'events': [{'id': '原事件id', 'display_title': '18—32字准确短标题', 'display_summary': '30—55字事实短说明', 'topic_id': 'macro|rates|household|international|general', 'card_template': 'data|policy|commentary', 'display_fact': {'label': '指标或焦点名称', 'value': '仅data卡使用，无则空串。policy/commentary必须空串', 'unit': '原单位，无则空串', 'period': 'value非空时必须有原指标期间，如2026年9月，无则空串', 'text': '政策适用条件或观点讨论焦点，无则空串', 'evidence_refs': [{'article_id': '对应事实原文id', 'fact_index': 0}]}, 'display_conditions': '不能删除的限定条件，无则空串'}]}
    editor = MarketEditor()
    request_id = 'market-display-' + data['content_version']
    if len(sys.argv) == 3 and sys.argv[1] == '--review-draft':
        draft = json.loads(Path(sys.argv[2]).read_text())
    else:
        draft = editor.primary.complete_json(system=EDITOR_SYSTEM, user=json.dumps({'task': '生成紧凑卡面，不复制原标题与长摘要。硬限制display_title最多32字符，display_summary最多55字符，display_fact.label最多14字符、text最多35字符，只选一个事实而非枚举政策；display_conditions最多35字符。可将非核心细节留在原详情，核心条件不可删除。住房标题必须包含“符合条件”，贴息不是利率。观点只选一个讨论焦点，不枚举所有预测数值。PMI标题聚焦制造业，单位期间在重点显示。主题按内容。policy/commentary的value/unit/period均为空串；text须可追溯对应facts。无重点则null。只返回schema。', 'schema': schema, 'materials': materials}, ensure_ascii=False), stage='market.display', request_id=request_id)
    proof = ROOT / 'output/playwright/market-deck/display-review.json'
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_text(json.dumps({'draft': draft, 'review': {'verdict': 'validation_pending'}}, ensure_ascii=False, indent=2))
    rows = draft.get('events')
    if not isinstance(rows, list) or len(rows) != len(events):
        raise ValueError('展示文案数量不匹配')
    refs = {e['id']: e for e in events}
    seen = set()
    for row in rows:
        if row.get('id') not in refs or row['id'] in seen:
            raise ValueError('展示文案事件未绑定')
        seen.add(row['id'])
        e = refs[row['id']]
        require_text(row.get('display_title'), 'display_title', 32)
        require_text(row.get('display_summary'), 'display_summary', 55)
        if len(row.get('display_conditions', '')) > 35:
            raise ValueError('重点条件过长')
        if row.get('topic_id') not in ('macro', 'rates', 'household', 'international', 'general') or row.get('card_template') not in ('data', 'policy', 'commentary'):
            raise ValueError('展示主题或模板不合法')
        fact = row.get('display_fact')
        if fact:
            if len(fact.get('text', '')) > 35 or len(fact.get('label', '')) > 14:
                raise ValueError('重点应仅选一个事实')
            if not fact.get('evidence_refs'):
                raise ValueError('重点缺少事实引用')
            for ref in fact['evidence_refs']:
                index = ref.get('fact_index')
                if type(index) is not int or not 0 <= index < len(e['facts']) or e['facts'][index]['article_id'] != ref.get('article_id'):
                    raise ValueError('重点引用不匹配')
            if fact.get('value') and (not fact.get('unit') or not fact.get('period')):
                raise ValueError('指标缺单位或期间')
        public_text = json.dumps({k: row[k] for k in ('display_title', 'display_summary', 'display_fact', 'display_conditions')}, ensure_ascii=False)
        # 证据索引不是新闻数字，不参与数值检查。
        if fact:
            public_text = row['display_title'] + row['display_summary'] + row['display_conditions'] + ' '.join(str(fact.get(k, '')) for k in ('label', 'value', 'unit', 'period', 'text'))
        if numbers(public_text) - numbers(json.dumps(e, ensure_ascii=False)):
            raise ValueError('短卡包含无来源数字')
    result = editor.reviewer.complete_json(system=REVIEW_SYSTEM, user=json.dumps({'kind': '版本绑定的热点短卡展示字段', 'draft': draft, 'materials': materials}, ensure_ascii=False), stage='reviewer.market', request_id=request_id)
    proof = ROOT / 'output/playwright/market-deck/display-review.json'
    proof.parent.mkdir(parents=True, exist_ok=True)
    proof.write_text(json.dumps({'draft': draft, 'review': result}, ensure_ascii=False, indent=2))
    if result.get('verdict') != 'approve':
        print(json.dumps(result, ensure_ascii=False))
        raise ValueError('短文案审核拒绝，未发布；意见保存在验收目录')
    review = {'status': 'approved', 'primary_model': editor.primary.model_id, 'reviewer_model': editor.reviewer.model_id, 'reviewed_at': now_iso(), 'reason': require_text(result.get('reason'), 'reason', 800)}
    for row in rows:
        e = refs[row['id']]
        row.update(event_version=e['version'], content_version=e['content_version'], article_versions={a['id']: a['version'] for a in e['articles']})
        row['display_version'] = hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
    packet = {'schema_version': 1, 'generated_at': now_iso(), 'review': review, 'events': rows}
    target = ROOT / 'assets/market_display.json'
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(target)
    print(json.dumps({'status': 'approved', 'events': len(rows), 'topics': sorted({r['topic_id'] for r in rows}), 'review': review}, ensure_ascii=False))


if __name__ == '__main__':
    build()
