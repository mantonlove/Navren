"""只导出指定浏览器运行的脱敏审计，不输出Cookie/工作区/账户标识。"""
from pathlib import Path
import sqlite3,json
rid='19d3c4ab0a0e41eaa4402ccbaa5acce5'
with sqlite3.connect(Path.home()/'Library/Application Support/Qingzhou/simulated-bank.sqlite3')as db:
 row=db.execute("SELECT details FROM audit WHERE event='agent_turn_completed' AND json_extract(details,'$.request_id')=?",(rid,)).fetchone()
assert row
r=json.loads(row[0]);safe={k:r[k]for k in ['primary_model','reviewer_model','tools','tool_calls','review_calls','duration_ms','primary_tokens','failure']}
assert safe['failure']is None and safe['review_calls']==1 and 'wealth_product'in safe['tools']
# 浏览器测得的页面快照，与下面的实际请求审计分开保存；不是由审计推断界面结果。
# 完成后再次打开聊天，实际 .runtime-panel / .runtime-step 均为 0，最终 3.6834 可见。
safe.update(response_id=rid,browser_displayed={'fund_id':'ORIENT:006210','nav_on':'2026-09-30','quantity':'1000','unit_nav':'3.6834','daily_pnl_minor':-400,'closing_equity_minor':368340,'review_notice_visible':True,'running_details_after_completion':0,'checked_runtime_selectors':['.runtime-panel','.runtime-step'],'horizontal_overflow':False},screenshot='output/equity/chat-real-model.png')
Path('output/equity/browser-runtime.json').write_text(json.dumps(safe,ensure_ascii=False,indent=2));print(json.dumps(safe,ensure_ascii=False))
