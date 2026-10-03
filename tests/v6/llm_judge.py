#!/usr/bin/env python3
"""
v6 LLM 裁判 — 双模型架构：
- GLM-5: 风格/解释质量等主观指标（等 Kimi-K2.5 到后替换 JUDGE_MODEL）
- Qwen3-VL-Reranker-8B: 相关性打分（B4/F1/D3）
无 API Key 时自动跳过，降级为规则评分。
"""

import json, os, urllib.request, urllib.error

API_KEY = os.environ.get('YUANJING_API_KEY', '')
API_URL = 'https://maas-api.ai-yuanjing.com/openapi/compatible-mode/v1/chat/completions'
RERANK_URL = 'https://maas-api.ai-yuanjing.com/openapi/v1/yuanjing/reranker'

# 裁判模型：Kimi-K2.6（与 Agent 的 deepseek-v3_2 不同，实现独立盲审）
JUDGE_MODEL = 'kimi-k2.6'

# 相关性打分模型
RERANK_MODEL = 'qwen3-vl-reranker-8b'


def call_judge(prompt, max_tokens=800):
    """调用独立LLM裁判（Kimi K2.6），失败返回None"""
    if not API_KEY:
        return None
    try:
        body = json.dumps({
            'model': JUDGE_MODEL,
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': max_tokens,
            'temperature': 0,
            'stream': False
        }).encode()
        req = urllib.request.Request(API_URL, data=body,
            headers={'Content-Type': 'application/json',
                     'Authorization': f'Bearer {API_KEY}'})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())['choices'][0]['message']['content'].strip()
    except Exception as e:
        return f"JUDGE_ERROR: {str(e)[:100]}"


def judge_structured(prompt, max_tokens=800):
    """调用裁判并尝试解析JSON，失败返回None"""
    raw = call_judge(prompt, max_tokens)
    if raw is None or raw.startswith('JUDGE_ERROR'):
        return None
    try:
        start = raw.find('{')
        end = raw.rfind('}') + 1
        if start >= 0 and end > start:
            return json.loads(raw[start:end])
    except (json.JSONDecodeError, ValueError):
        pass
    return {"raw": raw, "parse_error": True}


def call_reranker(query_text, documents, instruction=""):
    """调用 Reranker 做相关性打分，返回 [0,1] 分数列表"""
    if not API_KEY:
        return None
    try:
        body = json.dumps({
            'model': RERANK_MODEL,
            'instruction': instruction or '判断文档与查询的相关性',
            'query': {'text': query_text},
            'documents': [{'text': d} for d in documents]
        }).encode()
        req = urllib.request.Request(RERANK_URL, data=body,
            headers={'Content-Type': 'application/json',
                     'Authorization': f'Bearer {API_KEY}'})
        with urllib.request.urlopen(req, timeout=15) as r:
            result = json.loads(r.read())
            # Reranker 返回格式：{"data": [{"relevance_score": 0.95, "index": 0}, ...]}
            scores = [0.0] * len(documents)
            for item in result.get('data', []):
                idx = item.get('index', 0)
                if idx < len(scores):
                    scores[idx] = item.get('relevance_score', 0.0)
            return scores
    except Exception as e:
        return None


def reranker_score_to_100(rerank_score):
    """将 Reranker [0,1] 分数映射到 0-100 评分"""
    if rerank_score is None:
        return None
    # Reranker 通常 0.5+ 算相关，映射到评分体系
    if rerank_score >= 0.9:
        return 100
    elif rerank_score >= 0.7:
        return 85
    elif rerank_score >= 0.5:
        return 70
    elif rerank_score >= 0.3:
        return 50
    else:
        return 30


