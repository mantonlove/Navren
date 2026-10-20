"""可重建的公共句段索引，召回单位不是新证据或新引文。

小单位独立编码；父段只映射回同版原件 IDs。查询路径只读，不下载/建库。
参考 Haystack 的层级检索思想；没有引入框架或复制其实现。
"""
from __future__ import annotations

from collections import Counter
from bisect import bisect_right
from dataclasses import asdict
import json
import math
import time

from .store import KnowledgeError, now, packed, sha, tokens
from .vector_documents import representation
from .vectors import VectorIndex, fuse

POLICY = 'public-sentence-parent-index/2'
CANONICAL_WINDOW_POLICY = 'canonical-sentence-window/1'
MAX_CORE_CHARS = 6400
MAX_CORE_CITATIONS = 8
MAX_CONTEXT_CHARS = 1600
SCHEMA = '''
CREATE TABLE IF NOT EXISTS structure_releases(
 release_id TEXT NOT NULL REFERENCES releases(id), policy_id TEXT NOT NULL,
 release_manifest TEXT NOT NULL,unit_manifest TEXT NOT NULL,payload TEXT NOT NULL,
 PRIMARY KEY(release_id,policy_id));
CREATE TABLE IF NOT EXISTS structure_vector_releases(
 release_id TEXT NOT NULL REFERENCES releases(id),policy_id TEXT NOT NULL,profile_id TEXT NOT NULL,
 unit_manifest TEXT NOT NULL,vector_manifest TEXT NOT NULL,dimension INTEGER NOT NULL,
 vector_count INTEGER NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(release_id,policy_id,profile_id));
CREATE TABLE IF NOT EXISTS structure_vectors(
 release_id TEXT NOT NULL REFERENCES releases(id),policy_id TEXT NOT NULL,profile_id TEXT NOT NULL,
 unit_id TEXT NOT NULL,window_no INTEGER NOT NULL,text_sha256 TEXT NOT NULL,
 vector BLOB NOT NULL,vector_sha256 TEXT NOT NULL,
 PRIMARY KEY(release_id,policy_id,profile_id,unit_id,window_no));
'''


def reason(error):
    code = getattr(error, 'code', None)
    if code in {'structure_index_missing', 'structure_index_invalid', 'structure_build_timeout',
                'structure_unit_limit', 'structure_empty', 'release_manifest_mismatch'}:
        return code
    return VectorIndex._reason(error)


def _core_records(text, built):
    """恢复预算拆分前的原句范围；父段预算不再成为句子核心边界。"""
    from .retrieval_units import _sentence_spans
    parent_by_id = {unit.id: unit for unit in built.parents}
    sentence_issues = sorted((issue.start_char, issue.end_char) for issue in built.issues
                             if issue.code == 'child_budget_split')
    paragraph_issues = sorted((issue.start_char, issue.end_char) for issue in built.issues
                              if issue.code == 'parent_budget_split')
    sentence_starts = [start for start, _ in sentence_issues]
    paragraph_starts = [start for start, _ in paragraph_issues]
    natural = list(_sentence_spans(text, 0, len(text)))
    natural_starts = [start for start, _ in natural]
    groups = {}
    def containing(ranges, starts, start, end):
        index = bisect_right(starts, start)-1
        return ranges[index] if index >= 0 and end <= ranges[index][1] else None
    for child in built.children:
        sentence = containing(sentence_issues, sentence_starts, child.start_char, child.end_char)
        sentence = sentence or (child.start_char, child.end_char)
        parent = parent_by_id[child.parent_id]
        paragraph = containing(paragraph_issues, paragraph_starts, child.start_char, child.end_char)
        paragraph = paragraph or (parent.start_char, parent.end_char)
        key = (child.version_id, child.boundary_key, sentence, paragraph)
        groups.setdefault(key, []).append(child)
    cores = []; by_child = {}
    for (version, boundary, span, paragraph), children in sorted(groups.items()):
        children.sort(key=lambda unit: (unit.start_char, unit.end_char, unit.id))
        citations = list(dict.fromkeys(cid for child in children for cid in child.citation_ids))
        natural_span = containing(natural, natural_starts, span[0], span[1])
        complete = (natural_span == span and span[1]-span[0] <= MAX_CORE_CHARS
                    and len(citations) <= MAX_CORE_CITATIONS)
        group_id = 'sg-' + sha(packed([POLICY, version, boundary, span,
                            [child.id for child in children], citations]))
        core = {'group_id': group_id, 'kind': 'sentence_core', 'core': True,
                'version_id': version, 'boundary_key': boundary,
                'start_char': span[0], 'end_char': span[1],
                'paragraph_start': paragraph[0], 'paragraph_end': paragraph[1],
                'child_ids': [child.id for child in children],
                'parent_ids': list(dict.fromkeys(child.parent_id for child in children)),
                'citation_ids': citations, 'complete': complete,
                'continuation': len(children) > 1,
                'completion_reason': None if complete else 'core_completion_limit',
                'previous_core_id': None, 'next_core_id': None}
        cores.append(core)
        by_child.update((child.id, group_id) for child in children)
    paragraphs = {}
    for core in cores:
        key = (core['version_id'], core['boundary_key'], core['paragraph_start'], core['paragraph_end'])
        paragraphs.setdefault(key, []).append(core)
    for members in paragraphs.values():
        members.sort(key=lambda core: (core['start_char'], core['end_char'], core['group_id']))
        for left, right in zip(members, members[1:]):
            if (left['complete'] and right['complete']
                    and not text[left['end_char']:right['start_char']].strip()):
                left['next_core_id'] = right['group_id']
                right['previous_core_id'] = left['group_id']
    return cores, by_child


