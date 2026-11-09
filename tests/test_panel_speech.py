"""Offline format tests; these do not validate ASR, TTS, or business results."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from conversation_panels import panel_plain_text


def encoded(value, **kwargs):
    return json.dumps(value, ensure_ascii=False, **kwargs)


def panel(*blocks, **extra):
    return {"title": "资料核对", "blocks": list(blocks), **extra}


def sample_blocks():
    return [
        {"type": "text", "title": "适用条件", "text": "仅在日期和退改条件核实后判断；不构成承诺。"},
        {"type": "metrics", "title": "已知数据", "items": [
            {"label": "人数", "value": "0", "unit": "人", "note": "尚未确认同行人。"},
            {"label": "范围", "value": "5至8", "unit": "天", "note": "仅为候选，不是已安排。"},
        ]},
        {"type": "timeline", "title": "时间条件", "items": [
            {"at": "2026-10-26 18:00", "title": "核对资料", "detail": "等待原始依据；具体时间待确认。"},
        ]},
        {"type": "checklist", "title": "待核事项", "items": [
            {"text": "确认退改限制", "detail": "不可据此推断已办理。", "due": "周五前，待确认"},
            {"text": "保留原始记录"},
        ]},
        {"type": "progress", "title": "所报比例", "items": [
            {"label": "资料覆盖", "value": 0, "note": "比例由正文提供；不代算或推断完成状态。"},
            {"label": "上限", "value": 100},
        ]},
        {"type": "bars", "title": "示例数量", "unit": "条", "items": [
            {"label": "甲", "value": 0}, {"label": "乙", "value": 1000000000},
        ]},
        {"type": "quiz", "title": "概念核对", "items": [
            {"question": "这些记录是否已核实？", "answer": "仅为示例，仍需核实依据。"},
        ]},
    ]


class PanelSpeechTests(unittest.TestCase):
    def test_minimum_and_field_order(self):
        value = {"blocks": [{"text": "原文", "type": "text"}], "title": "标题"}
        self.assertEqual(panel_plain_text(encoded(value)), "标题\n原文")

    def test_exact_canonical_reading_order(self):
        blocks = sample_blocks()
        first = panel(*blocks[:3], summary="以下仅为条件化整理。")
        self.assertEqual(panel_plain_text(encoded(first)), "\n".join([
            "资料核对", "以下仅为条件化整理。", "适用条件",
            "仅在日期和退改条件核实后判断；不构成承诺。", "已知数据",
            "人数：0 人", "尚未确认同行人。", "范围：5至8 天", "仅为候选，不是已安排。",
            "时间条件", "2026-10-26 18:00：核对资料", "等待原始依据；具体时间待确认。",
        ]))
        second = panel(*blocks[3:6])
        self.assertEqual(panel_plain_text(encoded(second)), "\n".join([
            "资料核对", "待核事项", "确认退改限制", "不可据此推断已办理。", "截止：周五前，待确认",
            "保留原始记录", "所报比例", "资料覆盖：0%", "比例由正文提供；不代算或推断完成状态。",
            "上限：100%", "示例数量", "甲：0 条", "乙：1000000000 条",
        ]))
        self.assertEqual(panel_plain_text(encoded(panel(blocks[6]))), "资料核对\n概念核对\n问：这些记录是否已核实？\n答：仅为示例，仍需核实依据。")

    def test_all_text_values_and_conditions_are_retained(self):
        for block in sample_blocks():
            value = panel(block, summary="证据缺失时不得推断。")
            result = panel_plain_text(encoded(value))
            self.assertIsNotNone(result)
            self.assertIn(value["summary"], result)
            def check_strings(node):
                if isinstance(node, dict):
                    for key, child in node.items():
                        if key != "type":
                            check_strings(child)
                elif isinstance(node, list):
                    for child in node:
                        check_strings(child)
                elif isinstance(node, str):
                    self.assertIn(node, result)
            check_strings(value)
            self.assertNotIn('"blocks"', result)
            self.assertNotIn('"items"', result)

    def test_number_zero_endpoints_and_javascript_spelling(self):
        for number, expected in [(0, "0"), (-0.0, "0"), (1.0, "1"), (1000000000.0, "1000000000"),
                                 (0.000001, "0.000001"), (0.0000001, "1e-7"),
                                 (0.000000123, "1.23e-7"), (0.30000000000000004, "0.30000000000000004"),
                                 (5e-324, "5e-324")]:
            with self.subTest(number=number):
                value = panel({"type": "bars", "items": [{"label": "值", "value": number}]})
                self.assertEqual(panel_plain_text(encoded(value)), "资料核对\n值：" + expected)

    def test_invalid_numeric_types_constants_and_ranges(self):
        for kind, invalid_values in [
            ("progress", [True, False, "0", None, [], {}, -0.01, 100.00001, float("nan"), float("inf"), -float("inf")]),
            ("bars", [True, False, "0", None, [], {}, -1, 1000000000.1, float("nan"), float("inf")]),
        ]:
            for value in invalid_values:
                with self.subTest(kind=kind, value=value):
                    source = encoded(panel({"type": kind, "items": [{"label": "值", "value": value}]}))
                    self.assertIsNone(panel_plain_text(source))
        self.assertIsNone(panel_plain_text('{"title":"题","blocks":[{"type":"bars","items":[{"label":"值","value":1e999}]}]}'))

    def test_metric_value_is_a_string_without_numeric_conversion(self):
        for value in ["0", "0001.00", "1e3", "未核实", "约5，条件未确认"]:
            result = panel_plain_text(encoded(panel({"type": "metrics", "items": [{"label": "值", "value": value}]})))
            self.assertEqual(result, "资料核对\n值：" + value)
        for value in [0, 1.2, False, None]:
            self.assertIsNone(panel_plain_text(encoded(panel({"type": "metrics", "items": [{"label": "值", "value": value}]}))))

    def test_numeric_tokens_cannot_round_across_limits_or_underflow(self):
        for kind, token in [("progress", "100.0000000000000000001"),
                            ("bars", "1000000000.00000000001"),
                            ("progress", "1e-400"), ("bars", "-1e-400")]:
            source = '{"title":"题","blocks":[{"type":"' + kind + '","items":[{"label":"值","value":' + token + '}]}]}'
            self.assertIsNone(panel_plain_text(source))
        for token in ["-0", "-0.0", "0e-400", "0.0e400"]:
            source = '{"title":"题","blocks":[{"type":"bars","items":[{"label":"值","value":' + token + '}]}]}'
            self.assertEqual(panel_plain_text(source), "题\n值：0")

    def test_trim_matches_javascript_and_does_not_mutate_source(self):
        value = panel({"type": "text", "title": "\ufeff 小标题\u3000", "text": "\u00a0 正文\u2029"}, summary=" 说明 ")
        value["title"] = "\ufeff 标题\u2007"
        before = copy.deepcopy(value)
        self.assertEqual(panel_plain_text(encoded(value)), "标题\n说明\n小标题\n正文")
        self.assertEqual(value, before)

    def test_optional_empty_strings_are_allowed_and_not_announced(self):
        for block in [
            {"type": "text", "title": " ", "text": "原文"},
            {"type": "metrics", "title": "", "items": [{"label": "甲", "value": "0", "unit": " ", "note": ""}]},
            {"type": "timeline", "items": [{"at": "某日", "title": "事件", "detail": ""}]},
            {"type": "checklist", "items": [{"text": "事项", "detail": "", "due": " "}]},
            {"type": "progress", "items": [{"label": "甲", "value": 0, "note": ""}]},
            {"type": "bars", "unit": "", "items": [{"label": "甲", "value": 0}]},
        ]:
            with self.subTest(block=block):
                result = panel_plain_text(encoded(panel(block, summary=" ")))
                self.assertIsNotNone(result)
                self.assertNotIn("\n\n", result)
                self.assertNotIn("截止：", result)
                self.assertFalse(result.endswith(" "))

    def test_required_strings_are_nonempty_after_trim(self):
        fixtures = [
            panel({"type": "text", "text": " "}),
            panel({"type": "metrics", "items": [{"label": " ", "value": "0"}]}),
            panel({"type": "metrics", "items": [{"label": "甲", "value": " "}]}),
            panel({"type": "timeline", "items": [{"at": " ", "title": "事件"}]}),
            panel({"type": "timeline", "items": [{"at": "某日", "title": " "}]}),
            panel({"type": "checklist", "items": [{"text": " "}]}),
            panel({"type": "progress", "items": [{"label": " ", "value": 0}]}),
            panel({"type": "bars", "items": [{"label": " ", "value": 0}]}),
            panel({"type": "quiz", "items": [{"question": " ", "answer": "答"}]}),
            panel({"type": "quiz", "items": [{"question": "问", "answer": " "}]}),
            {"title": " ", "blocks": [{"type": "text", "text": "原文"}]},
        ]
        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                self.assertIsNone(panel_plain_text(encoded(fixture)))

    def test_string_limits_count_unicode_code_points(self):
        fields = [
            ({"type": "text", "text": "正文"}, "title", 40),
            ({"type": "text", "text": "正文"}, "text", 600),
            ({"type": "metrics", "items": [{"label": "甲", "value": "值", "unit": "条", "note": "说明"}]}, "items.0.label", 40),
            ({"type": "metrics", "items": [{"label": "甲", "value": "值"}]}, "items.0.value", 80),
            ({"type": "metrics", "items": [{"label": "甲", "value": "值"}]}, "items.0.unit", 12),
            ({"type": "metrics", "items": [{"label": "甲", "value": "值"}]}, "items.0.note", 160),
            ({"type": "timeline", "items": [{"at": "某日", "title": "事件"}]}, "items.0.at", 40),
            ({"type": "timeline", "items": [{"at": "某日", "title": "事件"}]}, "items.0.title", 80),
            ({"type": "timeline", "items": [{"at": "某日", "title": "事件"}]}, "items.0.detail", 160),
            ({"type": "checklist", "items": [{"text": "事项"}]}, "items.0.text", 120),
            ({"type": "checklist", "items": [{"text": "事项"}]}, "items.0.detail", 160),
            ({"type": "checklist", "items": [{"text": "事项"}]}, "items.0.due", 40),
            ({"type": "progress", "items": [{"label": "甲", "value": 0}]}, "items.0.label", 40),
            ({"type": "progress", "items": [{"label": "甲", "value": 0}]}, "items.0.note", 160),
            ({"type": "bars", "items": [{"label": "甲", "value": 0}]}, "items.0.label", 40),
            ({"type": "bars", "items": [{"label": "甲", "value": 0}]}, "unit", 12),
            ({"type": "quiz", "items": [{"question": "问", "answer": "答"}]}, "items.0.question", 120),
            ({"type": "quiz", "items": [{"question": "问", "answer": "答"}]}, "items.0.answer", 300),
        ]
        for block, field, maximum in fields:
            for count, accepted in [(maximum, True), (maximum + 1, False)]:
                with self.subTest(field=field, maximum=maximum, count=count):
                    value = copy.deepcopy(block)
                    target = value
                    parts = field.split(".")
                    for part in parts[:-1]:
                        target = target[int(part)] if isinstance(target, list) else target[part]
                    target[parts[-1]] = "😀" * count
                    self.assertEqual(panel_plain_text(encoded(panel(value))) is not None, accepted)
        for field, maximum in [("title", 40), ("summary", 120)]:
            for count in [maximum, maximum + 1]:
                value = panel({"type": "text", "text": "正文"})
                value[field] = "😀" * count
                self.assertEqual(panel_plain_text(encoded(value)) is not None, count == maximum)

    def test_raw_source_length_boundary_includes_formatting(self):
        source = encoded(panel({"type": "text", "text": "正文"}))
        self.assertIsNotNone(panel_plain_text(source + " " * (6000 - len(source))))
        self.assertIsNone(panel_plain_text(source + " " * (6001 - len(source))))
        self.assertIsNotNone(panel_plain_text(encoded(panel({"type": "text", "text": "正文"}), indent=2)))

    def test_block_and_item_counts(self):
        text = {"type": "text", "text": "正文"}
        for count in [0, 1, 3, 4]:
            self.assertEqual(panel_plain_text(encoded(panel(*([text] * count)))) is not None, 1 <= count <= 3)
        templates = [
            ("metrics", {"label": "甲", "value": "0"}, 3),
            ("timeline", {"at": "某日", "title": "事件"}, 6),
            ("checklist", {"text": "事项"}, 6),
            ("progress", {"label": "甲", "value": 0}, 6),
            ("bars", {"label": "甲", "value": 0}, 6),
            ("quiz", {"question": "问", "answer": "答"}, 5),
        ]
        for kind, item, maximum in templates:
            for count in [0, 1, maximum, maximum + 1]:
                with self.subTest(kind=kind, count=count):
                    value = panel({"type": kind, "items": [item] * count})
                    self.assertEqual(panel_plain_text(encoded(value)) is not None, 1 <= count <= maximum)

    def test_controls_rejected_even_before_trim(self):
        for character in [chr(i) for i in range(32)] + [chr(i) for i in range(127, 160)]:
            for field in ["title", "summary"]:
                with self.subTest(character=ord(character), field=field):
                    value = panel({"type": "text", "text": "正文"})
                    value[field] = character + "正文" + character
                    self.assertIsNone(panel_plain_text(encoded(value)))
        for block in sample_blocks():
            block["title"] = "\n"
            self.assertIsNone(panel_plain_text(encoded(panel(block))))

    def test_unknown_keys_at_every_level_reject_entire_panel(self):
        valid = panel({"type": "metrics", "items": [{"label": "甲", "value": "0"}]})
        for location in [(), ("blocks", 0), ("blocks", 0, "items", 0)]:
            for key in ["unexpected", "onclick", "action", "script", "__proto__", "constructor"]:
                with self.subTest(location=location, key=key):
                    value = copy.deepcopy(valid)
                    target = value
                    for part in location:
                        target = target[part]
                    target[key] = "不得执行"
                    self.assertIsNone(panel_plain_text(encoded(value)))
        for kind, unexpected in [("text", "items"), ("metrics", "unit"), ("progress", "text"), ("bars", "text")]:
            value = next(block for block in sample_blocks() if block["type"] == kind)
            value[unexpected] = []
            self.assertIsNone(panel_plain_text(encoded(panel(value))))

    def test_missing_fields_and_wrong_types(self):
        for source in [None, False, 1, {}, [], "", "null", "[]", "true", "1", '"文字"', "{", "{}"]:
            with self.subTest(source=source):
                self.assertIsNone(panel_plain_text(source))
        for field in ["title", "blocks"]:
            value = panel({"type": "text", "text": "正文"})
            del value[field]
            self.assertIsNone(panel_plain_text(encoded(value)))
        for wrong in [None, False, 1, {}, "字符串"]:
            value = panel({"type": "text", "text": "正文"})
            value["blocks"] = wrong
            self.assertIsNone(panel_plain_text(encoded(value)))
        for block in sample_blocks():
            for field in list(block):
                if field == "title":
                    continue
                value = copy.deepcopy(block)
                del value[field]
                if field != "unit":
                    self.assertIsNone(panel_plain_text(encoded(panel(value))))
            value = copy.deepcopy(block)
            value["title"] = None
            self.assertIsNone(panel_plain_text(encoded(panel(value))))

    def test_item_types_and_unknown_block_are_not_partially_retained(self):
        for item in [None, [], 0, True, "文字"]:
            value = panel({"type": "metrics", "items": [{"label": "有效", "value": "0"}, item]})
            self.assertIsNone(panel_plain_text(encoded(value)))
        for block in [None, [], "正文", {"type": "unknown", "items": []}, {"type": 1, "items": []}]:
            self.assertIsNone(panel_plain_text(encoded(panel({"type": "text", "text": "有效"}, block))))

    def test_duplicate_keys_including_equivalent_escape_reject(self):
        sources = [
            '{"title":"旧","title":"新","blocks":[{"type":"text","text":"正文"}]}',
            '{"title":"题","blocks":[{"type":"text","text":"旧","text":"新"}]}',
            '{"title":"题","blocks":[{"type":"bars","items":[{"label":"甲","value":0,"value":1}]}]}',
            '{"title":"题","t\\u0069tle":"覆盖","blocks":[{"type":"text","text":"正文"}]}',
        ]
        for source in sources:
            self.assertIsNone(panel_plain_text(source))

    def test_json_grammar_and_plain_fence_body_only(self):
        valid = encoded(panel({"type": "text", "text": "正文"}))
        for source in ["```navren-panel\n" + valid + "\n```", "\ufeff" + valid, valid + "false", valid + "/*注释*/", valid[:-1] + ",}"]:
            self.assertIsNone(panel_plain_text(source))
        self.assertIsNone(panel_plain_text("[" * 2500 + "]" * 2500))

    def test_text_is_literal_and_no_state_or_calculated_total_is_added(self):
        literal = '<script>alert("示例")</script> & [链接](https://example.invalid)'
        value = panel({"type": "text", "text": literal}, {"type": "bars", "items": [{"label": "甲", "value": 2}, {"label": "乙", "value": 3}]})
        result = panel_plain_text(encoded(value))
        self.assertEqual(result, "资料核对\n" + literal + "\n甲：2\n乙：3")
        self.assertNotIn("合计", result)
        self.assertNotIn("已完成", result)


if __name__ == "__main__":
    unittest.main()
