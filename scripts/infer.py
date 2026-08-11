#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AgentMemory infer — 类型/分类启发式推断（v2 热路径，零 LLM）

类型（fact / belief / preference）与分类（category）自动推断规则。
Q4 决策：自动推断 + 可覆盖（update --type），consolidate 时 LLM 复查。
Q6 决策：极简 add 降级时使用；低置信时手动路径可交互确认。
"""
import re

# ---- 类型推断 ----

# fact 强信号：规范编号、公式、实测数据、工具/API 用法
FACT_STRONG = [
    r"(?:GB|SL|DL|HJ|DB|NY|TB|JGJ)\s*[/T]?\s*\d{3,5}(?:[-–—]\s*\d{4})?",  # 规范编号
    r"[（(]?[《<]?[\u4e00-\u9fff]{2,12}(?:规范|规程|标准|手册|导则|条例)[》>]?[）)]?",  # 中文规范名
    r"(?:公式|函数|接口|API|命令|CLI|库|工具|软件|站点|断面|设备|型号)\s*[:：=]",  # 技术对象定义
    r"[A-Za-z\u4e00-\u9fff]{2,}\s*=\s*[^，。\n]{2,}",  # 定义式/公式（A=B 形式）
    r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}",  # 日期型事实
    r"(?:实测|测得|统计|记录|官方|文档|文档显示|源码|规范规定|标准要求|参数为|值为|结果为)",  # 来源性表述
]
# belief 强信号：观点、判断、方法论
BELIEF_STRONG = [
    r"(?:我认为|我觉得|建议|推荐|倾向|判断|推测|怀疑|觉得|考虑|打算|计划|应该|最好|值得|没必要)",
    r"(?:方法论|思路|框架|套路|流程|经验之谈|教训是)",
    r"(?:更好|更优|更合适|更简单|更高效|不推荐|避免|切记)",
]
# preference 强信号：喜好、习惯
PREFERENCE_STRONG = [
    r"(?:我喜欢|我喜欢用|习惯|偏好|更习惯|平时都用|一直用|总是用|从不|讨厌|不喜欢|愿意|坚持|固定用)",
    r"(?:以后|今后|每次|默认)(?:都|一律|统一|直接|只)",
    r"(?:报告|文档|表格|图表|模板)(?:用|都用|统一|一律|风格|样式|格式)",
]

# 用于低置信判断的中性词
FACT_MEDIUM = [r"(?:是|为|等于|位于|来自|属于|包含|参数|值为|结果为)", ]
BELIEF_MEDIUM = [r"(?:因为|由于|所以|相比之下|本质上|实际上|归根结底)", ]
PREFERENCE_MEDIUM = [r"(?:更|比较|相对|感觉)", ]


def infer_type(title, body, category=None, tags=None):
    """返回 (type, confidence)。confidence ∈ {'high','low'}，low 表示需要交互确认。"""
    text = f"{title}\n{body}"
    tags = tags or []
    cat = category or ""

    # 分类本身是偏好类时直接判定
    if cat == "user" or any("pref" in t or "偏好" in t for t in tags):
        return "preference", "high"
    if cat in ("failures", "corrections"):
        return "belief", "high"  # 踩坑/纠正是经验层

    def _score(patterns):
        return sum(1 for p in patterns if re.search(p, text))

    fs = _score(FACT_STRONG)
    bs = _score(BELIEF_STRONG)
    ps = _score(PREFERENCE_STRONG)

    # 强信号直接判定
    if ps >= 1 and ps >= fs and ps >= bs:
        return "preference", "high"
    if fs >= 1 and fs > bs and fs > ps:
        return "fact", "high"
    if bs >= 1 and bs > fs and bs > ps:
        return "belief", "high"
    # 平局或无强信号：用中性词细化
    if fs == 0 and bs == 0 and ps == 0:
        fm, bm, pm = (_score(x) for x in (FACT_MEDIUM, BELIEF_MEDIUM, PREFERENCE_MEDIUM))
        if fm >= 1 and fm >= bm and fm >= pm:
            return "fact", "high"
        if bm >= 1 and bm > fm:
            return "belief", "low"
        return "belief", "low"
    # 多信号冲突 → 低置信，交给调用方决定是否交互
    return "belief", "low"


# ---- 分类推断（category） ----

CATEGORY_HINTS = [
    ("failures", [r"(?:踩坑|报错|错误|失败|异常|崩溃|卡住|无法|不能|失败|bug|BUG|修复|解决|问题|坑)"]),
    ("corrections", [r"(?:纠正|更正|修正|之前说错|之前写错|重新理解|推翻|改口|补充说明)"]),
    ("patterns", [r"(?:模式|套路|模板|流程|惯例|每次|总是|定期|自动化|脚本化)"]),
    ("projects", [r"(?:项目|工程|任务|工作区|正在进行|这次做的|本项目的)"]),
    ("user", [r"(?:我喜欢|我的习惯|我的偏好|我通常|我的工作|我负责|我是)"]),
    ("knowledge", [r"(?:规范|标准|知识|原理|概念|资料|文档|教程|方法|公式)"]),
]


def infer_category(title, body, default="knowledge"):
    """返回 category。无命中返回 default（默认 knowledge，比 patterns 更中立）。"""
    text = f"{title}\n{body}"
    best, best_score = None, 0
    for cat, pats in CATEGORY_HINTS:
        s = sum(1 for p in pats if re.search(p, text))
        if s > best_score:
            best, best_score = cat, s
    return best or default


def infer_tags(title, body, max_tags=3):
    """从标题/正文抽取候选标签（关键词截取，简单启发式）。"""
    text = title
    candidates = []
    # 常见领域词
    for kw in ["水文", "水位", "流量", "降水", "断面", "水库", "圩堤", "洪量", "频率",
               "Excel", "Python", "HTML", "Word", "KML", "DEM", "SQL", "git", "pi",
               "飞书", "FastGPT", "数据库", "API", "Windows", "Linux", "npm", "脚本"]:
        if kw.lower() in text.lower() and kw not in candidates:
            candidates.append(kw)
    return candidates[:max_tags]


if __name__ == "__main__":
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else "测试"
    b = sys.stdin.read().strip() if not sys.stdin.isatty() else ""
    print(f"type     : {infer_type(t, b)}")
    print(f"category : {infer_category(t, b)}")
    print(f"tags     : {infer_tags(t, b)}")