def _context_options(core, cores, eligible_ids):
    """最多相邻前后各一句；同边界、同自然段，可跨预算父段。"""
    neighbors = [cores.get(core.get('previous_core_id')), cores.get(core.get('next_core_id'))]
    neighbors = [neighbor for neighbor in neighbors if neighbor and neighbor['complete']
                 and neighbor['version_id'] == core['version_id']
                 and neighbor['boundary_key'] == core['boundary_key']
                 and (neighbor['paragraph_start'],neighbor['paragraph_end']) ==
                     (core['paragraph_start'],core['paragraph_end'])
                 and set(neighbor['citation_ids']) <= eligible_ids]
    variants = [neighbors] if len(neighbors) == 2 else []
    variants.extend([neighbor] for neighbor in neighbors)
    options = []
    for extra in variants:
        members = sorted([core, *extra], key=lambda value: value['start_char'])
        start, end = members[0]['start_char'], members[-1]['end_char']
        if end-start > MAX_CONTEXT_CHARS:
            continue
        ids = list(dict.fromkeys(cid for member in members for cid in member['citation_ids']))
        if len(ids) > MAX_CORE_CITATIONS:
            continue
        options.append({'group_id': 'cx-'+sha(packed([POLICY, core['group_id'],
            [member['group_id'] for member in members], ids])),
            'kind': 'context_supplement', 'core': False, 'complete': True,
            'version_id': core['version_id'], 'boundary_key': core['boundary_key'],
            'start_char': start, 'end_char': end, 'citation_ids': ids,
            'child_ids': list(dict.fromkeys(uid for member in members for uid in member['child_ids'])),
            'parent_ids': list(dict.fromkeys(pid for member in members for pid in member['parent_ids'])),
            'required_group_id': core['group_id'], 'score': 0.0})
    return options


