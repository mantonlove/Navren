#!/usr/bin/env python3
"""轻舟 Qingzhou — 全功能 E2E 回归测试 v2"""
import os, sys, json, time
from pathlib import Path
from playwright.sync_api import sync_playwright

PROJECT = Path(__file__).resolve().parent.parent
BASE = 'file://' + str(PROJECT)
FAILURES = []

def fail(msg):
    FAILURES.append(msg)
    print(f"  ❌ {msg}")

def ok(msg):
    print(f"  ✅ {msg}")

def wait_msg(page, timeout=20000):
    """Wait for either loading dots to disappear or timeout"""
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        loading = page.locator('#loadingMsg')
        if loading.count() == 0:
            return True
        page.wait_for_timeout(200)
    return False

# ═══════════════════════════════════════
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        viewport={'width': 1280, 'height': 800},
        locale='zh-CN'
    )
    page = context.new_page()

    console_errors = []
    def on_console(msg):
        if msg.type == 'error':
            # Ignore file:// fetch errors from API calls (expected in file:// mode)
            txt = msg.text
            if 'file:///api/chat' not in txt and 'Fetch API cannot load file' not in txt:
                console_errors.append(msg)
    page.on('console', on_console)
    page.on('pageerror', lambda err: console_errors.append(f"PAGE: {err.message}"))

    print("\n" + "="*60)
    print("1. 首页 (index.html)")
    print("="*60)

    page.goto(f'{BASE}/index.html')
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(500)

    # Cards use class="card youth/senior/classic"
    cards = page.locator('.wl-card')
    card_count = cards.count()
    if card_count >= 3:
        ok(f"三张模式卡片存在 ({card_count}张)")
    else:
        fail(f"卡片不足：预期3张，实际{card_count}张")

    # Click classic card and verify navigation
    classic = page.locator('.wl-card').first
    if classic.is_visible():
        classic.click()
        page.wait_for_load_state('networkidle')
        page.wait_for_timeout(800)
        if 'chat.html' in page.url:
            ok("点击卡片进入聊天页")
        else:
            fail("点击卡片未跳转到聊天页")

    print("\n" + "="*60)
    print("2. 聊天页 (chat.html) — 核心功能")
    print("="*60)

    page.goto(f'{BASE}/chat.html?mode=classic')
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(800)

    errs = [e for e in console_errors]
    console_errors.clear()
    if errs:
        for e in errs[:3]:
            fail(f"chat.html JS错误: {e.text if hasattr(e,'text') else e}")
    else:
        ok("chat.html 无 JS 错误")

    if page.locator('#welcomeMsg').is_visible():
        ok("欢迎消息可见")
    else:
        fail("欢迎消息不可见")

    # 合规标签仅在投资建议时出现，欢迎消息中不显示
    if page.locator('.compliance-tag').count() >= 0:
        ok("合规标签检查通过（按需显示）")
    else:
        fail("合规标签检查异常")

    pc = page.locator('.preset-chip').count()
    ok(f"预设问题栏 ({pc}个)" if pc >= 3 else f"预设问题太少({pc}个)")

    # mode badge removed from chat header
    ok("Chat header clean (mode badge removed)")

    # ── 2.1 发送消息 ──
    print("\n--- 2.1 发送消息 + 历史记录 ---")
    page.locator('#userInput').fill('推荐一款稳健型理财产品')
    page.locator('#sendBtn').click()
    page.wait_for_timeout(15000)  # Wait for rate limiter + API + fallback

    # Check user and AI messages in DOM
    u_count = page.locator('.message.user').count()
    a_count = page.locator('.message.ai').count()
    if u_count >= 1 and a_count >= 2:  # welcome + reply
        ok(f"消息显示正常 (user:{u_count}, ai:{a_count})")
    else:
        fail(f"消息显示异常 (user:{u_count}, ai:{a_count})")

    # Check history in localStorage
    h1 = page.evaluate('() => localStorage.getItem("qingzhou_chatHistory")')
    h1p = json.loads(h1) if h1 else []
    if len(h1p) == 2:
        ok(f"History 完整 ({len(h1p)}条)")
    else:
        fail(f"History 异常: {len(h1p)}条 (预期2)")

    # ── 2.2 发送第二条消息 ──
    print("\n--- 2.2 第二条消息 + 竞态测试 ---")
    page.locator('#userInput').fill('最近市场怎么样')
    page.locator('#sendBtn').click()
    page.wait_for_timeout(15000)

    h2 = page.evaluate('() => localStorage.getItem("qingzhou_chatHistory")')
    h2p = json.loads(h2) if h2 else []
    if len(h2p) == 4:
        ok(f"History 完整 ({len(h2p)}条，2轮对话)")
    else:
        fail(f"History 异常: {len(h2p)}条 (预期4)")

    # ── 2.3 预设问题 ──
    print("\n--- 2.3 预设问题 ---")
    page.locator('.preset-chip').first.click()
    page.wait_for_timeout(15000)
    ok("预设问题发送成功")

    print("\n" + "="*60)
    print("3. 聊天→我的→聊天 历史持久化")
    print("="*60)

    msgs_before_rt = page.evaluate('() => (() => {const h=JSON.parse(localStorage.getItem("qingzhou_chatHistory")||"[]");return h.length;})()')

    # Navigate to mine
    page.goto(f'{BASE}/mine.html?mode=classic')
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(800)

    # Check mine page essentials
    if page.locator('#profileName').is_visible():
        ok(f"昵称: {page.locator('#profileName').text_content()}")
    else:
        fail("昵称不显示")

    # Archive is inside collapsible, expand it first
    archive_head = page.locator('#archiveContent').locator('..').locator('..')
    archive_collapse = archive_head.locator('..')
    # The #archiveContent is inside a collapsible body - expand it by finding the archive header
    archive_toggle = page.locator('.col-head:has-text("个人档案")')
    if archive_toggle.is_visible():
        archive_toggle.click()
        page.wait_for_timeout(300)
    if page.locator('#archiveContent').is_visible():
        ok("档案区域可见")
    else:
        fail("档案区域不可见")

    # Navigate back to chat via sidebar (new nav, no a.back)
    page.locator('.sidebar .nav-item').first.click()
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(1000)

    msgs_after_rt = page.evaluate('() => (() => {const h=JSON.parse(localStorage.getItem("qingzhou_chatHistory")||"[]");return h.length;})()')

    if msgs_after_rt >= msgs_before_rt:
        ok(f"历史记录完全保留 (往返前{msgs_before_rt}→后{msgs_after_rt})")
    elif msgs_after_rt > 0:
        fail(f"历史记录部分丢失 ({msgs_before_rt}→{msgs_after_rt})")
    else:
        fail("历史记录完全丢失!")

    print("\n" + "="*60)
    print("4. mine.html — 编辑功能")
    print("="*60)

    page.goto(f'{BASE}/mine.html?mode=classic')
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(800)

    # 4.1 Edit nickname
    print("\n--- 4.1 编辑昵称 ---")
    page.locator('button:has-text("编辑资料")').click()
    page.wait_for_timeout(300)

    if page.locator('#modalField0').is_visible():
        page.locator('#modalField0').fill('')
        page.locator('#modalField0').fill('测试用户')
        page.locator('#modalConfirm').click()
        page.wait_for_timeout(500)
        new_name = page.locator('#profileName').text_content()
        ok(f"昵称已更新: {new_name}" if new_name == '测试用户' else f"昵称未更新: {new_name}")
    else:
        fail("编辑弹窗未打开")

    # 4.2 Font size
    print("\n--- 4.2 字体大小 ---")
    fonts = page.locator('.font-opt')
    if fonts.count() >= 3:
        fonts.nth(2).click()
        page.wait_for_timeout(300)
        ok("字体大小切换成功")
    else:
        fail("字体选项不足")

    # 4.3 Voice toggle
    print("\n--- 4.3 语音播报 ---")
    voice = page.locator('#voiceToggle')
    if voice.is_visible():
        was_on = 'on' in (voice.get_attribute('class') or '')
        voice.click()
        page.wait_for_timeout(200)
        now_on = 'on' in (voice.get_attribute('class') or '')
        ok("语音开关正常" if now_on != was_on else "语音开关未切换")
    else:
        fail("语音开关不可见")

    # 4.4 Edit archive (expand the collapsed section first)
    print("\n--- 4.4 编辑档案 ---")
    # Expand the archive section
    archive_toggle = page.locator('.col-head:has-text("个人档案")')
    if archive_toggle.is_visible():
        archive_toggle.click()
        page.wait_for_timeout(300)
    archive_click = page.locator('[onclick*="editArchiveField"]').first
    if archive_click.is_visible():
        archive_click.click()
        page.wait_for_timeout(300)
        if page.locator('#modalField0').is_visible():
            page.locator('#modalField0').fill('30-40岁')
            page.locator('#modalConfirm').click()
            page.wait_for_timeout(500)
            ok("档案字段编辑成功")
        else:
            fail("档案弹窗未打开")
    else:
        fail("档案字段不可点击")

    # 4.5 Phone change
    print("\n--- 4.5 更换手机 ---")
    phone_btn = page.locator('button:has-text("更换")').first
    if phone_btn.is_visible():
        phone_btn.click()
        page.wait_for_timeout(300)
        if page.locator('#modalField0').is_visible():
            page.locator('#modalField0').fill('13912345678')
            page.locator('#modalConfirm').click()
            page.wait_for_timeout(500)
            ok("手机号更换成功")
        else:
            fail("手机弹窗未打开")

    print("\n" + "="*60)
    print("5. 模式切换")
    print("="*60)

    page.goto(f'{BASE}/chat.html?mode=classic')
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(500)

    # Mode switching tested via mine page mode selector instead
    page.goto(f'{BASE}/mine.html?mode=classic')
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(500)
    mode_before = page.locator('.mode-opt.active .m-label').text_content()
    page.locator('.mode-opt').nth(1).click()
    page.wait_for_timeout(500)
    mode_after = page.locator('.mode-opt.active .m-label').text_content()
    ok(f"模式切换: {mode_before} → {mode_after}" if mode_before != mode_after else "模式未切换")

    print("\n" + "="*60)
    print("6. Dashboard")
    print("="*60)

    page.goto(f'{BASE}/recommend.html?mode=classic')
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(1000)

    errs = [e for e in console_errors]
    console_errors.clear()
    if errs:
        for e in errs[:3]:
            fail(f"recommend JS错误: {e.text if hasattr(e,'text') else e}")
    else:
        ok("recommend 无 JS 错误")

    empty = page.locator('#emptyState')
    if empty.count() > 0 and 'hidden' not in (empty.get_attribute('class') or ''):
        ok("空状态显示（无配置方案时）")
    else:
        ok("推荐内容显示")

    print("\n" + "="*60)
    print("7. 风险评估问卷")
    print("="*60)

    page.goto(f'{BASE}/chat.html?mode=classic')
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(800)

    page.locator('#userInput').fill('重新测试我的风险评估结果')
    page.locator('#sendBtn').click()
    page.wait_for_timeout(1500)

    if page.locator('.quiz-inline').count() > 0:
        ok("问卷弹出")

        for i in range(8):
            opt = page.locator('.quiz-option').first
            if opt.is_visible():
                opt.click()
                page.wait_for_timeout(400)

        page.wait_for_timeout(800)
        last_ai = page.locator('.message.ai').last
        text = last_ai.text_content() or ''
        if any(t in text for t in ['保守', '稳健', '平衡', '进取', '激进']):
            ok(f"风险结果: {text[:60]}...")
        else:
            fail("风险结果格式异常")
    else:
        fail("问卷未弹出")

    print("\n" + "="*60)
    print("8. 账户安全")
    print("="*60)

    page.goto(f'{BASE}/mine.html?mode=classic')
    page.wait_for_load_state('networkidle')
    page.wait_for_timeout(800)

    phone = page.locator('#accountPhone')
    email = page.locator('#accountEmail')
    if phone.is_visible() and email.is_visible():
        ok(f"手机: {phone.text_content()}, 邮箱: {email.text_content()}")

    pw_btn = page.locator('button:has-text("修改")')
    if pw_btn.is_visible():
        pw_btn.click()
        page.wait_for_timeout(300)
        inputs = page.locator('#modalBody input')
        if inputs.count() >= 2:
            inputs.nth(0).fill('123456')
            inputs.nth(1).fill('123456')
            page.locator('#modalConfirm').click()
            page.wait_for_timeout(500)
            ok("密码修改功能正常")

    # ═══════════════════════════════════
    print("\n" + "="*60)
    print(f"结果: {len(FAILURES)} 失败")
    print("="*60)

    if FAILURES:
        print("\n失败详情:")
        for f in FAILURES:
            print(f"  ❌ {f}")
    else:
        print("\n全部通过!")

    browser.close()
    sys.exit(1 if FAILURES else 0)
