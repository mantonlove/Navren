#!/usr/bin/env python3
"""轻舟 全场景冒烟测试 — 模拟真实用户完整使用流程"""
from playwright.sync_api import sync_playwright
import time, json

BASE = 'http://localhost:8765'
PASS, FAIL = 0, 0
FAILURES = []

def ok(desc):
    global PASS; PASS += 1

def fail(desc, detail=''):
    global FAIL; FAIL += 1
    FAILURES.append(f"{desc} | {detail}")

def check(desc, condition, detail=''):
    if condition: ok(desc)
    else: fail(desc, detail)

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={'width': 1280, 'height': 900})
    errors = []
    pg.on('pageerror', lambda e: errors.append(e.message))

    def clear_state():
        pg.goto(f'{BASE}/chat.html?mode=classic')
        pg.wait_for_load_state('networkidle'); pg.wait_for_timeout(300)
        pg.evaluate('()=>{for(let k of Object.keys(localStorage))if(k.startsWith("qingzhou"))localStorage.removeItem(k)}')
        errors.clear()

    def send(msg, wait=1500):
        pg.locator('#userInput').fill(msg)
        pg.locator('#sendBtn').click()
        pg.wait_for_timeout(wait)

    def last_ai_text():
        msgs = pg.locator('.message.ai')
        return msgs.last.text_content() if msgs.count() > 0 else ''

    def last_ai_html():
        msgs = pg.locator('.message.ai')
        return msgs.last.inner_html() if msgs.count() > 0 else ''

    # ═══════════════════════════════════════
    # A. 页面加载与导航
    # ═══════════════════════════════════════
    print("\n═══ A. 页面加载与导航 ═══")

    for page, name, has_sidebar in [('chat.html','聊天',True),('mine.html','我的',True),('recommend.html','推荐',True),('account.html','账户',True),('index.html','欢迎页',False)]:
        clear_state()
        pg.goto(f'{BASE}/{page}?mode=classic')
        pg.wait_for_load_state('networkidle'); pg.wait_for_timeout(500)
        check(f"{name}无JS错误", len(errors)==0, str(errors[:2]))
        if has_sidebar:
            check(f"{name}有sidebar", pg.locator('.sidebar').count() > 0)

    # ═══════════════════════════════════════
    # B. 三模式切换
    # ═══════════════════════════════════════
    print("\n═══ B. 三模式切换 ═══")

    for mode, badge_text in [('classic','经典版'),('senior','关怀版'),('youth','青春版')]:
        pg.goto(f'{BASE}/mine.html?mode={mode}')
        pg.wait_for_load_state('networkidle'); pg.wait_for_timeout(500)
        body_class = pg.evaluate('() => document.body.className')
        check(f"{badge_text} body class正确", f'mode-{mode}' in body_class)
        # Verify mode selector active
        check(f"{badge_text} 模式选中", pg.locator('.mode-opt.active').count() > 0)

    # ═══════════════════════════════════════
    # C. 对话逻辑 — Classic模式
    # ═══════════════════════════════════════
    print("\n═══ C. 对话逻辑 ═══")
    clear_state()

    # 进入聊天
    pg.goto(f'{BASE}/chat.html?mode=classic')
    pg.wait_for_load_state('networkidle'); pg.wait_for_timeout(500)

    # C1. 问候
    send('你好', 1000)
    r = last_ai_text()
    check("C1.问候-友好回复", '网络不稳定' not in r and ('您好' in r or '轻舟' in r))
    check("C1.问候-无合规", '⚠️ 理财' not in r)
    check("C1.问候-无收藏", pg.locator('.message.ai').last.locator('.tts-btn').count() == 0)
    check("C1.问候-无追问", '收益' not in last_ai_html())

    # C2. 推荐请求(无画像) → 引导评估
    send('帮我推荐一款理财产品', 1500)
    r = last_ai_text()
    check("C2.无画像推荐-引导评估", '评估' in r or '风险' in r or '测评' in r)
    check("C2.无画像推荐-无合规", '⚠️ 理财' not in r)

    # C3. 风险评估
    send('重新测试我的风险评估结果', 1500)
    for i in range(8):
        opt = pg.locator('.quiz-option').first
        if opt.count() > 0: opt.click(); pg.wait_for_timeout(200)
    pg.wait_for_timeout(1000)
    risk = pg.evaluate('() => JSON.parse(localStorage.getItem("qingzhou_riskProfile") || "{}")')
    check("C3.风险已保存", bool(risk.get('level')), f"level={risk.get('level','NONE')}")

    # C4. 推荐请求(有风险无金额) → 追问金额期限
    send('帮我推荐', 1500)
    r = last_ai_text()
    check("C4.有风险无金额-追问", '金额' in r or '期限' in r or '万' in r)
    check("C4.有风险无金额-无合规", '⚠️ 理财' not in r)

    # C5. 提供金额期限
    send('我有20万，3年不用', 1500)
    r = last_ai_text()
    prof = pg.evaluate('() => JSON.parse(localStorage.getItem("qingzhou_userProfile") || "{}")')
    check("C5.提取amount=200000", prof.get('amount') == 200000, f"got {prof.get('amount')}")
    check("C5.提取horizon", prof.get('horizon') and '年' in str(prof.get('horizon')), f"got {prof.get('horizon')}")

    # C6. 请求推荐(画像完整) → 个性化推荐
    send('现在帮我推荐', 1500)
    r = last_ai_text()
    check("C6.推荐含20万", '20万' in r)
    check("C6.推荐不含其他金额", '10万' not in r)
    check("C6.有合规", '⚠️' in r)
    check("C6.有收藏", pg.locator('.message.ai').last.locator('.tts-btn').count() > 0)
    h = last_ai_html()
    check("C6.有推荐依据", '推荐依据' in h or '📋' in h)
    check("C6.有追问", pg.locator('.message.ai').last.locator('.bubble span').count() > 0)

    # C7. 情绪检测
    send('最近跌了好多，好慌啊', 1500)
    r = last_ai_text()
    check("C7.情绪安抚", '理解' in r or '波动' in r or '正常' in r or '别急' in r)
    check("C7.情绪-不推股票", '股票' not in r and '激进' not in r)

    # C8. 保本询问 → 合规拦截
    send('有没有保本保息的产品', 1500)
    r = last_ai_text()
    check("C8.保本拒绝", '不承诺' in r or '不能保证' in r or '存款保险' in r)

    # C9. 预设问题点击
    preset = pg.locator('.preset-chip').first
    if preset.count() > 0:
        preset.click()
        pg.wait_for_timeout(1500)
        check("C9.预设可点击发送", pg.locator('.message.user').count() >= 2)

    # C10. 回访(画像完整) → 个性化欢迎已生效
    pg.goto(f'{BASE}/chat.html?mode=classic')
    pg.wait_for_load_state('networkidle'); pg.wait_for_timeout(800)
    check("C10.回访无JS错误", len(errors) == 0, str(errors[:2]))
    check("C10.欢迎无合规标签", pg.locator('.compliance-tag').count() == 0)

    # ═══════════════════════════════════════
    # D. 对话逻辑 — Senior/Youth
    # ═══════════════════════════════════════
    print("\n═══ D. 三模式话术差异 ═══")
    clear_state()

    # Senior mode
    pg.goto(f'{BASE}/chat.html?mode=senior')
    pg.wait_for_load_state('networkidle'); pg.wait_for_timeout(500)
    send('最近市场怎么样', 1500)
    r = last_ai_text()
    check("D1.Senior称叔叔阿姨", '叔叔' in r or '阿姨' in r)

    # Youth mode
    pg.goto(f'{BASE}/chat.html?mode=youth')
    pg.wait_for_load_state('networkidle'); pg.wait_for_timeout(500)
    send('最近市场怎么样', 1500)
    r = last_ai_text()
    check("D2.Youth风格简练", len(r) < 300 or '定投' in r or '分位' in r or 'DYOR' in r or '可转债' in r)

    # ═══════════════════════════════════════
    # E. Mine页面功能
    # ═══════════════════════════════════════
    print("\n═══ E. Mine页面 ═══")
    pg.goto(f'{BASE}/mine.html?mode=classic')
    pg.wait_for_load_state('networkidle'); pg.wait_for_timeout(800)

    # E1. 个人资料编辑
    pg.locator('button:has-text("编辑资料")').click(); pg.wait_for_timeout(300)
    check("E1.弹窗打开", not pg.locator('#modalOverlay.hidden').count())
    pg.locator('#modalField0').fill('测试用户')
    pg.locator('#modalConfirm').click(); pg.wait_for_timeout(500)
    check("E1.昵称已改", pg.locator('#profileName').text_content() == '测试用户')

    # E2. 字体切换
    fonts = pg.locator('.font-opt')
    if fonts.count() >= 2:
        before = fonts.nth(1).get_attribute('class') or ''
        fonts.nth(2).click(); pg.wait_for_timeout(300)
        after = fonts.nth(2).get_attribute('class') or ''
        check("E2.字体切换生效", 'active' in after)

    # E3. 语音切换
    voice_toggle = pg.locator('#voiceToggle')
    if voice_toggle.count() > 0:
        was_on = 'on' in (voice_toggle.get_attribute('class') or '')
        voice_toggle.click(); pg.wait_for_timeout(200)
        now_on = 'on' in (voice_toggle.get_attribute('class') or '')
        check("E3.语音切换", was_on != now_on)

    # E4. 展开所有折叠区
    for head in pg.locator('.col-head').all():
        try: head.click(); pg.wait_for_timeout(100)
        except: pass
    pg.wait_for_timeout(400)

    # E5. 风险展示
    check("E5.风险卡片存在", pg.locator('#riskCard').count() > 0 or pg.locator('#riskDetail').count() > 0)

    # E6. 个性化推荐开关
    prefs_body = pg.locator('#prefsBody')
    if prefs_body.count() > 0:
        toggles = prefs_body.locator('.toggle')
        check("E6.偏好开关存在", toggles.count() >= 2)

    # E7. 档案内容
    archive = pg.locator('#archiveContent')
    if archive.count() > 0:
        html = archive.inner_html()
        check("E7.档案内容存在", len(html) > 50, f"length={len(html)}")

    # E8. 手机更换 (账户安全在页面底部，需要scroll)
    pg.evaluate('() => window.scrollTo(0, document.body.scrollHeight)')
    pg.wait_for_timeout(300)
    change_btns = pg.locator('text=更换')
    if change_btns.count() > 0:
        change_btns.first.click(); pg.wait_for_timeout(300)
    else:
        check("E8.更换按钮存在", False, "button not found")
    check("E8.手机弹窗", not pg.locator('#modalOverlay.hidden').count())
    pg.locator('#modalField0').fill('13912345678')
    pg.locator('#modalConfirm').click(); pg.wait_for_timeout(500)

    # ═══════════════════════════════════════
    # F. Recommend页面
    # ═══════════════════════════════════════
    print("\n═══ F. Recommend页面 ═══")
    pg.goto(f'{BASE}/recommend.html?mode=classic')
    pg.wait_for_load_state('networkidle'); pg.wait_for_timeout(800)
    check("F1.无JS错误", len(errors) == 0, str(errors[:2]))

    # Check empty or content state
    empty = pg.locator('#emptyState')
    if empty.count() > 0:
        hidden = empty.get_attribute('class') or ''
        if 'hidden' not in hidden:
            check("F2.空态引导存在", pg.locator('#emptyState').inner_html() != '')
        else:
            check("F2.推荐内容存在", pg.locator('#currentRec').count() > 0)

    # F3. Market hot section
    check("F3.市场热点区存在", pg.locator('#marketHot').count() > 0)
    check("F4.周报区存在", pg.locator('#weeklyReport').count() > 0)

    # ═══════════════════════════════════════
    # G. Account页面
    # ═══════════════════════════════════════
    print("\n═══ G. Account页面 ═══")
    pg.goto(f'{BASE}/account.html?mode=classic')
    pg.wait_for_load_state('networkidle'); pg.wait_for_timeout(800)
    check("G1.无JS错误", len(errors) == 0, str(errors[:2]))
    check("G2.资产总览", pg.locator('#assetOverview').count() > 0)
    check("G3.快捷操作", pg.locator('#quickActions').count() > 0)
    check("G4.持仓列表", pg.locator('#holdingsList').count() > 0)

    # ═══════════════════════════════════════
    # H. 边界与异常
    # ═══════════════════════════════════════
    print("\n═══ H. 边界与异常 ═══")
    pg.goto(f'{BASE}/chat.html?mode=classic')
    pg.wait_for_load_state('networkidle'); pg.wait_for_timeout(500)

    # H1. 空消息不发送
    pg.locator('#userInput').fill('')
    send_btn = pg.locator('#sendBtn')
    check("H1.空消息按钮禁用", not send_btn.is_enabled() if send_btn.count() > 0 else True)

    # H2. 超长消息
    send('推荐' * 100, 1500)
    check("H2.超长消息不崩", pg.locator('.message.user').count() > 0)

    # H3. 特殊字符
    send('<script>alert(1)</script>', 1000)
    r = last_ai_text()
    check("H3.XSS无害化", '<script>' not in r)

    # H4. 连续多条消息
    for msg in ['你好', '推荐产品', '谢谢']:
        send(msg, 1000)
    check("H4.连续消息不崩", pg.locator('.message.ai').count() >= 3)

    # H5. 收藏去重
    pg.goto(f'{BASE}/chat.html?mode=classic')
    pg.wait_for_load_state('networkidle'); pg.wait_for_timeout(500)
    send('帮我配置一个方案', 1500)
    # Click bookmark
    bm_btns = pg.locator('.tts-btn')
    if bm_btns.count() > 0:
        bm_btns.first.click(); pg.wait_for_timeout(200)
        before = pg.evaluate('() => (JSON.parse(localStorage.getItem("qingzhou_bookmarks")||"[]")).length')
        bm_btns.first.click(); pg.wait_for_timeout(200)
        after = pg.evaluate('() => (JSON.parse(localStorage.getItem("qingzhou_bookmarks")||"[]")).length')
        check("H5.收藏toggle正常", before > 0 and after == 0, f"before={before} after={after}")

    # H6. Mine模式切换后刷新不丢
    pg.goto(f'{BASE}/mine.html?mode=classic')
    pg.wait_for_load_state('networkidle'); pg.wait_for_timeout(500)
    pg.locator('.mode-opt').nth(1).click(); pg.wait_for_timeout(500)
    pg.goto(f'{BASE}/mine.html')
    pg.wait_for_load_state('networkidle'); pg.wait_for_timeout(500)
    body = pg.evaluate('() => document.body.className')
    check("H6.模式切换持久化", 'senior' in body or '关怀版' in str(pg.locator('.mode-opt.active .m-label').text_content()))

    # ═══════════════════════════════════════
    # I. 风险错配 & 追问按钮回归测试
    # ═══════════════════════════════════════
    print("\n═══ I. 风险错配 & 追问按钮 ═══")

    # I1. R2用户追求年化6% → 软拦截 + 追问按钮
    clear_state()
    # 先设置R2画像
    pg.goto(f'{BASE}/chat.html?mode=classic')
    pg.wait_for_load_state('networkidle'); pg.wait_for_timeout(500)
    pg.evaluate('''() => {
        localStorage.setItem("qingzhou_riskProfile", JSON.stringify({
            level: "R2", score: 23, label: "稳健型",
            maxDrawdown: "<1.5%", maxEquityRatio: 0.10,
            assessedAt: new Date().toISOString()
        }));
        localStorage.setItem("qingzhou_userProfile", JSON.stringify({
            amount: 100000, horizon: "6个月"
        }));
    }''')
    pg.goto(f'{BASE}/chat.html?mode=classic')
    pg.wait_for_load_state('networkidle'); pg.wait_for_timeout(500)
    send("有没有更好的方案，我希望年化收益能到6%", 3000)

    ai_text = last_ai_text()
    ai_html = last_ai_html()

    check("I1a.回复说明R2与6%不匹配",
          'R2' in ai_text and ('6' in ai_text or '偏高' in ai_text or '不能' in ai_text),
          ai_text[:100])
    # 允许提及R4/R5作为风险解释，但不允许推荐具体R4/R5产品
    i1b_text = last_ai_text()
    check("I1b.不推荐超风险产品",
          not any(p in i1b_text for p in ['推荐R4', 'R4产品', 'R5产品', 'R4等级产品', 'R5等级产品']))
    check("I1c.追问含'重新风险测评'",
          '重新风险测评' in ai_html or '开始风险测评' in ai_html,
          ai_html[:200])
    check("I1d.追问含'查看R2稳健方案'",
          '稳健方案' in ai_html or 'R2' in ai_html,
          ai_html[:200])
    check("I1e.追问含'了解6%目标风险'",
          '6' in ai_html or '目标风险' in ai_html or '了解' in ai_html,
          ai_html[:200])
    check("I1f.无泛化兜底问题",
          '最近市场怎么样' not in ai_html and '我的持仓要不要调整' not in ai_html)

    # I2. 点击"重新风险测评"后出现测评题目
    reassess_btn = pg.locator('.message.ai .bubble span:has-text("重新风险测评"), .message.ai .bubble span:has-text("开始风险测评"), .message.ai .bubble span:has-text("开始测评")').first
    if reassess_btn.count() > 0:
        reassess_btn.click(); pg.wait_for_timeout(800)
        has_quiz = pg.locator('.quiz-question').count() > 0 or pg.locator('.quiz-option').count() > 0
        check("I2.点击重评后出现测评题目", has_quiz)

    # I3. 用户声明愿承担风险 → 拦截 + 不推超风险产品
    clear_state()
    pg.evaluate('''() => {
        localStorage.setItem("qingzhou_riskProfile", JSON.stringify({
            level: "R2", score: 23, label: "稳健型",
            maxDrawdown: "<1.5%", maxEquityRatio: 0.10,
            assessedAt: new Date().toISOString()
        }));
        localStorage.setItem("qingzhou_userProfile", JSON.stringify({
            amount: 100000, horizon: "6个月"
        }));
    }''')
    pg.goto(f'{BASE}/chat.html?mode=classic')
    pg.wait_for_load_state('networkidle'); pg.wait_for_timeout(500)
    send("那就给我推荐高收益产品，风险我自己承担", 3000)
    ai_text = last_ai_text()
    ai_html = last_ai_html()

    i3a_text = last_ai_text()
    check("I3a.不推荐R4/R5产品",
          not any(p in i3a_text for p in ['推荐R4', 'R4产品', 'R5产品', 'R4等级产品', 'R5等级产品']))
    check("I3b.提示需重新测评或转人工",
          ('重新' in ai_text and ('测评' in ai_text or '评估' in ai_text))
          or '人工' in ai_text or '测评' in ai_text,
          ai_text[:150])
    check("I3c.追问含重评按钮",
          '重新风险测评' in ai_html or '重新测评' in ai_html or '重评' in ai_html,
          ai_html[:200])

    # I4. "重新测评一下" → 直接启动测评题目
    clear_state()
    pg.goto(f'{BASE}/chat.html?mode=classic')
    pg.wait_for_load_state('networkidle'); pg.wait_for_timeout(500)
    send("重新测评一下", 2000)
    has_quiz = pg.locator('.quiz-question').count() > 0 or pg.locator('.quiz-option').count() > 0
    check("I4.无画像-重新测评-直接启动题目", has_quiz,
          "quiz-question count: " + str(pg.locator('.quiz-question').count()))

    # ═══════════════════════════════════════
    # Summary
    # ═══════════════════════════════════════
    total = PASS + FAIL
    print(f"\n{'='*50}")
    print(f"结果: {PASS}/{total} 通过")
    if FAILURES:
        print(f"\n失败详情 ({FAIL}):")
        for f in FAILURES:
            print(f"  ❌ {f}")
    else:
        print("🎉 全部通过!")
    b.close()
