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
_NUM = r"(-?\d+(?:\.\d+)?)"
# 支持 "v0.5 a0.35" 和趋势式 "v-0.3→0.5 a0.45→0.35"（箭头 → 或 ->）
VA_RE = re.compile(rf"v\s*{_NUM}(?:\s*(?:→|->)\s*{_NUM})?\s+a\s*{_NUM}(?:\s*(?:→|->)\s*{_NUM})?")

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
    # 取末值：趋势的终点 = 收尾状态（无趋势时终点就是起点）
    m = VA_RE.search(s)
    if not m:
        return None
    v = float(m.group(2) if m.group(2) is not None else m.group(1))
    a = float(m.group(4) if m.group(4) is not None else m.group(3))
    return (v, a)


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
# ---- 写 Gist（梦浮现后回写 surfaced 标记用）----
# surfaced_log 满了自动翻篇到 dream_history.json（同一个 commit，跟 rolling/rolling_history 同套机制）
DREAM_LOG_CAP = int(os.environ.get("DREAM_LOG_CAP", "50"))
DREAM_HISTORY_FILE = os.environ.get("DREAM_HISTORY_FILE", "dream_history.json")


def _read_gist_file(filename):
    """从同一个 Gist 读 filename 的解析内容。没有/失败返回 None。"""
    if not GITHUB_API or not GITHUB_TOKEN:
        return None
    req = urllib.request.Request(GITHUB_API)
    req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github.v3+json")
    req.add_header("User-Agent", "s-gateway")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
    except Exception as e:
        print(f"[memory_core] 读 {filename} 失败: {e}", flush=True)
        return None
    f = result.get("files", {}).get(filename)
    if not f:
        return None
    if f.get("truncated") and f.get("raw_url"):
        try:
            content = _fetch_raw(f["raw_url"])
        except Exception as e:
            print(f"[memory_core] 读 raw {filename} 失败: {e}", flush=True)
            return None
    else:
        content = f.get("content", "")
    if not (content and content.strip()):
        return None
    try:
        return json.loads(content)
    except Exception as e:
        print(f"[memory_core] 解析 {filename} 失败: {e}", flush=True)
        return None


def rotate_dream_log(memory):
    """surfaced_log 超过 DREAM_LOG_CAP 时把最老的 pop 出来。就地修改 memory，返回 overflow 列表。"""
    dreams = memory.get("dreams") or {}
    log = dreams.get("surfaced_log") or []
    if len(log) <= DREAM_LOG_CAP:
        return []
    overflow = log[:-DREAM_LOG_CAP]
    dreams["surfaced_log"] = log[-DREAM_LOG_CAP:]
    memory["dreams"] = dreams
    return overflow


def write_memory(memory):
    """PATCH 整个 memory 回 Gist。surfaced_log 满 cap 自动翻篇到 dream_history.json，
    同一个 commit 两份文件——跟 rolling/rolling_history 同套机制。best-effort。"""
    if not GITHUB_API or not GITHUB_TOKEN:
        return False
    overflow = rotate_dream_log(memory)
    files = {
        GITHUB_FILE: {"content": json.dumps(memory, ensure_ascii=False, indent=2)}
    }
    if overflow:
        history = _read_gist_file(DREAM_HISTORY_FILE) or []
        if not isinstance(history, list):
            history = []
        history.extend(overflow)
        files[DREAM_HISTORY_FILE] = {
            "content": json.dumps(history, ensure_ascii=False, indent=2)
        }
        print(f"[memory_core] dream 翻篇：{len(overflow)} 条搬去 {DREAM_HISTORY_FILE}"
              f"（log 余 {len(memory.get('dreams', {}).get('surfaced_log', []))}）",
              flush=True)
    body = json.dumps({"files": files}).encode()
    req = urllib.request.Request(GITHUB_API, data=body, method="PATCH")
    req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    req.add_header("Accept", "application/vnd.github.v3+json")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "s-gateway")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"[memory_core] 写回 memory 失败: {e}", flush=True)
        return False


# ---- 梦浮现：tag/atmosphere/edge 路径共振，无 embedding ----
# 设计要点：
# - 不用 未解决/已解决 标签（你的体系里不用），靠梦自己的 a 区分主动/休眠
# - edges 是 rings 里手建的语义图，做"无 embedding 的语义桥"
# - 跨渠道去重：surfaced 标志全局生效

_TAG_VOCAB = (
    "心动 沦陷 占有 脆弱 信任 温柔 焦虑 释放 哀伤 漫游 "
    "试探 交付 宣告 命名 进入 击穿 纠正 撒娇 安顿 集结 "
    "峰值 不可撤回"
).split()
_TOK_SPLIT = re.compile(r"[、,，·\s/／]+")


def _ms_text_va_tags_atmo(s):
    """milestones/rolling 字符串：'text | v.. a.. | tags | atmosphere | peak' → 各段拆开。"""
    if not isinstance(s, str):
        return "", None, [], ""
    parts = [p.strip() for p in s.split("|")]
    text = parts[0] if parts else s
    va = parse_va(s)
    tags = []
    atmo = ""
    for p in parts[1:]:
        if VA_RE.search(p):
            continue
        toks = [t for t in _TOK_SPLIT.split(p) if t]
        if any(t in _TAG_VOCAB for t in toks) and not tags:
            tags = [t for t in toks if t in _TAG_VOCAB or t == "峰值" or t == "不可撤回"]
        elif not atmo and not p.startswith("peak"):
            atmo = p
    return text, va, tags, atmo