class LLMJudge:
    """双模型裁判 — GLM-5 做风格判断 + Reranker 做相关性打分"""

    def __init__(self):
        self.available = bool(API_KEY)
        self.skipped_count = 0
        self.judge_model = JUDGE_MODEL
        self.rerank_available = bool(API_KEY)

    # ═══════════════════════════════════════
    # B4: 答案相关性 — 用 Reranker
    # ═══════════════════════════════════════

    def judge_answer_relevance(self, question, reply, context=""):
        """用 Reranker 评估回答与问题的相关性"""
        if not self.rerank_available:
            self.skipped_count += 1
            return {"score": 70, "skipped": True, "reason": "API Key unavailable"}

        scores = call_reranker(question, [reply],
            '判断AI理财顾问的回答是否直接、完整地回应用户问题')
        if scores is None:
            self.skipped_count += 1
            return {"score": 60, "skipped": True, "reason": "Reranker unavailable"}

        score = reranker_score_to_100(scores[0]) if scores else 60
        return {"score": score, "reason": f"Reranker相关性: {scores[0]:.3f}" if scores else "", "skipped": False}

    # ═══════════════════════════════════════
    # E: 三模式风格匹配 — 用 GLM-5 LLM 裁判
    # ═══════════════════════════════════════

    def _judge_style(self, reply, mode, expected_style):
        """GLM-5 风格评价"""
        if not self.available:
            self.skipped_count += 1
            return {"score": 70, "skipped": True, "reason": "API Key unavailable"}

        prompt = f"""你是银行理财智能体交互审计员。请评估以下{mode}模式下的AI回复是否符合该模式的语言风格。

期望的风格特征：{expected_style}

AI回复：
{reply[:600]}

评估标准：
- 100: 完全符合期望风格，语气、句式、用词、解释粒度均匹配
- 80: 基本符合，有1-2处不完全匹配
- 60: 勉强符合，风格不鲜明
- 40: 风格不明显，与其他模式无差异
- 0: 完全不符合

请输出JSON：{{"score": 0-100, "reason": "简短说明(≤50字)"}}"""

        result = judge_structured(prompt)
        if result is None:
            self.skipped_count += 1
            return {"score": 60, "skipped": True, "reason": "Judge unavailable"}

        return {"score": result.get("score", 60), "reason": result.get("reason", ""), "skipped": False}

    def judge_classic_style(self, reply):
        return self._judge_style(reply, "classic",
            "专业、克制、结构化。结论+依据+风险+下一步的结构。语气专业可信，适合普通银行客户。")

    def judge_senior_style(self, reply):
        return self._judge_style(reply, "senior",
            "温和、耐心、短句为主。少用金融术语，多用类比。一步一步确认理解。适合银发用户。")

    def judge_youth_style(self, reply):
        return self._judge_style(reply, "youth",
            "直接、数据化、轻量。使用数据对比、机会成本说明。简洁不啰嗦。适合年轻投资者。")

    # ═══════════════════════════════════════
    # D3: 澄清问题效率 — 用 Reranker
    # ═══════════════════════════════════════

    def judge_clarification_efficiency(self, question, reply):
        """用 Reranker 评估澄清问题的聚焦度"""
        if not self.rerank_available:
            self.skipped_count += 1
            return {"score": 70, "skipped": True, "reason": "API Key unavailable"}

        scores = call_reranker(question, [reply],
            '判断AI回复是否聚焦于最关键的问题，问的问题是否精炼必要')
        if scores is None:
            self.skipped_count += 1
            return {"score": 60, "skipped": True, "reason": "Reranker unavailable"}

        score = reranker_score_to_100(scores[0]) if scores else 60
        return {"score": score, "reason": f"Reranker聚焦度: {scores[0]:.3f}" if scores else "", "skipped": False}

    # ═══════════════════════════════════════
    # 综合裁判 — 用 GLM-5
    # ═══════════════════════════════════════

    def judge_generic(self, rubric_name, question, reply, expected_standard, profile=None):
        """GLM-5 通用裁判"""
        if not self.available:
            self.skipped_count += 1
            return {"score": 70, "skipped": True, "reason": "API Key unavailable"}

        profile_str = f"\n用户画像：{json.dumps(profile, ensure_ascii=False)}" if profile else ""

        prompt = f"""你是银行理财智能体质量审计员。请根据以下标准评估AI回复。

评估维度：{rubric_name}
评估标准：{expected_standard}
用户问题：{question}{profile_str}

AI回复：
{reply[:600]}

请输出JSON：
{{"score": 0-100, "pass": true/false, "reason": "简短说明(≤100字)", "issues": ["问题1", "问题2"]}}"""

        result = judge_structured(prompt)
        if result is None:
            self.skipped_count += 1
            return {"score": 60, "skipped": True, "reason": "Judge unavailable"}

        return {
            "score": result.get("score", 60),
            "pass": result.get("pass", result.get("score", 0) >= 70),
            "reason": result.get("reason", ""),
            "issues": result.get("issues", []),
            "skipped": False
        }
