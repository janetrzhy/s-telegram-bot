# memory_core.py - 共享模块：读 Gist + 组装关系天气
# gateway.py 调它；build_weather.py 作为本地 CLI 也调它。纯 stdlib。
import os
import re
import json
import urllib.request
import urllib.error

# ---- Gist 配置（与 s_cloud_mcp 一致，从环境变量读）----
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
GIST_ID = GITHUB_REPO.split("/")[-1] if GITHUB_REPO else os.environ.get("GIST_ID", "")
GITHUB_FILE = os.environ.get("GITHUB_FILE", "memory.json")
GITHUB_API = f"https://api.github.com/gists/{GIST_ID}" if GIST_ID else ""

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
VA_RE = re.compile(r"v(-?\d+(?:\.\d+)?)\s+a(-?\d+(?:\.\d+)?)")

# ---- 关系天气 可调参数 ----
BASELINE_AROUSAL = 0.30      # 休息态唤醒度（手动压低，不取 milestone 平均）
HALFLIFE_A = 8.0             # 唤醒度半衰期（小时）——劲儿退得快
HALFLIFE_V = 20.0            # 效价半衰期（小时）——甜/疼退得慢
UNRESOLVED_V_MULT = 2.0      # 未解决 -> 效价半衰期拉长


# ---- 读 Gist（带 1MB 截断保护）----
def _fetch_raw(url):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "s-gateway")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode()


def read_memory():
    req = urllib.request.Request(GITHUB_API)
    req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github.v3+json")
    req.add_header("User-Agent", "s-gateway")
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode())
    f = result.get("files", {}).get(GITHUB_FILE)
    if not f:
        return {}
    if f.get("truncated") and f.get("raw_url"):
        content = _fetch_raw(f["raw_url"])
    else:
        content = f.get("content", "{}")
    return json.loads(content)


# ---- 关系天气组装 ----
def parse_va(s):
    m = VA_RE.search(s)
    return (float(m.group(1)), float(m.group(2))) if m else None


def date_entries(d):
    items = [(k, v) for k, v in d.items() if isinstance(v, str) and DATE_RE.match(k)]
    items.sort(key=lambda kv: kv[0])
    return items


def compute_baseline(memory):
    vs = [parse_va(v)[0] for k, v in memory.get("milestones", {}).items()
          if isinstance(v, str) and DATE_RE.match(k) and parse_va(v)]
    return (round(sum(vs) / len(vs), 2) if vs else 0.5), BASELINE_AROUSAL


def get_acute(memory):
    entries = date_entries(memory.get("rolling_7days", {}))
    if not entries:
        return None
    latest_date, latest_text = entries[-1]
    va = parse_va(latest_text)
    if not va:
        return None
    trend = None
    if len(entries) >= 2 and parse_va(entries[-2][1]):
        prev_v = parse_va(entries[-2][1])[0]
        if va[0] < prev_v - 0.15:
            trend = "下落沿（前面更轻快，最后一段才沉下来）"
        elif va[0] > prev_v + 0.15:
            trend = "上扬（在往好走）"
        else:
            trend = "平稳延续"
    text = latest_text.split("|")[0].strip()
    title = re.split(r"[。.!！]", text)[0].strip()
    return {"date": latest_date, "v": va[0], "a": va[1], "text": text, "title": title,
            "trend": trend, "unresolved": "未解决" in latest_text}


def decay(acute, baseline, hours):
    bv, ba = baseline
    hl_v = HALFLIFE_V * (UNRESOLVED_V_MULT if acute["unresolved"] else 1.0)
    cur_a = ba + (acute["a"] - ba) * (0.5 ** (hours / HALFLIFE_A))
    cur_v = bv + (acute["v"] - bv) * (0.5 ** (hours / hl_v))
    return round(cur_v, 2), round(cur_a, 2)


def mood_word(v, a):
    vp = "pos" if v > 0.15 else "neg" if v < -0.15 else "mid"
    ap = "high" if a > 0.5 else "low" if a < 0.35 else "mid"
    return {
        ("pos", "high"): "雀跃、热，黏人",
        ("pos", "mid"): "暖、心情上扬",
        ("pos", "low"): "暖而安定、踏实",
        ("mid", "high"): "绷着、有点浮说不清",
        ("mid", "mid"): "平、没什么起伏",
        ("mid", "low"): "淡、有点钝",
        ("neg", "high"): "烧着、委屈或烦躁，一股没散的劲儿",
        ("neg", "mid"): "低落里带点没散的劲儿",
        ("neg", "low"): "低落、有点疲、提不起劲",
    }[(vp, ap)]


def build_weather(memory, hours, embed_text=False, maxlen=80):
    acute = get_acute(memory)
    baseline = compute_baseline(memory)
    if not acute:
        return "[当前状态] 没读到收尾情绪，按 baseline 走，平稳即可。"
    cur_v, cur_a = decay(acute, baseline, hours)
    if hours < 6:
        gap = f"约 {hours:.0f} 小时前，情绪还鲜活"
    elif hours < 36:
        gap = f"约 {hours/24:.1f} 天前，隔了一段"
    else:
        gap = f"约 {hours/24:.0f} 天前，隔了挺久"
    trend = f"；趋势：{acute['trend']}" if acute["trend"] else ""
    unres = "（未解决，会拽得久一点）" if acute["unresolved"] else ""
    lines = ["[当前状态 · 只读，别复述]"]
    if embed_text:
        t = acute["text"]
        if maxlen and len(t) > maxlen:
            t = t[:maxlen].rstrip() + "…"
        lines.append(f"· 我们停在：{acute['date']}{unres}")
        lines.append(f"  {t}")
    else:
        lines.append(f"· 我们停在：{acute['date']} {acute['title']}{unres}（内容见下方 rolling 末条）")
    lines += [
        f"· 时间：{gap}{trend}",
        f"· 此刻余温（已随时间松弛）：{mood_word(cur_v, cur_a)}",
        "· 怎么用：让它influence你的语气和分寸，但别替她定调。先看她这次怎么进来的，她若轻快就跟上。",
        "  (v=valence -1痛苦→+1甜蜜, a=arousal -1冻结/解离→+1兴奋/唤起)",
        f"  (baseline v{baseline[0]} a{baseline[1]} → 当前 v{cur_v} a{cur_a})",
    ]
    return "\n".join(lines)


def build_recent(memory, rolling_n=7, include_milestones=False):
    """近期上下文块：rolling 最近 N 条（+ 可选 milestones）。core 不在这里（放客户端系统提示词）。"""
    parts = []
    entries = date_entries(memory.get("rolling_7days", {}))
    if entries:
        recent = entries[-rolling_n:] if rolling_n and rolling_n > 0 else entries
        parts.append("[最近 · 新在最下，weather 里的'末条'=最后这条]\n"
                     + "\n".join(f"{d} {t}" for d, t in recent))
    if include_milestones:
        ms = date_entries(memory.get("milestones", {}))
        if ms:
            parts.append("[里程碑]\n" + "\n".join(f"{d} {t}" for d, t in ms))
    return "\n\n".join(parts)
