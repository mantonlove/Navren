"""纯合成书目/正文语法；不读取验收题、金融原件、网络或模型。"""
import socket
import unittest
from unittest.mock import patch

from local_knowledge.reference_roles import POLICY, ROLE, detect_reference_roles


class ReferenceRoleTests(unittest.TestCase):
    def assertPointerOnly(self, pointer, before="", after=""):
        text = before + pointer + after
        result = detect_reference_roles(text)
        self.assertEqual(result, {"policy": POLICY, "spans": [
            {"role": ROLE, "start": len(before), "end": len(before) + len(pointer)},
        ]})
        span = result["spans"][0]
        self.assertEqual(text[span["start"]:span["end"]], pointer)
        self.assertEqual(text[span["end"]:], after)

    def assertUnknown(self, text):
        self.assertEqual(detect_reference_roles(text), {"policy": POLICY, "spans": []})

    def test_dated_quoted_pointer_and_real_statement_are_separate(self):
        pointer = ('For more information, see Smith, J. (2025), '
                   '“Programme design”, Working Paper Series, No. 12, '
                   'Publisher: “Institute Alpha”, 7 June 2025.')
        self.assertPointerOnly(pointer, "The programme opened in June. ",
                               " The programme requires a completed application.")

    def test_title_topic_words_are_metadata_without_becoming_facts(self):
        pointer = ('For further information, see Jones, F. (2024), '
                   '“Does Policy Beta Guarantee Returns? Restrictions and Penalties.”, '
                   'Research Bulletin, Publisher: “Institute Delta”, 14 October 2024.')
        self.assertPointerOnly(pointer, after=" No guarantee applies to the programme.")
        self.assertEqual(set(detect_reference_roles(pointer)), {"policy", "spans"})
        self.assertEqual(set(detect_reference_roles(pointer)["spans"][0]), {"role", "start", "end"})

    def test_author_initials_and_metadata_abbreviations_are_not_endpoints(self):
        pointer = ('For more information, see Alpha, S., Beta, F. and Gamma, J.-P. '
                   '(2023), "Survey methods", Discussion Paper, No. 4, Vol. 2, pp. 11–19, '
                   'Publisher: “Institute Sigma”, 2 Sep. 2023.')
        self.assertPointerOnly(pointer, after=" The survey contained 80 responses.")

    def test_ascii_single_quoted_title_and_possessive_author_name(self):
        pointer = ("For more information, see O'Neill, A. (2025), "
                   "'Survey participants' choices', Research Report, 4 May 2025.")
        self.assertPointerOnly(pointer, after=" The sample excludes withdrawn responses.")

    def test_curly_possessive_and_nested_title_quotes(self):
        pointer = ('For further information, see de Vries, A. (2024), '
                   '“The Council’s study of ‘safe’ in survey responses”, Publisher: “Institute Q”, 3 March 2024.')
        self.assertPointerOnly(pointer, after=" The word safe is not a guarantee.")

    def test_multiple_entries_joined_by_semicolon(self):
        pointer = ('For more information, see Alpha, S. (2024), "First method", '
                   'Working Paper, 1 January 2024; and Beta, F. (2025), "Second method", '
                   'Research Bulletin, 2 February 2025.')
        self.assertPointerOnly(pointer, after=" Both methods require complete records.")

    def test_multiple_complete_entries_after_period(self):
        pointer = ('For further information, see Alpha, S. (2024), “First title”, '
                   'Working Paper, 1 January. Beta, F. (2025), “Second title”, Research Bulletin, 2 February.')
        self.assertPointerOnly(pointer, after=" The programme begins on 3 March.")

    def test_two_independent_pointer_spans_preserve_between_body(self):
        first = ('For more information, see Alpha, S. (2024), “One”, Working Paper, January 2024.')
        body = " The eligibility rule remains in force. "
        second = ('For further information, see Beta, F. (2025), “Two”, '
                  'Research Bulletin, 2025-02-03.')
        text = first + body + second + " The filing deadline is unchanged."
        self.assertEqual(detect_reference_roles(text), {"policy": POLICY, "spans": [
            {"role": ROLE, "start": 0, "end": len(first)},
            {"role": ROLE, "start": len(first) + len(body),
             "end": len(first) + len(body) + len(second)},
        ]})

    def test_all_supported_date_forms_and_whitespace_offsets(self):
        for date in ("5 June 2024", "June 5, 2024", "June 5 2024", "5 June", "June 5",
                     "June 2024", "2024-06-05"):
            with self.subTest(date=date):
                pointer = ('For more information, see Alpha, S. (2024), '
                           f'“Methods”, Research Report, {date}.')
                self.assertPointerOnly(pointer, "  \n", "\nThe conclusion remains provisional.")

    def test_invalid_dates_are_unknown(self):
        for date in ("31 February 2024", "2025-02-29", "2024-13-01", "June 45, 2024"):
            with self.subTest(date=date):
                self.assertUnknown('For more information, see Alpha, S. (2024), "Methods", '
                                   f'Research Report, {date}.')

    def test_bare_year_or_missing_date_does_not_guess_end(self):
        for ending in ("Research Report, 2024.", "Research Report.", "Research Report, June.", "Research Report"):
            with self.subTest(ending=ending):
                self.assertUnknown('For more information, see Alpha, S. (2024), "Methods", ' + ending)

    def test_author_year_and_quoted_title_are_all_required(self):
        for citation in (
            'Alpha, S., "Methods", Research Report, 5 June 2024.',
            'Alpha, S. (2024), Methods, Research Report, 5 June 2024.',
            '(2024), "Methods", Research Report, 5 June 2024.',
            'The policy states (2024), "Methods", Research Report, 5 June 2024.',
        ):
            with self.subTest(citation=citation):
                self.assertUnknown("For more information, see " + citation)

    def test_plain_see_and_legal_cross_references_are_preserved(self):
        for text in (
            "See Section 12 of the Act. The exception applies to registered applicants.",
            'See Alpha, S. (2024), "Methods", Research Report, 5 June 2024.',
            "For more information on the obligation, see Article 8 of Regulation (2024).",
            "For further information on the obligation, see sections 8 and 9 of the Act.",
            "For more information, see paragraph 12. Applicants must file a report.",
        ):
            with self.subTest(text=text):
                self.assertUnknown(text)

    def test_conditional_or_embedded_pointers_are_preserved(self):
        pointer = 'for more information, see Alpha, S. (2024), "Methods", Research Report, 5 June 2024.'
        for text in (
            "If an applicant requests an exception, " + pointer,
            "Applicants may, " + pointer,
            "Only if the rule changes; " + pointer,
            'The report says "' + pointer + '" The rule remains applicable.',
            "(" + pointer + ") The rule remains applicable.",
            "The result is supported by Alpha, S. (2024), “Methods”, Research Report, 5 June 2024.",
        ):
            with self.subTest(text=text):
                self.assertUnknown(text)

    def test_factual_clause_in_publication_tail_is_not_metadata(self):
        for tail in (
            "which concludes that the programme is safe, 5 June 2024.",
            "Research Report states the guarantee applies, 5 June 2024.",
            "The Programme Is Guaranteed, 5 June 2024.",
            "Research Report, 5 June 2024 if the applicant qualifies.",
            "Research Report, provided the conditions apply, 5 June 2024.",
        ):
            with self.subTest(tail=tail):
                self.assertUnknown('For more information, see Alpha, S. (2024), "Methods", ' + tail)

    def test_conditions_inside_pointer_header_remain_unknown(self):
        for header in (
            "For more information on the method if the rule applies, see ",
            "For further information on the method, unless an exception applies, see ",
            "For more information on the study subject to approval, see ",
        ):
            with self.subTest(header=header):
                self.assertUnknown(header + 'Alpha, S. (2024), "Methods", Research Report, 5 June 2024.')

    def test_untyped_numbers_are_not_publication_metadata(self):
        for tail in (
            "Research Report, Amount 500, 5 June 2024.",
            "Research Report, 25, 5 June 2024.",
            "Research Report 20, 5 June 2024.",
        ):
            with self.subTest(tail=tail):
                self.assertUnknown('For more information, see Alpha, S. (2024), "Methods", ' + tail)

    def test_independent_audit_condition_and_statement_counterexamples(self):
        # 原始独立复查反例保留，不以改写原句来回避 v1 的误判。
        for statement in (
            "Withdrawals Require Prior Approval",
            "Investors Pay All Fees",
            "Subject To Prior Approval",
            "WITHDRAWALS REQUIRE PRIOR APPROVAL",
        ):
            with self.subTest(statement=statement):
                self.assertUnknown('For more information, see Alpha, S. (2024), "Methods", '
                                   f'Institute A, {statement}, 5 June 2024.')
                # 使用确实支持的元字段隔离附加声明的拒绝规则。
                self.assertUnknown('For more information, see Alpha, S. (2024), "Methods", '
                                   f'Research Report, {statement}, 5 June 2024.')

    def test_unknown_title_case_fields_never_borrow_metadata_role(self):
        for statement in (
            "Applicants Submit Records", "Users Pay Fees", "Requests Need Review",
            "Children Require Adult Supervision", "Devices Collect Logs", "Access Ends Tomorrow",
            "Subject To Registration", "Only With Permission", "Unless Approval Arrives",
            "Provided The Schedule Changes", "Any Other Capitalized Field",
        ):
            for variant in (statement, statement.upper(), statement.lower()):
                with self.subTest(statement=variant):
                    self.assertUnknown('For further information, see Alpha, S. (2024), '
                                       f'“Methods”, Working Paper, {variant}, 5 June 2024.')

    def test_unmarked_proper_names_are_explicitly_unknown(self):
        # v1 接受了这些合成格式；v2 不再把裸名称当成已知出版元信息。
        for field in ("Institute Alpha", "Institute A", "European Example University", "Alpha Report",
                      "University Closes", "UNIVERSITY CLOSES", "Acronym XYZ", "Research Bulletin Extra"):
            with self.subTest(field=field):
                self.assertUnknown('For more information, see Alpha, S. (2024), “Methods”, '
                                   f'{field}, 5 June 2024.')

    def test_publication_suffix_does_not_type_an_arbitrary_statement(self):
        for field in (
            "Applicants Submit Records Report", "Users Pay Fees Working Paper",
            "Subject To Registration Bulletin", "Provided Approval Publisher: “Institute A”",
        ):
            with self.subTest(field=field):
                self.assertUnknown('For more information, see Alpha, S. (2024), “Methods”, '
                                   f'{field}, 5 June 2024.')

    def test_explicitly_named_publication_fields_are_complete_and_quoted(self):
        for field in (
            'Publisher: “Institute Alpha”', 'Journal: "Example Quarterly"',
            'Series: “Example Series, Section Q”', 'Proceedings: “Example Conference”',
        ):
            with self.subTest(field=field):
                pointer = f'For more information, see Alpha, S. (2024), “Methods”, {field}, 5 June 2024.'
                self.assertPointerOnly(pointer, after=" Applicants must obtain separate approval.")
        for field in (
            "Publisher: Institute Alpha", 'Publisher: “Institute A” Subject To Approval',
            'Journal: "Example Quarterly", Users Pay Fees', 'Publisher: “Institute A',
        ):
            with self.subTest(field=field):
                self.assertUnknown(f'For more information, see Alpha, S. (2024), “Methods”, {field}, 5 June 2024.')

    def test_author_area_cannot_accept_an_arbitrary_title_case_statement(self):
        for author in (
            "Applicants Submit Records", "Users Pay Fees", "Subject To Prior Approval",
            "WITHDRAWALS REQUIRE PRIOR APPROVAL", "Institute Alpha", "John Smith", "S.",
        ):
            with self.subTest(author=author):
                self.assertUnknown(f'For more information, see {author} (2024), “Methods”, '
                                   'Research Report, 5 June 2024.')

    def test_clear_author_and_title_may_end_in_a_date_without_optional_fields(self):
        pointer = 'For more information, see Alpha, S. (2024), “Methods”, 5 June 2024.'
        self.assertPointerOnly(pointer, after=" Applications require prior approval.")

    def test_access_dates_are_explicit_final_date_labels(self):
        for label in ("accessed on", "retrieved on", "accessed", "retrieved"):
            with self.subTest(label=label):
                pointer = (f'For more information, see Alpha, S. (2024), “Methods”, '
                           f'Research Report, {label} 5 June 2024.')
                self.assertPointerOnly(pointer, after=" Users must obtain permission.")
        self.assertUnknown('For more information, see Alpha, S. (2024), “Methods”, '
                           'accessed on, Research Report, 5 June 2024.')

    def test_independent_audit_header_fact_and_obligation_counterexamples(self):
        # 独立复查原句逐字保留：自由主题可能承载义务、限制或事实。
        for text in (
            'For more information on the fact that applicants must submit records, see Alpha, S. '
            '(2024), "Methods", Research Report, 5 June 2024.',
            'For more information on withdrawals requiring prior approval, see Alpha, S. '
            '(2024), "Methods", Research Report, 5 June 2024.',
        ):
            with self.subTest(text=text):
                self.assertUnknown(text)

    def test_every_free_topic_is_unknown_including_plain_former_positive_topics(self):
        # 不靠谓词或主题黑名单区别安全 topic：v3 一律保留 on/about 原文。
        for qualifier in ("on", "about"):
            for topic in (
                "the programme", "the method", "governance", "the survey", "the Council’s study",
                "the fact that devices collect logs", "users paying every charge", "students requiring permission",
                "“Programme design”", "Applicant Obligations", "ANY OTHER TOPIC",
            ):
                with self.subTest(qualifier=qualifier, topic=topic):
                    self.assertUnknown(f'For further information {qualifier} {topic}, see Alpha, S. '
                                       '(2024), “Methods”, Research Report, 5 June 2024.')

    def test_bare_legal_or_name_word_is_not_a_verified_personal_author(self):
        for author in ("Regulation", "Article", "Section", "Act", "Alpha", "Smith", "Institute"):
            with self.subTest(author=author):
                self.assertUnknown(f'For more information, see {author} (2024), "Section Eight", '
                                   'Research Report, 5 June 2024.')

    def test_explicit_initials_are_required_for_every_personal_author(self):
        for author in ("Alpha, S.", "S. Alpha", "de Vries, J.-P.", "Alpha, S. and Beta, F.",
                       "Alpha, S., Beta, F. and Gamma, J.", "Alpha, S. et al."):
            with self.subTest(author=author):
                pointer = f'For more information, see {author} (2024), “Methods”, Research Report, 5 June 2024.'
                self.assertPointerOnly(pointer, after=" Users require prior consent.")
        for author in ("Alpha, S. and Beta", "Alpha and Beta, F.", "de Vries", "Alpha et al."):
            with self.subTest(author=author):
                self.assertUnknown(f'For more information, see {author} (2024), “Methods”, '
                                   'Research Report, 5 June 2024.')

    def test_date_in_a_later_fact_sentence_is_never_used_as_reference_end(self):
        self.assertUnknown('For more information, see Alpha, S. (2024), "Methods", Research Report. '
                           'The programme will begin on 5 June 2024.')

    def test_unbalanced_titles_or_context_are_unknown(self):
        for text in (
            'For more information, see Alpha, S. (2024), “Methods", Research Report, 5 June 2024.',
            'For more information, see Alpha, S. (2024), "Methods, Research Report, 5 June 2024.',
            'The report says “For more information, see Alpha, S. (2024), "Methods", '
            'Research Report, 5 June 2024.',
            'For more information, see Alpha, S. (2024), "Methods", Research Report (Volume 1, 5 June 2024.',
        ):
            with self.subTest(text=text):
                self.assertUnknown(text)

    def test_unclosed_semicolon_list_is_unknown_instead_of_partial_removal(self):
        self.assertUnknown('For more information, see Alpha, S. (2024), "One", Working Paper, '
                           '5 June 2024; Beta, F. (2025), "Two", Research Bulletin. '
                           'The programme starts on 6 June 2025.')

    def test_a_complete_entry_keeps_ambiguous_following_body(self):
        pointer = ('For more information, see Alpha, S. (2024), "One", Research Report, 5 June 2024.')
        self.assertPointerOnly(pointer, after=' Beta, F. (2025) found that the programme has no guarantee.')

    def test_does_not_classify_chinese_or_generic_navigation(self):
        for text in (
            "更多信息请参见甲（2024），《方法》，某机构，2024年6月5日。项目要求实名登记。",
            "For more information, visit the website. The guarantee does not apply.",
            "For more information see Alpha, S. (2024), “Methods”, Research Report, 5 June 2024.",
            "For information on the method, see Alpha, S. (2024), “Methods”, Research Report, 5 June 2024.",
        ):
            with self.subTest(text=text):
                self.assertUnknown(text)

    def test_pure_local_and_restricted_inputs(self):
        pointer = 'For more information, see Alpha, S. (2024), “Methods”, Research Report, 5 June 2024.'
        with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")):
            self.assertPointerOnly(pointer)
        for text in (None, 4, [], {}, "", "x" * 2_000_001):
            with self.subTest(kind=type(text).__name__):
                self.assertUnknown(text)


if __name__ == "__main__":
    unittest.main()