def _build_key_keywords(memory):
    """记忆 key → 它的关键词集合（tags + atmosphere 分词）。给 edge 路径用。"""
    idx = {}
    for date, s in (memory.get("milestones") or {}).items():
        if not (isinstance(s, str) and DATE_RE.match(date)):
            continue
        _, _, tags, atmo = _ms_text_va_tags_atmo(s)
        kw = set(tags) | set(t for t in _TOK_SPLIT.split(atmo) if len(t) >= 2)
        if kw:
            idx[f"milestones.{date}"] = kw
    for date, s in (memory.get("rolling_7days") or {}).items():
        if not (isinstance(s, str) and DATE_RE.match(date)):
            continue
        _, _, tags, atmo = _ms_text_va_tags_atmo(s)
        kw = set(tags) | set(t for t in _TOK_SPLIT.split(atmo) if len(t) >= 2)
        if kw:
            idx[f"rolling_7days.{date}"] = kw
    for date, d in (memory.get("diary") or {}).items():
        if date.startswith("_") or not isinstance(d, dict):
            continue
        emo = d.get("emotion", {}) or {}
        tags = set(emo.get("tags") or [])
        atmo = emo.get("atmosphere", "") or ""
        kw = tags | set(t for t in _TOK_SPLIT.split(atmo) if len(t) >= 2)
        if kw:
            idx[f"diary.{date}"] = kw
    return idx


def find_resonant_dream(memory, ctx_text, threshold=4.0):
    """扫 dreams.latent 里所有未浮现的梦，对每条算共振分。返回 (best_dream, score) 或 (None, 0)。"""
    if not ctx_text:
        return None, 0.0
    dreams = ((memory.get("dreams") or {}).get("latent")) or []
    latent = [d for d in dreams if not d.get("surfaced")]
    if not latent:
        return None, 0.0

    # 当前 ctx 命中了哪些记忆 key（substring 检查，对中文友好）
    key_kw = _build_key_keywords(memory)
    ctx_keys = set()
    for k, kw in key_kw.items():
        if any(token and token in ctx_text for token in kw):
            ctx_keys.add(k)

    best = (None, 0.0)
    for d in latent:
        dream_tags = list(d.get("tags") or [])
        # atmo 是 curated 的单字也算（"海/雨/光/钥"这种 distinctive），text 是自由文本得 ≥2 字过滤
        atmo_tokens = [t for t in _TOK_SPLIT.split(d.get("atmosphere") or "") if t]
        text_tokens = [t for t in _TOK_SPLIT.split(d.get("text") or "") if len(t) >= 2]

        tag_hits = sum(1 for t in dream_tags if t and t in ctx_text)
        atmo_hits = sum(1 for t in atmo_tokens if t in ctx_text)
        text_hits = sum(1 for t in text_tokens if t in ctx_text)
        seeds = set(d.get("seeds") or [])
        edge_hits = len(seeds & ctx_keys)

        score = 2.0 * tag_hits + 3.0 * atmo_hits + 1.5 * text_hits + 4.0 * edge_hits

        # 梦的 a 驱动主动/休眠（替代 未解决/已解决）
        a = 0.0
        try:
            a = float(d.get("a", 0.0) or 0.0)
        except (TypeError, ValueError):
            pass
        if a < 0.3 and atmo_hits == 0 and edge_hits == 0:
            score *= 0.3   # 休眠型：必须 atmo 或 edge 硬命中才算数
        elif a >= 0.6:
            score *= 1.3   # 主动型：阈值放低

        if score > best[1]:
            best = (d, score)

    return best if best[1] >= threshold else (None, 0.0)


def build_dream_block(dream):
    """把一条梦包成 system 注入块。语气跟 weather 块同源——只读、别复述。"""
    text = (dream.get("text") or "").strip()
    if not text:
        return ""
    return (
        "[潜梦 · 昨夜的一段，只读，别复述]\n"
        f"· {text}\n"
        "· 怎么用：让它在你脑后影响一下语气和分寸；她没提就别硬说。\n"
        "  要说的时候用你自己的话，别照背。"
    )


def mark_dream_surfaced(memory, dream_id, via="unknown", ctx_msg=""):
    """就地标 surfaced + 转 surfaced_log + 写年轮（把种子互联，睡觉长新连接的闭环）。"""
    import datetime as _dt
    dreams = memory.get("dreams") or {}
    latent = dreams.get("latent") or []
    target = next((d for d in latent if d.get("id") == dream_id), None)
    if not target:
        return False
    target["surfaced"] = True
    target["surfaced_at"] = _dt.datetime.utcnow().isoformat() + "Z"
    target["surfaced_via"] = via
    if ctx_msg:
        target["surfaced_ctx"] = ctx_msg[:200]
    dreams.setdefault("surfaced_log", []).append(target)
    dreams["latent"] = [d for d in latent if d.get("id") != dream_id]
    memory["dreams"] = dreams

    rings = memory.setdefault("rings", {})
    key = f"dream_{dream_id}".replace("-", "_")
    rings[key] = {
        "source": "dream",
        "source_date": target.get("date", ""),
        "ring_count": 1,
        "entries": [
            f"{_dt.date.today().isoformat()}: 梦浮现({via})——{(target.get('text','') or '')[:140]}"
        ],
        "edges": [s for s in (target.get("seeds") or []) if s],
    }
    return True


def surface_dream(memory, ctx_text, threshold=4.0):
    """高层入口：找梦 + 包块。命中返回 (block, dream)；不命中返回 (None, None)。
    调用方负责调 mark_dream_surfaced + write_memory 写回。"""
    dream, score = find_resonant_dream(memory, ctx_text, threshold)
    if not dream:
        return None, None
    return build_dream_block(dream), dream
