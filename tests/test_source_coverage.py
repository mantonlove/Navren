"""国别只影响已准入公共窗口软召回，不能推断事实或机构角色。"""
from copy import deepcopy
import unittest
from local_knowledge.source_coverage import apply_source_coverage


def row(cid,version='v-us',**extra):
    return {'id':cid,'version_id':version,'publisher':'Federal Reserve',
            'source_url':'https://www.federalreserve.gov/synthetic',
            'visibility':'public','data_origin':'real','status':'verified',
            'enabled':1,'paused':0,'read_allowed':1,'outbound_allowed':1,**extra}


def group(cid,rank=1,**extra):
    return {'group_id':'cw-'+cid,'kind':'canonical_sentence_window','complete':True,
            'canonical_anchor_id':cid,'citation_ids':[cid],'canonical_anchor_rank':rank,**extra}


class SourceCoverageTests(unittest.TestCase):
    def test_explicit_country_adds_only_soft_rank_without_changing_sources(self):
        rows=[row('a')];groups=[group('a',130)];before=deepcopy((rows,groups))
        output,meta=apply_source_coverage('美国2029年公开政策资料有哪些？',groups,rows)
        self.assertEqual(output[0]['source_scope_rank'],1)
        self.assertTrue(meta['used']);self.assertEqual(meta['authority'],'candidate_coverage_only')
        self.assertEqual((rows,groups),before)
        self.assertNotIn('institution_ids',meta);self.assertNotIn('2029',str(meta))

    def test_country_does_not_override_any_existing_admission(self):
        for change in ({'visibility':'private'},{'data_origin':'synthetic'},{'status':'withdrawn'},
                       {'paused':1},{'enabled':0},{'read_allowed':0},{'outbound_allowed':0},
                       {'source_url':'https://federalreserve.gov.evil.test/a'},
                       {'source_url':'https://user@federalreserve.gov/a'},
                       {'publisher':'Federal Reserve and synthetic bank'}):
            with self.subTest(change=change):
                output,meta=apply_source_coverage('美国资料', [group('a')],[row('a',**change)])
                self.assertNotIn('source_scope_rank',output[0]);self.assertFalse(meta['used'])

    def test_mixed_version_incomplete_hidden_and_partial_map_never_get_rank(self):
        rows=[row('a'),row('b','v-other'),row('private',visibility='private')]
        for change in ({'complete':False},{'rejection':'not_eligible'},
                       {'citation_ids':['a','b']},{'citation_ids':['a','private']},
                       {'kind':'canonical_singleton'},{'canonical_anchor_rank':None}):
            with self.subTest(change=change):
                output,meta=apply_source_coverage('美国资料',[group('a',**change)],rows)
                self.assertFalse(meta['used']);self.assertNotIn('source_scope_rank',output[0])

    def test_scope_is_recomputed_for_each_query_and_original_remains_intact(self):
        old=group('a',source_scope_rank=1)
        output,meta=apply_source_coverage('未指定国别的公共资料',[old],[row('a')])
        self.assertNotIn('source_scope_rank',output[0]);self.assertFalse(meta['used'])
        self.assertEqual(old['source_scope_rank'],1)

    def test_distinct_documents_precede_many_windows_from_one_document(self):
        rows=[row('a1'),row('a2'),row('b','v-next')]
        groups=[group('a1',1),group('a2',2),group('b',90)]
        output,meta=apply_source_coverage('美国资料',groups,rows)
        rank={item['group_id']:item['source_scope_rank'] for item in output}
        self.assertLess(rank['cw-b'],rank['cw-a2']);self.assertEqual(meta['window_count'],3)


if __name__=='__main__':unittest.main()
