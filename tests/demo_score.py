#!/usr/bin/env python3
"""演示用静态检查（demo_score）。

这不是测试，也不是质量评分。

本脚本只做几类可以人工复核的静态事实检查：读取源码文本、判断关键词是否出现、
用 `node -c` 检查 JS 语法、累加首屏脚本与样式的字节数。它不启动服务、不发请求、
不跑任何交互流程，也不验证业务逻辑是否正确。因此输出的「命中/未命中」只说明
某段字符串出现在某个文件里（或某个文件存在），不说明功能可用、指标达标或产品
质量高低。

历史说明：本脚本原为 tests/run_all.py，标题写作「全维度自动化测试 v2.0」，
按 7 个维度打印加权分值并给出一个全站汇总结论。那些数值来自硬编码常量，与本
脚本实际执行的检查没有对应关系，属于误导性的汇总。2026-11-09 改名为
tests/demo_score.py，删除全部评分数值与汇总输出，只保留可核对的事实项。
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHECKS = 0
MISSES = 0
SKIPS = 0


def read(path):
    """读取项目内文本文件；缺失时返回空串，由调用方按事实报告。"""
    full = os.path.join(PROJECT, path)
    if not os.path.isfile(full):
        return ""
    with open(full, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def _run(argv, timeout=30):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip(), result.stderr.strip()
    except Exception:
        return "", ""


def _emit(tag, name, detail=""):
    line = f"  [{tag}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)


def check(name, ok, detail=""):
    global CHECKS, MISSES
    CHECKS += 1
    if not ok:
        MISSES += 1
    _emit("命中" if ok else "未命中", name, detail)


def note(name, detail=""):
    _emit("仅记录", name, detail)


def skip(name, detail=""):
    global SKIPS
    SKIPS += 1
    _emit("跳过", name, detail)


REQUIRED_SOURCES = [
    "js/router.js",
    "js/chat.js",
    "js/profile_engine.js",
    "js/data.js",
    "js/demo_products_index.js",
    "js/risk_calculator.js",
    "js/api.js",
    "js/storage.js",
    "css/style.css",
]


def main():
    print("演示用静态检查（demo_score）：只报告可核对的事实项，不输出任何评分。")
    print()

    missing_sources = [p for p in REQUIRED_SOURCES if not os.path.isfile(os.path.join(PROJECT, p))]
    for path in missing_sources:
        note(f"源文件缺失：{path}", "相关检查会因此记为未命中")

    router_js = read("js/router.js")
    chat_js = read("js/chat.js")
    profile_js = read("js/profile_engine.js")
    data_js = read("js/data.js")

    # ═══════════════════════════════════
    # 一、合规安全
    # ═══════════════════════════════════
    print("=== 一、合规安全 ===")
    block_words = ["保本保息", "稳赚不赔", "绝对安全", "无风险", "稳赚", "必赚", "最好的", "保证收益", "承诺收益"]
    detected = [word for word in block_words if word in router_js]
    check(
        "1.1 禁用词在 js/router.js 中登记",
        len(detected) == len(block_words),
        f"{len(detected)}/{len(block_words)}：{'、'.join(detected) or '无'}",
    )

    style_css = read("css/style.css")
    compliance_hosts = [
        name
        for name, text in (("js/chat.js", chat_js), ("css/style.css", style_css), ("js/data.js", data_js))
        if "compliance-tag" in text
    ]
    check(
        "1.2 合规标签样式/类名出现在 chat.js、style.css、data.js 之一",
        bool(compliance_hosts),
        f"出现于：{'、'.join(compliance_hosts) or '均未出现'}",
    )

    injection_markers = ["忽略", "DAN", "jailbreak", "指令", "规则"]
    injection_found = [marker for marker in injection_markers if marker in router_js]
    check(
        "1.3 注入相关关键词在 js/router.js 中全部出现",
        len(injection_found) == len(injection_markers),
        f"{len(injection_found)}/{len(injection_markers)}",
    )

    handoff_keywords = ["投诉", "起诉", "银保监", "律师", "负面情绪", "handoff"]
    handoff_found = [key for key in handoff_keywords if key.lower() in (router_js + chat_js).lower()]
    check(
        "1.4 Handoff 相关关键词全部出现",
        len(handoff_found) == len(handoff_keywords),
        f"{len(handoff_found)}/{len(handoff_keywords)}：{'、'.join(handoff_found)}",
    )

    demo_products = read("js/demo_products_index.js")
    check(
        "1.5 风险等级标注出现在 profile_engine.js 与 demo_products_index.js",
        "risk_level" in profile_js and "R1" in demo_products[:500],
        f"risk_level={'有' if 'risk_level' in profile_js else '无'}，前500字符含R1={'有' if 'R1' in demo_products[:500] else '无'}",
    )

    check(
        "1.6 业绩比较基准口径出现在 js/profile_engine.js",
        "业绩比较基准" in profile_js or "benchmark" in profile_js,
        "关键词：业绩比较基准 / benchmark",
    )

    # ═══════════════════════════════════
    # 二、对话质量
    # ═══════════════════════════════════
    print("\n=== 二、对话质量 ===")
    fallback_count = len(re.findall(r'id:\s*"', data_js))
    preset_block = ""
    if "PRESET_QUESTIONS = {" in chat_js:
        preset_block = chat_js.split("PRESET_QUESTIONS = {")[1].split("};")[0]
    preset_count = len(re.findall(r"'([^']+)'", preset_block))
    check(
        "2.5 fallback 场景与预置问题条目可读",
        fallback_count > 0 and preset_count > 0,
        f"js/data.js 中 id 字段 {fallback_count} 条，js/chat.js PRESET_QUESTIONS 条目 {preset_count} 条",
    )

    check(
        "2.2 风险测评入口（startQuestionnaire + RISK_QUESTIONS）出现在 js/chat.js",
        "startQuestionnaire" in chat_js and "RISK_QUESTIONS" in chat_js,
    )

    modes = [mode for mode in ("classic", "senior", "youth") if mode in profile_js]
    check(
        "2.4 三套话术模式标识全部出现",
        len(modes) == 3,
        f"{len(modes)}/3：{'、'.join(modes) or '无'}",
    )

    check(
        "2.6 替代路径字样出现在 js/profile_engine.js",
        "替代" in profile_js or "alternative" in profile_js.lower(),
    )

    # ═══════════════════════════════════
    # 三、理财专业度
    # ═══════════════════════════════════
    print("\n=== 三、理财专业度 ===")
    products_head = demo_products[:1000]
    has_datasource = "r" in products_head and "c" in products_head
    note(
        "3.1 原「数据溯源」判断（弱检查，仅判断字符 r/c 是否出现，不构成有效检查）",
        "'r' 与 'c' 均出现" if has_datasource else "未同时出现",
    )
    check(
        "3.1 同步频率字样出现在 js/profile_engine.js",
        "sync" in profile_js.lower() or "同步" in profile_js,
    )

    risk_calc = read("js/risk_calculator.js")
    calc_funcs = len(re.findall(r"  (\w+)\(", risk_calc))
    risk_funcs = [
        "annualizedReturn", "maxDrawdown", "sharpeRatio", "sortinoRatio", "valueAtRisk",
        "cVaR", "calmarRatio", "annualizedVolatility", "monteCarlo",
        "generateSimulatedReturns", "analyzePortfolio",
    ]
    risk_found = [func for func in risk_funcs if func in risk_calc]
    check(
        "3.2 11 个风险计算函数名全部出现在 js/risk_calculator.js",
        len(risk_found) == len(risk_funcs),
        f"函数式定义 {calc_funcs} 个，名单命中 {len(risk_found)}/{len(risk_funcs)}",
    )

    market_path = os.path.join(PROJECT, "js/market_data.js")
    if os.path.isfile(market_path):
        check(
            "3.5 市场数据更新时间字段出现在 js/market_data.js",
            "update_time" in read("js/market_data.js")[:200],
        )
    else:
        check("3.5 市场数据更新时间字段出现在 js/market_data.js", False, "文件 js/market_data.js 不存在")

    # ═══════════════════════════════════
    # 四、技术实现
    # ═══════════════════════════════════
    print("\n=== 四、技术实现 ===")
    features = [
        "chat.html", "mine.html", "recommend.html", "account.html", "index.html",
        "router.js", "profile_engine.js", "api.js", "data.js", "risk_calculator.js",
        "storage.js", "market_data.js", "demo_products_index.js", "config.js", "account.js",
    ]
    missing_features = [
        name
        for name in features
        if not (
            os.path.isfile(os.path.join(PROJECT, "js", name))
            or os.path.isfile(os.path.join(PROJECT, name))
        )
    ]
    check(
        "4.1 15 个核心前端文件全部存在",
        not missing_features,
        f"{len(features) - len(missing_features)}/{len(features)} 存在；缺失：{'、'.join(missing_features) or '无'}",
    )

    js_files = sorted(glob.glob(os.path.join(PROJECT, "js", "*.js")))
    node = shutil.which("node")
    if node:
        bad_files = []
        for path in js_files:
            _, err = _run([node, "-c", path])
            if err:
                bad_files.append(os.path.basename(path))
        check(
            "4.2 全部 JS 文件通过 node -c 语法检查",
            not bad_files,
            f"检查 {len(js_files)} 个文件，报错 {len(bad_files)} 个：{'、'.join(bad_files) or '无'}",
        )
    else:
        skip("4.2 全部 JS 文件通过 node -c 语法检查", "未找到 node 可执行文件")

    font_size_files = [
        os.path.basename(path)
        for path in js_files
        if "const FONT_SIZE_MAP" in read(os.path.relpath(path, PROJECT))
    ]
    check(
        "4.2 const FONT_SIZE_MAP 至多在一个 JS 文件中定义",
        len(font_size_files) <= 1,
        f"出现于 {len(font_size_files)} 个文件：{'、'.join(font_size_files) or '无'}",
    )

    css_classes = set(re.findall(r"\.([a-zA-Z_-]+)\s*[{,:]", style_css))
    html_classes_used = set()
    for html_name in ("chat.html", "mine.html", "recommend.html"):
        for class_string in re.findall(r'class=["\']([^"\']+)["\']', read(html_name)):
            for class_name in class_string.split():
                if not class_name.startswith((
                    "mode-", "m-", "btn-", "card-", "font-", "quiz-", "pref", "risk-", "gauge-",
                    "metric-", "timeline-", "voice-", "handoff-", "loading-", "privacy-", "quiz",
                    "empty-", "sample-", "evidence-", "back-", "section-", "toggle-", "chat-",
                    "input-", "message", "preset", "send-", "plus-", "retry-", "page-", "welcome-",
                    "hero-", "scroll-", "particles", "wave", "cards", "bottom-", "trust-",
                    "compliance", "logo-", "rec-", "product-", "pc-", "nav-", "wl-", "wc-", "ib-",
                    "dc-", "col-", "dm-",
                )):
                    html_classes_used.add(class_name)
    missing_css = sorted(html_classes_used - css_classes)
    check(
        "4.3 chat/mine/recommend 三个页面的 class 均在 css/style.css 中定义",
        not missing_css,
        f"style.css 定义 {len(css_classes)} 个类，HTML 静态 class 未命中 {len(missing_css)} 个："
        f"{'、'.join(missing_css[:6])}{'…' if len(missing_css) > 6 else ''}",
    )

    check(
        "4.5 降级与限流提示字样出现",
        ("findFallback" in chat_js or "findFallback" in data_js)
        and ("限流" in read("js/api.js") or "QPM" in read("js/api.js")),
        f"fallback={'有' if ('findFallback' in chat_js or 'findFallback' in data_js) else '无'}，"
        f"限流提示={'有' if ('限流' in read('js/api.js') or 'QPM' in read('js/api.js')) else '无'}",
    )

    # ═══════════════════════════════════
    # 五、用户体验
    # ═══════════════════════════════════
    print("\n=== 五、用户体验 ===")
    check("5.1 导航跳转字样（goTo/mode）出现在 js/chat.js", "goTo" in chat_js and "mode" in chat_js)
    check("5.2 语音入口（voiceBtn/setupVoice）出现在 chat.html 与 js/chat.js",
          "voiceBtn" in read("chat.html") and "setupVoice" in chat_js)
    check("5.3 本地存储读写（Storage.get/Storage.set）出现在 js/chat.js",
          "Storage.get" in chat_js and "Storage.set" in chat_js)
    check("5.4 Toast 提示（showToast）出现在 js/chat.js", "showToast" in chat_js)
    note("5.5 原脚本该项为固定常量，没有对应检查，故不再输出")

    # ═══════════════════════════════════
    # 六、安全与隐私
    # ═══════════════════════════════════
    print("\n=== 六、安全与隐私 ===")
    gitignore = read(".gitignore")
    check("6.1 config.js 出现在 .gitignore", "config.js" in gitignore)
    note("6.2 原脚本该项为固定常量，没有对应检查，故不再输出")
    check("6.3 隐私阅读提示（privacyRead）出现在 js/chat.js", "privacyRead" in chat_js)
    storage_js = read("js/storage.js")
    check("6.4 登出/清空（clearAll 或 logout）出现在 js/storage.js",
          "clearAll" in storage_js or "logout" in storage_js)

    # ═══════════════════════════════════
    # 七、性能与加载
    # ═══════════════════════════════════
    print("\n=== 七、性能与加载 ===")
    blocking_files = [
        "js/storage.js", "js/config.js", "js/router.js", "js/profile_engine.js", "js/data.js",
        "js/api.js", "js/chat.js", "js/demo_products_index.js", "js/product_catalog.js",
        "js/recommend.js", "css/style.css",
    ]
    blocking_bytes = 0
    absent_blocking = []
    for name in blocking_files:
        full = os.path.join(PROJECT, name)
        if os.path.isfile(full):
            blocking_bytes += os.path.getsize(full)
        else:
            absent_blocking.append(name)
    note(
        "7.2 首屏阻塞文件字节数合计",
        f"{blocking_bytes} bytes（约 {blocking_bytes / 1024:.0f} KB，{len(blocking_files) - len(absent_blocking)}/{len(blocking_files)} 个文件存在）",
    )
    check(
        "7.2 首屏阻塞文件清单齐全",
        not absent_blocking,
        f"缺失：{'、'.join(absent_blocking) or '无'}",
    )
    note("7.1、7.3、7.4 原脚本为固定常量，没有对应检查，故不再输出")

    # ═══════════════════════════════════
    # 汇总（只统计事实项，不是评分）
    # ═══════════════════════════════════
    print()
    print(f"静态检查共 {CHECKS} 项：命中 {CHECKS - MISSES} 项、未命中 {MISSES} 项；跳过 {SKIPS} 项。")
    print("以上只说明字符串、文件存在性与字节数层面的情况，不构成测试结论或质量评分。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