def _canonical_sentence_windows(text, rows, cores, eligible_ids):
    """将原 canonical 范围扩展到所有相交完整自然句，保留失败约束。

    这是查询时的纯派生视图，不改变 /2 的单位载荷、向量或原件。窗口
    包含原范围本身及边缘完整句；完整映射、严格边界和固定预算同时成立。
    """
    from .retrieval_units import _sentence_spans
    from .evidence_group_selection import _boundary
    rows = sorted((dict(row) for row in rows),
                  key=lambda row: (row['start_char'],row['end_char'],row['id']))
    if not rows:
        return []
    sentences = list(_sentence_spans(text,0,len(text)))
    sentence_ends = [end for _,end in sentences]
    by_span = {}
    for core in cores:
        by_span.setdefault((core['start_char'],core['end_char']),[]).append(core)
    result = []
    for anchor in rows:
        if anchor['id'] not in eligible_ids:
            continue
        original_start,original_end = anchor['start_char'],anchor['end_char']
        index = bisect_right(sentence_ends,original_start)
        intersecting = []
        while index < len(sentences) and sentences[index][0] < original_end:
            intersecting.append(sentences[index]);index += 1
        start = min(original_start,intersecting[0][0]) if intersecting else original_start
        end = max(original_end,intersecting[-1][1]) if intersecting else original_end
        members = [row for row in rows if row['start_char'] < end and row['end_char'] > start]
        citations = [row['id'] for row in members]
        boundary = _boundary(anchor)
        why = None
        if any(_boundary(row) != boundary for row in members):
            why = 'mixed_boundary'
        elif not set(citations) <= eligible_ids:
            why = 'not_eligible'
        elif end-start > MAX_CORE_CHARS or len(citations) > MAX_CORE_CITATIONS:
            why = 'core_completion_limit'
        cursor = start
        for row in members:
            if row['start_char'] > cursor:
                break
            cursor = max(cursor,row['end_char'])
        if why is None and (anchor['id'] not in citations or cursor < end):
            why = 'invalid_span'
        matching = []
        for span in intersecting:
            exact = by_span.get(span,())
            if len(exact) != 1 or not exact[0]['complete']:
                if why is None:why = 'core_completion_limit'
                continue
            core = exact[0]
            if (core['version_id'] != anchor['version_id']
                    or not set(core['citation_ids']) <= set(citations)):
                if why is None:why = 'invalid_span'
            matching.append(core)
        if not intersecting and why is None:
            why = 'core_completion_limit'
        identity = [CANONICAL_WINDOW_POLICY,POLICY,anchor['version_id'],anchor['id'],
                    boundary,start,end,citations,[core['group_id'] for core in matching],why]
        result.append({'group_id':'cw-'+sha(packed(identity)),
            'kind':'canonical_sentence_window','core':True,'complete':why is None,
            'version_id':anchor['version_id'],'canonical_anchor_id':anchor['id'],
            'boundary_key':'cw-boundary-'+sha(packed(boundary)),
            'start_char':start,'end_char':end,'citation_ids':citations,
            'child_ids':list(dict.fromkeys(uid for core in matching for uid in core['child_ids'])),
            'parent_ids':list(dict.fromkeys(pid for core in matching for pid in core['parent_ids'])),
            'window_core_ids':[core['group_id'] for core in matching],
            'completion_reason':why,'rejection':why,'score':0.0,
            'context_options':[],'matched_child_ids':[],'child_rank':None,
            'recall_rank_positions':{}})
    return result


