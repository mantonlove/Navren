"""通用合成财务/公共政策问句；不使用原验收题、私人信息或模型。"""
import unittest
from unittest.mock import patch
import socket

from local_knowledge.query_segments import POLICY, REASONS, build_query_segments


class QuerySegmentsTests(unittest.TestCase):
    def assertFallback(self, query):
        result = build_query_segments(query)
        self.assertEqual(result["queries"], [query])
        self.assertFalse(result["used"])
        self.assertEqual(result["count"], 1)
        self.assertIn(result["reason"], REASONS)

    def test_two_explicit_questions(self):
        query = "产品甲的募集方式是什么？政策乙何时生效？"
        result = build_query_segments(query)
        self.assertEqual(result["queries"], ["产品甲的募集方式是什么？", "政策乙何时生效？"])
        self.assertTrue(result["used"])
        self.assertEqual(result["policy"], POLICY)
        self.assertEqual(result["count"], 2)

    def test_comma_and_semicolon_questions_have_their_own_subjects(self):
        for separator in ("，", ",", "；", ";"):
            with self.subTest(separator=separator):
                query = "产品甲的风险等级是多少" + separator + "政策乙的适用范围是什么"
                self.assertEqual(build_query_segments(query)["queries"],
                                 ["产品甲的风险等级是多少", "政策乙的适用范围是什么"])

    def test_explicit_task_colon_preserves_every_original_question(self):
        query = "请回答以下问题：产品甲的费用是多少；政策乙何时实施；银行丙的存款保险范围是什么"
        result = build_query_segments(query)
        self.assertEqual(result["queries"], [
            "请回答以下问题：产品甲的费用是多少",
            "请回答以下问题：政策乙何时实施",
            "请回答以下问题：银行丙的存款保险范围是什么",
        ])
        self.assertEqual(result["reason"], "shared_prefix_questions")

    def test_shared_original_time_and_source_prefix_is_not_rewritten(self):
        prefix = "截至2026年10月1日，依据同一版本官方资料，请分别回答："
        query = prefix + "产品甲的风险等级是什么？政策乙的实施日期是什么？"
        result = build_query_segments(query)
        self.assertEqual(result["queries"], [prefix + "产品甲的风险等级是什么？",
                                             prefix + "政策乙的实施日期是什么？"])

    def test_temporal_prefix_without_colon_is_copied(self):
        query = "截至2026年10月1日，产品甲的风险等级是什么，政策乙何时生效"
        self.assertEqual(build_query_segments(query)["queries"], [
            "截至2026年10月1日，产品甲的风险等级是什么",
            "截至2026年10月1日，政策乙何时生效",
        ])

    def test_scope_prefix_without_colon_is_copied(self):
        query = "根据公开合同，产品甲的收费方式是什么；条例乙的适用范围是什么"
        self.assertEqual(build_query_segments(query)["queries"], [
            "根据公开合同，产品甲的收费方式是什么", "根据公开合同，条例乙的适用范围是什么",
        ])

    def test_multiple_necessary_qualifiers_without_colon_are_all_copied(self):
        prefix = "截至2026年10月1日，在同一渠道，依据官方合同，"
        query = prefix + "产品甲的风险等级是什么？政策乙何时生效？"
        self.assertEqual(build_query_segments(query)["queries"], [
            prefix + "产品甲的风险等级是什么？", prefix + "政策乙何时生效？",
        ])

    def test_bare_common_date_prefix_is_copied_verbatim(self):
        for prefix in ("2026年10月1日，", "2026-10-01，", "2026年1月1日至2026年12月31日，"):
            with self.subTest(prefix=prefix):
                query = prefix + "产品甲的风险等级是什么？政策乙何时生效？"
                self.assertEqual(build_query_segments(query)["queries"], [
                    prefix + "产品甲的风险等级是什么？", prefix + "政策乙何时生效？",
                ])

    def test_independent_question_particles_keep_their_explicit_objects(self):
        query = "产品甲可以提前赎回吗，政策乙会在明年实施吗？"
        self.assertEqual(build_query_segments(query)["queries"], [
            "产品甲可以提前赎回吗", "政策乙会在明年实施吗？",
        ])

    def test_explicit_common_subject_header_keeps_its_reference(self):
        prefix = "关于产品甲，请回答："
        query = prefix + "其风险等级是什么？谁是发行机构？"
        result = build_query_segments(query)
        self.assertEqual(result["queries"], [prefix + "其风险等级是什么？", prefix + "谁是发行机构？"])

    def test_numbered_original_questions_remain_original_text(self):
        query = "以下问题：1. 产品甲的费用是多少；2. 政策乙何时生效"
        self.assertEqual(build_query_segments(query)["queries"], [
            "以下问题：1. 产品甲的费用是多少", "以下问题：2. 政策乙何时生效",
        ])

    def test_two_independent_imperative_requests(self):
        query = "请说明产品甲的费用，列出条例乙的适用对象"
        self.assertEqual(build_query_segments(query)["queries"],
                         ["请说明产品甲的费用", "列出条例乙的适用对象"])

    def test_generic_cross_domain_questions(self):
        pairs = [
            ("城市甲的预算如何安排", "学校乙的入学规则是什么"),
            ("CPI为何上涨", "GDP何时公布"),
            ("新条例何时实施", "银行丙的适用规则是什么"),
            ("项目甲的投入是多少", "部门乙的职责是什么"),
        ]
        for first, second in pairs:
            with self.subTest(first=first):
                self.assertEqual(build_query_segments(first + "；" + second)["queries"], [first, second])

    def test_parenthetical_commas_and_question_marks_are_not_boundaries(self):
        first = "产品甲（包括份额A，份额B）的风险等级是什么"
        second = "政策乙的实施时间是什么"
        self.assertEqual(build_query_segments(first + "；" + second)["queries"], [first, second])
        first = "政策甲（附件标题为“何时生效？”）的适用范围是什么"
        self.assertEqual(build_query_segments(first + "；" + second)["queries"], [first, second])

    def test_numeric_commas_time_colon_and_ranges_stay_inside_questions(self):
        first = "产品甲的1,000元申购门槛是什么"
        second = "政策乙的08:30发布时间是什么"
        self.assertEqual(build_query_segments(first + "；" + second)["queries"], [first, second])
        self.assertFallback("从2026年1月1日，到2026年12月31日，产品甲的利率是多少？")
        self.assertFallback("产品甲的1,000元至2,000元申购范围是什么？")

    def test_urls_with_query_punctuation_are_not_split_points(self):
        query = "政策甲的链接https://example.test/document?a=1,b=2是什么意思？"
        self.assertFallback(query)

    def test_english_possessive_and_explicit_topics(self):
        query = "What is Product Alpha's fee; when does Policy Beta apply?"
        result = build_query_segments(query)
        self.assertTrue(result["used"])
        self.assertEqual(result["queries"], ["What is Product Alpha's fee", "when does Policy Beta apply?"])

    def test_single_compound_field_question_is_not_split(self):
        for query in (
            "产品甲的风险、费用和流动性分别是什么？",
            "产品甲的风险等级，费用和期限分别是什么？",
            "请介绍产品甲的费用、期限，以及申购条件。",
            "产品甲的风险是多少，费用是多少？",
            "产品甲的风险是什么，收益呢？",
            "产品甲的风险等级是什么，发行机构是谁？",
            "产品甲的风险等级是什么，谁是发行机构？",
        ):
            with self.subTest(query=query):
                self.assertFallback(query)

    def test_conditions_and_exclusions_are_not_split_or_lost(self):
        for query in (
            "如果产品甲不保本，银行乙是否承担损失？",
            "只有符合条例甲的条件，产品乙是否允许申购？",
            "产品甲的风险是多少，但不要考虑短期表现，产品乙费用是多少？",
            "产品甲是否适合，只要政策乙仍生效？",
            "若产品甲出现亏损，银行乙如何处理？",
            "产品甲的费用是多少；政策乙是否适用，除非另有通知？",
            "If Product Alpha is withdrawn, can Policy Beta apply?",
        ):
            with self.subTest(query=query):
                self.assertFallback(query)

    def test_comparison_operands_are_not_separated(self):
        for query in (
            "产品甲，产品乙哪个风险更低？",
            "请比较产品甲，产品乙的费用和期限。",
            "产品甲风险是什么，与产品乙相比是否更低？",
            "产品甲与产品乙，政策丙与政策丁分别有什么区别？",
            "产品甲的风险和费用，或产品乙的收益哪个更稳定？",
            "请比较产品甲的费用，产品乙是否更低？",
            "产品甲的风险等级是什么，产品乙是否更低？",
            "产品甲的收益率是多少，产品乙是否优于它？",
            "What is Product Alpha's cost; is Product Beta cheaper?",
        ):
            with self.subTest(query=query):
                self.assertFallback(query)

    def test_generic_answer_requirements_are_not_independent_retrieval_tasks(self):
        for suffix in ("给出官方依据", "给出可靠的依据", "列出所有来源链接", "说明回答格式"):
            with self.subTest(suffix=suffix):
                self.assertFallback("产品甲的风险等级是什么？政策乙何时生效？" + suffix)

    def test_independent_complete_comparison_keeps_both_operands(self):
        first = "请比较产品甲与产品乙的风险"
        second = "政策丙何时实施"
        self.assertEqual(build_query_segments(first + "；" + second)["queries"], [first, second])

    def test_implicit_previous_subject_never_becomes_an_independent_view(self):
        for query in (
            "产品甲的风险是什么？它的费用是多少？",
            "产品甲的费用是多少；该产品是否在售？",
            "产品甲的期限是什么，前者的风险是多少？",
            "截至2026年10月1日，产品甲费用是多少？其风险等级是什么？",
            "请回答以下问题：产品甲费用是多少；其发行机构是谁？",
        ):
            with self.subTest(query=query):
                self.assertFallback(query)

    def test_application_questions_with_omitted_second_subject_keep_whole_query(self):
        for subject in ("这个项目", "项目甲", "学校乙", "部门丙"):
            for second in ("最终选择多少家？", "最后录取多少家？", "随后批准多少家？"):
                with self.subTest(subject=subject, second=second):
                    self.assertFallback(subject + "收到近80份申请来自哪些群体，" + second)

    def test_both_explicit_project_subjects_can_be_split(self):
        first = "项目甲收到近80份申请来自哪些群体"
        second = "项目甲最终选择多少家？"
        self.assertEqual(build_query_segments(first + "，" + second)["queries"], [first, second])

    def test_shared_original_project_header_supplies_the_omitted_subject(self):
        prefix = "关于项目甲，请回答："
        first = "收到近80份申请来自哪些群体"
        second = "最终选择多少家？"
        self.assertEqual(build_query_segments(prefix + first + "，" + second)["queries"],
                         [prefix + first, prefix + second])

    def test_temporal_header_alone_cannot_supply_the_project_subject(self):
        self.assertFallback("截至2026年10月1日，项目甲收到哪些申请，最终选择多少家？")
        self.assertFallback("以下问题：项目甲收到哪些申请；最终选择多少家？")

    def test_long_actions_are_not_accepted_as_noun_subjects(self):
        for second in ("最后决定多少家？", "完成审核需要多久？", "公布结果需要多久？",
                       "选择学校需要多久？", "批准的企业有多少家？"):
            with self.subTest(second=second):
                self.assertFallback("项目甲收到了哪些申请，" + second)

    def test_relation_objects_do_not_lose_their_first_question_antecedent(self):
        for second in ("选择的企业有多少家？", "接受资助的学校有哪些？",
                       "负责执行的企业有多少家？", "被选中的企业需要哪些材料？",
                       "参与的学校有哪些要求？", "列在上述名单的企业有哪些？"):
            with self.subTest(second=second):
                self.assertFallback("项目甲招募哪些机构，" + second)

    def test_unknown_relation_verbs_do_not_gain_subjects_from_their_objects(self):
        for relation in ("扶持", "挑选", "推荐", "评审", "关注", "援助", "联系", "赞助"):
            with self.subTest(relation=relation):
                self.assertFallback("项目甲招募哪些机构，" + relation + "的企业有多少家？")
        self.assertFallback("项目甲招募哪些机构，帮助学校乙需要多少资金？")

    def test_linked_inference_question_is_not_an_independent_view(self):
        for second in ("是否意味着资金需求增加？", "是否意味着政策乙支持学校丙？",
                       "是否表明学校乙得到支持？", "能否据此推断城市丙会增加补贴？",
                       "是否能够说明学校乙得到支持？", "这是不是意味着政策乙会增加支持？",
                       "因此学校乙需要多少材料？", "其中学校乙有多少家？"):
            with self.subTest(second=second):
                self.assertFallback("项目甲招募哪些机构，" + second)
        self.assertFallback("关于项目甲，请回答：收到哪些申请？是否意味着政策乙预算增加？")
        self.assertFallback("关于项目甲，请回答：收到哪些申请？是否能够说明政策乙预算增加？")

    def test_preposed_conditions_never_apply_only_to_the_first_split_view(self):
        for prefix in ("只有资助条件得到满足，", "满足资助条件以后，", "符合报名要求以后，",
                       "当学校乙完成申请后，", "仅在学校乙完成申请以后，",
                       "在预算充足的前提下，", "Once schools apply, "):
            with self.subTest(prefix=prefix):
                self.assertFallback(prefix + "项目甲招募哪些机构？政策乙何时生效？")

    def test_explicit_relation_subject_can_remain_inside_its_complete_clause(self):
        first = "项目甲的申请条件是什么"
        second = "项目甲资助的学校需要哪些材料？"
        self.assertEqual(build_query_segments(first + "；" + second)["queries"], [first, second])

    def test_four_questions_do_not_silently_drop_the_last_task(self):
        query = "产品甲费用是多少；政策乙何时生效；银行丙的职责是什么；城市丁的预算是多少"
        self.assertFallback(query)
        self.assertEqual(build_query_segments(query)["reason"], "segment_limit_exceeded")

    def test_unbalanced_quotes_or_brackets_fail_closed(self):
        for query in (
            "产品甲（份额A的风险是什么；政策乙何时生效",
            "产品甲“风险是什么；政策乙何时生效",
            "产品甲的风险是什么）；政策乙何时生效",
        ):
            with self.subTest(query=query):
                self.assertFallback(query)
                self.assertEqual(build_query_segments(query)["reason"], "unbalanced_delimiters")

    def test_original_string_and_diagnostic_contract_are_preserved(self):
        query = "  产品甲的费用是多少； 政策乙何时生效  "
        original = query
        result = build_query_segments(query)
        self.assertEqual(query, original)
        self.assertEqual(set(result), {"queries", "policy", "used", "count", "reason"})
        self.assertIn(result["reason"], REASONS)
        self.assertNotIn(query, repr({key: value for key, value in result.items() if key != "queries"}))
        self.assertEqual(build_query_segments(query), result)

    def test_no_network_or_model_is_needed(self):
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network_forbidden")):
            result = build_query_segments("产品甲的费用是多少；条例乙何时实施")
        self.assertTrue(result["used"])

    def test_invalid_types_use_fixed_local_error_and_long_query_keeps_original(self):
        for value in (None, 42, ["query"]):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "^invalid_query_segments_input$"):
                    build_query_segments(value)
        query = "产品甲的费用是多少；政策乙何时生效" * 150
        self.assertFallback(query)
        self.assertEqual(build_query_segments(query)["reason"], "input_too_long")


if __name__ == "__main__":
    unittest.main()