class StructureIndex:
    def __init__(self, provider, encoder=None):
        self.provider = provider
        self.encoder = encoder

    def _derive(self, db, release_id):
        from .retrieval_units import build_units
        if not self.provider._release_manifest_valid(db, release_id):
            raise KnowledgeError('release_manifest_mismatch')
        rows = self.provider._rows(db, release_id)
        market = [dict(r) for r in rows if representation(r)['kind'] == 'market_full_text']
        groups = {}
        for row in market:
            groups.setdefault(row['version_id'], []).append(row)
        parents = []; children = []; issues = []; cores = []
        for version_id, members in sorted(groups.items()):
            version = db.execute('SELECT parsed_text,parsed_sha256 FROM document_versions WHERE id=?',
                                 (version_id,)).fetchone()
            if version is None or sha(version['parsed_text'].encode()) != version['parsed_sha256']:
                raise KnowledgeError('structure_index_invalid')
            built = build_units(version['parsed_text'], members, version_id=version_id)
            version_cores, by_child = _core_records(version['parsed_text'], built)
            cores.extend(version_cores)
            parents.extend(asdict(unit) for unit in built.parents)
            for unit in built.children:
                value = asdict(unit)
                value['embedding_text'] = members[0]['title'][:120] + '\n' + unit.text
                value['core_group_id'] = by_child[unit.id]
                children.append(value)
            issues.extend(dict(asdict(issue),version_id=version_id) for issue in built.issues)
        if len(parents) + len(children) > 20000:
            raise KnowledgeError('structure_unit_limit')
        payload = {'policy': POLICY, 'parents': sorted(parents, key=lambda u: u['id']),
                   'children': sorted(children, key=lambda u: u['id']), 'issues': issues,
                   'cores': sorted(cores, key=lambda group: group['group_id'])}
        return rows, payload, sha(packed(payload))

    def _load_units(self, db, release_id):
        rows, expected, digest = self._derive(db, release_id)
        meta = db.execute('SELECT * FROM structure_releases WHERE release_id=? AND policy_id=?',
                          (release_id, POLICY)).fetchone()
        if meta is None:
            raise KnowledgeError('structure_index_missing')
        manifest = db.execute('SELECT manifest_sha256 FROM releases WHERE id=?', (release_id,)).fetchone()[0]
        if (meta['release_manifest'] != manifest or meta['unit_manifest'] != digest
                or meta['payload'] != packed(expected)):
            raise KnowledgeError('structure_index_invalid')
        return rows, expected, digest

    @staticmethod
    def _vector_manifest(rows):
        return sha(packed([(r['unit_id'], r['window_no'], r['text_sha256'], r['vector_sha256']) for r in rows]))

    def _load_vectors(self, db, release_id, member, payload, digest):
        import numpy as np
        meta = db.execute('''SELECT * FROM structure_vector_releases
            WHERE release_id=? AND policy_id=? AND profile_id=?''',
            (release_id, POLICY, member.profile_id)).fetchone()
        if meta is None:
            raise KnowledgeError('structure_index_missing')
        rows = db.execute('''SELECT * FROM structure_vectors
            WHERE release_id=? AND policy_id=? AND profile_id=? ORDER BY unit_id,window_no''',
            (release_id, POLICY, member.profile_id)).fetchall()
        expected = {u['id']: sha(packed([POLICY, u['embedding_text']])) for u in payload['children']}
        maximum = VectorIndex._max_windows(member)
        if (meta['unit_manifest'] != digest or meta['dimension'] != member.dimension
                or meta['vector_count'] != len(rows) or not len(expected) <= len(rows) <= len(expected)*maximum
                or set(r['unit_id'] for r in rows) != set(expected)
                or meta['vector_manifest'] != self._vector_manifest(rows)):
            raise KnowledgeError('structure_index_invalid')
        ids = []; matrix = []; windows = {}
        for row in rows:
            uid = row['unit_id']; blob = bytes(row['vector'])
            if (row['text_sha256'] != expected[uid] or row['window_no'] != windows.get(uid, 0)
                    or len(blob) != member.dimension*4 or sha(blob) != row['vector_sha256']):
                raise KnowledgeError('structure_index_invalid')
            vec = np.frombuffer(blob, dtype='<f4')
            if not np.isfinite(vec).all() or abs(float(np.linalg.norm(vec))-1) > 0.002:
                raise KnowledgeError('structure_index_invalid')
            windows[uid] = row['window_no'] + 1
            if windows[uid] > maximum:
                raise KnowledgeError('structure_index_invalid')
            ids.append(uid); matrix.append(vec)
        return ids, np.stack(matrix) if matrix else np.empty((0, member.dimension), dtype=np.float32)

    def build(self, release_id, *, max_seconds=300):
        import numpy as np
        started = time.monotonic()
        selected = VectorIndex(self.provider, self.encoder)._encoder()
        from .retrieval_encoder import is_bundle
        factories = selected.factories if is_bundle(selected) else (lambda: selected,)
        with self.provider.db() as db:
            db.execute('BEGIN')
            rows, payload, digest = self._derive(db, release_id)
            VectorIndex(self.provider)._build_rows_valid(db, rows)
            manifest = db.execute('SELECT manifest_sha256 FROM releases WHERE id=?', (release_id,)).fetchone()[0]
        if not payload['children']:
            return {'ready': True, 'applicable': False, 'policy': POLICY, 'child_count': 0, 'parent_count': 0}
        results = []; all_generated = []
        for factory in factories:
            member = factory(); generated = []
            with self.provider.db() as db:
                try:
                    self._load_units(db, release_id)
                    self._load_vectors(db, release_id, member, payload, digest)
                    results.append({'profile_id': member.profile_id, 'reused': True})
                    continue
                except KnowledgeError:
                    pass
            for start in range(0, len(payload['children']), 8):
                if time.monotonic()-started > max_seconds:
                    raise KnowledgeError('structure_build_timeout')
                batch = payload['children'][start:start+8]
                outputs = member.encode_documents([u['embedding_text'] for u in batch])
                if len(outputs) != len(batch):
                    raise KnowledgeError('structure_index_invalid')
                for unit, array in zip(batch, outputs):
                    value = np.asarray(array, dtype='<f4')
                    if (value.ndim != 2 or value.shape[1] != member.dimension
                            or not 1 <= len(value) <= VectorIndex._max_windows(member)
                            or not np.isfinite(value).all()):
                        raise KnowledgeError('structure_index_invalid')
                    for window, vec in enumerate(value):
                        if abs(float(np.linalg.norm(vec))-1) > 0.002:
                            raise KnowledgeError('structure_index_invalid')
                        blob = vec.tobytes()
                        generated.append({'unit_id': unit['id'], 'window_no': window,
                            'text_sha256': sha(packed([POLICY, unit['embedding_text']])),
                            'vector': blob, 'vector_sha256': sha(blob)})
            generated.sort(key=lambda r: (r['unit_id'], r['window_no']))
            all_generated.append((member, generated))
            results.append({'profile_id': member.profile_id, 'reused': False, 'vector_count': len(generated)})
        with self.provider.db() as db:
            db.execute('BEGIN IMMEDIATE')
            current_rows, current_payload, current_digest = self._derive(db, release_id)
            VectorIndex(self.provider)._build_rows_valid(db, current_rows)
            if current_digest != digest or time.monotonic()-started > max_seconds:
                raise KnowledgeError('structure_build_timeout')
            # 同一事务内提交全部变化；失败不覆盖旧索引。
            db.execute('INSERT OR REPLACE INTO structure_releases VALUES(?,?,?,?,?)',
                       (release_id, POLICY, manifest, digest, packed(current_payload)))
            for member, generated in all_generated:
                db.execute('DELETE FROM structure_vectors WHERE release_id=? AND policy_id=? AND profile_id=?',
                           (release_id, POLICY, member.profile_id))
                db.executemany('INSERT INTO structure_vectors VALUES(?,?,?,?,?,?,?,?)',
                    [(release_id, POLICY, member.profile_id, r['unit_id'], r['window_no'],
                      r['text_sha256'], r['vector'], r['vector_sha256']) for r in generated])
                db.execute('INSERT OR REPLACE INTO structure_vector_releases VALUES(?,?,?,?,?,?,?,?)',
                    (release_id, POLICY, member.profile_id, digest, self._vector_manifest(generated),
                     member.dimension, len(generated), now()))
        return {'ready': True, 'applicable': True, 'policy': POLICY, 'profiles': results,
                'child_count': len(payload['children']), 'parent_count': len(payload['parents']),
                'unit_manifest': digest, 'issues': payload['issues'],
                'build_seconds': round(time.monotonic()-started, 3)}

    @staticmethod
    def _lexical(query, units):
        terms = set(tokens(query))
        counts = {u['id']: Counter(tokens(u['text'])) for u in units}
        average = sum(sum(c.values()) for c in counts.values()) / max(1, len(counts))
        frequencies = Counter(term for count in counts.values() for term in terms if term in count)
        scores = {}; weights = {}
        for uid, count in counts.items():
            overlap = terms & count.keys()
            if not overlap: continue
            size = sum(count.values()); total = 0.0
            for term in overlap:
                freq = count[term]; df = frequencies[term]
                idf = math.log(1 + (len(counts)-df+0.5)/(df+0.5))
                total += idf * freq * 2.2 / (freq + 1.2*(0.25+0.75*size/max(1, average)))
            scores[uid] = -total
            weights[uid] = len(overlap)/max(1, len(terms))
        return scores, weights

    def search(self, db, release_id, query, eligible_ids, *, projected_query=None,
               translated_query=None, cross_language=False):
        meta = {'policy': POLICY, 'ready': False, 'used': False, 'reason': None,
                'parent_groups': [], 'evidence_groups': [], 'view_failures': {}, 'unit_count': 0}
        try:
            canonical_rows, payload, digest = self._load_units(db, release_id)
            parents = {u['id']: u for u in payload['parents']}
            cores = {group['group_id']: group for group in payload['cores']}
            window_rows = {}
            for row in canonical_rows:
                if representation(row)['kind'] == 'market_full_text':
                    window_rows.setdefault(row['version_id'],[]).append(row)
            windows = []
            for version_id,members in sorted(window_rows.items()):
                if not any(row['id'] in eligible_ids for row in members):
                    continue
                version = db.execute('SELECT parsed_text,parsed_sha256 FROM document_versions WHERE id=?',
                                     (version_id,)).fetchone()
                if version is None or sha(version['parsed_text'].encode()) != version['parsed_sha256']:
                    raise KnowledgeError('structure_index_invalid')
                windows.extend(_canonical_sentence_windows(version['parsed_text'],members,
                    [core for core in cores.values() if core['version_id']==version_id],eligible_ids))
            # 完整映射不可见的句组仍作为拒绝约束保留。若先删掉整组，后续
            # canonical fallback 会把其可见部分重新造为 singleton，绕过
            # 原子核心；这些 ID 不成为证据，也没有原文或向量参与不可见召回。
            blocked = [dict(group,score=0.0,matched_child_ids=[],child_rank=None,context_options=[])
                       for group in cores.values() if set(group['citation_ids']) & eligible_ids
                       and not set(group['citation_ids']) <= eligible_ids]
            units = [u for u in payload['children'] if set(u['citation_ids']) <= eligible_ids]
            meta.update(ready=True, unit_count=len(units), issues=payload['issues'],
                        evidence_groups=[*blocked,*windows],used=bool(blocked or windows),blocked_core_count=len(blocked),
                        canonical_window_policy=CANONICAL_WINDOW_POLICY,
                        canonical_window_count=len(windows),
                        complete_canonical_window_count=sum(window['complete'] for window in windows),
                        rejected_canonical_window_count=sum(not window['complete'] for window in windows))
            if not units: return meta
            features = {'original': query, 'public_concepts': projected_query, 'translated': translated_query}
            lexical = {}; weights = {}; views = {}; branches = []
            for name, value in features.items():
                if value:
                    lexical[name], weights[name] = self._lexical(value, units)
            import numpy as np
            from .retrieval_encoder import is_bundle, score_policy
            try:
                selected = VectorIndex(self.provider, self.encoder)._encoder()
                factories = selected.factories if is_bundle(selected) and cross_language else \
                    selected.factories[:1] if is_bundle(selected) else (lambda: selected,)
                for branch, factory in enumerate(factories):
                    try:
                        member = factory()
                        ids, matrix = self._load_vectors(db, release_id, member, payload, digest)
                        accepted = {u['id'] for u in units}
                        offsets = [i for i, uid in enumerate(ids) if uid in accepted]
                        minimum, distance = score_policy(member)
                        branch_features = features if branch == 0 else {'original': query}
                        failures = {}
                        for name, value in branch_features.items():
                            if not value: continue
                            try:
                                vec = np.asarray(member.encode_query(value), dtype=np.float32)
                                if (vec.shape != (member.dimension,) or not np.isfinite(vec).all()
                                        or abs(float(np.linalg.norm(vec))-1) > 0.002):
                                    raise KnowledgeError('vector_inference_failed')
                                sims = matrix[offsets] @ vec
                                threshold = max(minimum, float(sims.max())-distance)
                                scores = {}
                                for index, score in zip(offsets, sims):
                                    if float(score) >= threshold:
                                        scores[ids[index]] = max(scores.get(ids[index], -1), float(score))
                                views[str(branch)+':'+name] = scores
                            except Exception as error:
                                failures[name] = reason(error)
                        branches.append({'profile_id': member.profile_id, 'failures': failures})
                        meta['view_failures'].update({str(branch)+':'+k: v for k, v in failures.items()})
                    except Exception as error:
                        branches.append({'profile_id': None, 'reason': reason(error)})
            except Exception as error:
                branches.append({'profile_id': None, 'reason': reason(error)})
            rankings = fuse({}, {}, semantic_views=views, lexical_views=lexical,
                            lexical_view_weights=weights)
            parent_scores = {}
            core_scores = {}; matched_children = {}
            children = {u['id']: u for u in units}
            for uid, score in rankings.items():
                parent = children[uid]['parent_id']
                parent_scores[parent] = min(parent_scores.get(parent, 1.0), score)
                core_id = children[uid]['core_group_id']
                core_scores[core_id] = min(core_scores.get(core_id, 1.0), score)
                matched_children.setdefault(core_id, []).append(uid)
            child_positions = {uid: rank+1 for rank,uid in enumerate(sorted(rankings,key=lambda uid:(rankings[uid],uid)))}
            recall_positions={}
            raw_branches=[('lexical.'+name,values,False) for name,values in lexical.items()]
            raw_branches.extend(('vector.'+('primary' if name.startswith('0:') else 'secondary')+'.'+name.split(':',1)[1],values,True)
                                for name,values in views.items())
            for name,values,descending in raw_branches:
                ordered=sorted(values,key=lambda uid:((-values[uid] if descending else values[uid]),uid))
                for position,uid in enumerate(ordered,1):
                    core_id=children[uid]['core_group_id']
                    slot=recall_positions.setdefault(core_id,{})
                    slot[name]=min(slot.get(name,position),position)
            meta['evidence_groups'] = []
            for core_id, score in sorted(core_scores.items(),key=lambda item:(item[1],item[0])):
                group = dict(cores[core_id])
                group.update(score=score, matched_child_ids=matched_children[core_id],
                             child_rank=min(child_positions[uid] for uid in matched_children[core_id]),
                             recall_rank_positions=recall_positions.get(core_id,{}),
                             context_options=_context_options(group,cores,eligible_ids))
                meta['evidence_groups'].append(group)
            meta['evidence_groups'].extend(group for group in blocked if group['group_id'] not in core_scores)
            for window in windows:
                matches = [cid for cid in window['window_core_ids'] if cid in core_scores]
                matched = list(dict.fromkeys(uid for cid in matches for uid in matched_children[cid]))
                positions = {}
                for cid in matches:
                    for branch,rank in recall_positions.get(cid,{}).items():
                        positions[branch] = min(positions.get(branch,rank),rank)
                window.update(score=min((core_scores[cid] for cid in matches),default=0.0),
                              matched_child_ids=matched,
                              child_rank=min((child_positions[uid] for uid in matched),default=None),
                              recall_rank_positions=positions)
            meta['evidence_groups'].extend(windows)
            meta['parent_groups'] = [{'parent_id': pid, 'citation_ids': parents[pid]['citation_ids'],
                                     'score': score, 'continuation': parents[pid]['continuation']}
                for pid, score in sorted(parent_scores.items(), key=lambda item: (item[1], item[0]))
                if set(parents[pid]['citation_ids']) <= eligible_ids]
            meta.update(used=bool(core_scores or blocked or windows), vector_used=any(views.values()),
                        child_candidate_count=len(rankings), branches=branches,
                        complete_core_count=sum(cores[cid]['complete'] for cid in core_scores),
                        unresolved_core_count=sum(not cores[cid]['complete'] for cid in core_scores))
            if any(b.get('reason') or b.get('failures') for b in branches):
                meta['reason']='structure_partial'
            return meta
        except Exception as error:
            meta['reason'] = reason(error)
            return meta


def select_complete_context(selected, matches, parent_groups, limit):
    """保留来源排序和 facet 种子；每次加入完整 canonical 映射组。"""
    result = list(selected); skipped = 0
    by_citation = {}
    for group in parent_groups:
        for cid in group['citation_ids']:
            by_citation.setdefault(cid, group)
    for _, cid, _ in matches:
        if cid in result: continue
        group = by_citation.get(cid)
        ids = group['citation_ids'] if group else [cid]
        missing = [value for value in ids if value not in result]
        if len(result) + len(missing) <= limit:
            result.extend(missing)
        elif group:
            skipped += 1
    # facet种子仍可能缺少父段的其他映射；明确报告，而不声称完整。
    selected_ids=set(result)
    # 一个canonical chunk可能映射多个父段，不能只检查其第一个父段。
    incomplete=sum(bool(set(group['citation_ids']) & selected_ids)
                   and not set(group['citation_ids']) <= selected_ids for group in parent_groups)
    return result, {'budget_skipped_groups': skipped, 'incomplete_selected_groups': incomplete}
