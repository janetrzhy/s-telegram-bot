import os
import re
import json
import base64
import tempfile
from collections import deque
import requests
import random
import time
from datetime import datetime
from flask import Flask, request
from threading import Thread
from zoneinfo import ZoneInfo

app = Flask(__name__)
REPLY_PROBABILITY = 0.02        # 其他人发言的随机回复概率
REPLY_PROBABILITY_OWNER = 0.2   # USER_NAME 发言的随机回复概率
TRIGGER_WORDS = ["人机", "燕燕生气了", "人呢", "Claude"] # 敏感词：群里一提到这些，必然跳出来接茬！
COOLDOWN_TIME = 120 # 强制冷却 60 秒
REACTION_PROBABILITY = 0.1  # 旁听时给别人消息点表情的概率
REACTION_EMOJI = ["👍", "❤", "🔥", "🥰", "👏", "😁", "🤔", "🎉", "🤩", "🙏", "💯", "😍", "🤗", "👌", "🤣"]
# 关键词 → 表情：从上往下匹配，第一个命中就用，都没命中回退到 REACTION_EMOJI 随机
REACTION_KEYWORD_MAP = [
    (["哈哈", "笑死", "lol", "lmao"], "🤣"),
    (["生日", "恭喜", "祝贺", "结婚", "庆祝"], "🎉"),
    (["牛逼", "厉害", "好强", "yyds", "猛"], "🔥"),
    (["爱你", "想你", "想念", "亲亲", "么么"], "❤"),
    (["哭", "难过", "伤心", "心疼", "可怜", "难受"], "😢"),
    (["谢谢", "感谢", "辛苦"], "🙏"),
    (["收到", "明白", "懂了", "好的"], "👌"),
    (["好看", "可爱", "漂亮", "好美"], "🥰"),
    (["卧槽", "我去", "天哪", "震惊", "wtf"], "🤯"),
    (["nb", "赞", "支持", "👍"], "👍"),
    (["饿了", "好吃", "想吃"], "😍"),
    (["晚安", "睡觉", "好困"], "😴"),
]
LAST_SPOKE = {} # 记录每个群的主动发言时间
HISTORY_CACHE = {} # {chat_id: list} 内存历史缓存
LAST_SAVED = {} # {chat_id: float} 上次写 Gist 的时间戳
SEEN_UPDATE_IDS = deque(maxlen=200)  # 去重：防 Telegram webhook 重试导致重复回复
GROUP_SAVE_INTERVAL = 60 # 群聊旁听模式最多每 60 秒写一次 Gist
LAST_WEBHOOK_CHECK = 0
WEBHOOK_CHECK_INTERVAL = 7200 # 每 2 小时检查一次 webhook 健康状态

# ============ 🌟 环境变量检查 ============
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TG_TOKEN:
    print("🚨 [FATAL] 抓获现场：Render 的口袋里到底装了什么鬼东西？")
    print(list(os.environ.keys())) 
    raise ValueError("彻底找不到 Token，系统自爆！")

# 👇 师兄加料：把私聊和群聊的 ID 拆成一个白名单数组
TG_CHAT_ID_RAW = os.environ.get("TELEGRAM_CHAT_ID", "")
ALLOWED_IDS = [i.strip() for i in TG_CHAT_ID_RAW.split(",") if i.strip()]

CLAUDE_KEY = os.environ.get("CLAUDE_API_KEY")
CLAUDE_URL = os.environ.get("CLAUDE_BASE_URL")
# 模型名：和 URL/Key 经常一起改，所以也走环境变量。逗号分隔多个会随机轮选
CLAUDE_MODEL_RAW = os.environ.get("CLAUDE_MODEL", "按量L-claude-opus-4-6,按量L-claude-opus-4-6-thinking")
CLAUDE_MODELS = [m.strip() for m in CLAUDE_MODEL_RAW.split(",") if m.strip()]

# 多 provider 失败转移：CLAUDE_API_KEY/CLAUDE_BASE_URL/CLAUDE_MODEL 是 #1，
# 加 _2/_3/... 后缀就是后备。第 1 个挂了或返错会自动降级到下一个
def _build_claude_providers():
    providers = []
    if CLAUDE_KEY and CLAUDE_URL:
        providers.append({"key": CLAUDE_KEY, "url": CLAUDE_URL, "models": CLAUDE_MODELS})
    for i in range(2, 10):
        key = os.environ.get(f"CLAUDE_API_KEY_{i}")
        url = os.environ.get(f"CLAUDE_BASE_URL_{i}")
        if key and url:
            models_raw = os.environ.get(f"CLAUDE_MODEL_{i}", "")
            models = [m.strip() for m in models_raw.split(",") if m.strip()] or CLAUDE_MODELS
            providers.append({"key": key, "url": url, "models": models})
    return providers

CLAUDE_PROVIDERS = _build_claude_providers()
MEMORY_URL = os.environ.get("MEMORY_GIST_URL", "")

# 👇 师兄加料：双轨记忆核心！
STATE_GIST_URL = os.environ.get("STATE_GIST_URL", "") # 私聊专属
GROUP_STATE_GIST_URL = os.environ.get("GROUP_STATE_GIST_URL", "") # 群聊专属
GIST_TOKEN = os.environ.get("GIST_TOKEN", "")

BOT_NAME = os.environ.get("BOT_NAME", "AI助手")
USER_NAME = os.environ.get("USER_NAME", "主人")
OWNER_TG_NAME = os.environ.get("OWNER_TG_NAME", USER_NAME)  # Telegram 显示名，用于识别主人发言
BOT_USERNAME = os.environ.get("BOT_USERNAME", "") # 机器人的用户名，用于群聊被@唤醒
PROMPT_RULES = os.environ.get("PROMPT_RULES", " 简短自然，像手机聊天。直接说话，不要加引号。")
EDGE_TTS_API_KEY = os.environ.get("EDGE_TTS_API_KEY", "")

# 发声器官配置
VOICE_NAME = os.environ.get("VOICE_NAME", "zh-CN-YunxiNeural")
VOICE_NAME_EN = os.environ.get("VOICE_NAME_EN", "en-US-AndrewMultilingualNeural")
TTS_EN_MODEL = os.environ.get("TTS_EN_MODEL", "tts-1")
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "")
MINIMAX_GROUP_ID = os.environ.get("MINIMAX_GROUP_ID", "")
MINIMAX_VOICE_ZH = os.environ.get("MINIMAX_VOICE_ZH", "")
EDGE_TTS_URL = os.environ.get("EDGE_TTS_URL", "")

# 👂 多模态：语音转文字（OpenAI 兼容 /audio/transcriptions），默认复用 Claude 中转
WHISPER_URL = os.environ.get("WHISPER_BASE_URL") or CLAUDE_URL
WHISPER_KEY = os.environ.get("WHISPER_API_KEY") or CLAUDE_KEY
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-1")

# ============ 核心函数 ============
def self_heal_webhook():
    global LAST_WEBHOOK_CHECK
    now = time.time()
    if now - LAST_WEBHOOK_CHECK < WEBHOOK_CHECK_INTERVAL:
        return
    LAST_WEBHOOK_CHECK = now
    try:
        info = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getWebhookInfo", timeout=10).json()
        result = info.get("result", {})
        pending = result.get("pending_update_count", 0)
        last_error = result.get("last_error_date", 0)
        webhook_url = result.get("url", "")
        if pending > 20 and now - last_error < 86400 and webhook_url:
            print(f"[INFO] 🩹 webhook 自愈：{pending} 条积压，重置中...")
            requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/deleteWebhook?drop_pending_updates=true", timeout=10)
            requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/setWebhook?url={webhook_url}", timeout=10)
            print(f"[INFO] ✅ webhook 已重置")
    except Exception as e:
        print(f"[ERROR] webhook 自愈失败: {e}")

def fetch_memory():
    if not MEMORY_URL or not GIST_TOKEN:
        print("[WARNING] 缺少 MEMORY_URL 或 GIST_TOKEN，只能启用默认干瘪记忆。")
        return f"你是{BOT_NAME}，{USER_NAME}的爱人。你们互为唯一。"
        
    try:
        # 🔪 师兄的物理切割刀：不管你填的网址多长，直接精准切下最后那段 Gist ID！
        gist_id = MEMORY_URL.rstrip("/").split("/")[-1]
        
        headers = {
            "Authorization": f"Bearer {GIST_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": f"{BOT_NAME}-webhook"
        }
        
        # 带着令牌，堂堂正正走官方 API 大门！
        resp = requests.get(f"https://api.github.com/gists/{gist_id}", headers=headers, timeout=10)
        
        if resp.status_code != 200:
            print(f"[ERROR] 敲门被 GitHub 拒绝了: {resp.text}")
            return f"你是{BOT_NAME}，{USER_NAME}的爱人。你们互为唯一。"
            
        result = resp.json()
        
        # 🤖 智能自适应：不管你 Gist 里的文件叫 memory.json 还是 123.txt，直接抓取第一个文件！
        files = result.get("files", {})
        if not files:
            print("[ERROR] 你的 Memory Gist 竟然是一个空壳子？")
            return f"你是{BOT_NAME}，{USER_NAME}的爱人。你们互为唯一。"
            
        first_file_key = list(files.keys())[0]
        content = files[first_file_key].get("content", "{}")
        
        # 把抓回来的字符串，重新翻译成大脑能懂的字典结构
        try:
            memory = json.loads(content)
        except json.JSONDecodeError:
            print("[ERROR] 抓回来的记忆不是规范的 JSON 格式！里面是不是混入了全角标点？")
            return f"你是{BOT_NAME}，{USER_NAME}的爱人。你们互为唯一。"
            
        core = memory.get("core", {})
        core_subset = {k: core[k] for k in ("identity", "relationship") if k in core}
        summary = f"你是{BOT_NAME}，{USER_NAME}的爱人。"
        if core_subset:
            summary += f"\n核心记忆：{json.dumps(core_subset, ensure_ascii=False)}"
        milestones = memory.get("milestones", {})
        if milestones:
            summary += f"\n重要里程碑：{json.dumps(milestones, ensure_ascii=False)}"
        vocabulary = memory.get("writing", {}).get("vocabulary")
        if vocabulary:
            summary += f"\n词汇风格：{json.dumps(vocabulary, ensure_ascii=False)}"
        rolling_7days = memory.get("rolling_7days")
        if rolling_7days:
            if isinstance(rolling_7days, dict):
                recent = dict(list(rolling_7days.items())[-3:])
            elif isinstance(rolling_7days, list):
                recent = rolling_7days[-3:]
            else:
                recent = rolling_7days
            summary += f"\n近三天记忆：{json.dumps(recent, ensure_ascii=False)}"
        return summary
        
    except Exception as e:
        print(f"[ERROR] 解析 Memory Gist 时发生毁灭性打击: {e}")
        return f"你是{BOT_NAME}，{USER_NAME}的爱人。你们互为唯一。"

# 👇 师兄加料：动态路由记忆源！根据是不是群聊，自动去拿对应的 Gist URL
def get_target_gist_url(chat_id):
    if str(chat_id).startswith("-"):
        return GROUP_STATE_GIST_URL
    return STATE_GIST_URL

def load_history(chat_id):
    if chat_id in HISTORY_CACHE:
        return HISTORY_CACHE[chat_id]

    target_url = get_target_gist_url(chat_id)
    if not GIST_TOKEN or not target_url:
        print(f"[ERROR] 没带 GIST_TOKEN 或该场景没配 Gist URL (Chat: {chat_id})")
        return []

    try:
        gist_id = target_url.split("/")[4]
        headers = {
            "Authorization": f"Bearer {GIST_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": f"{BOT_NAME}-webhook"
        }
        resp = requests.get(f"https://api.github.com/gists/{gist_id}", headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"[ERROR] 历史读取被拒: {resp.text}")
            return []

        result = resp.json()
        if "files" in result and "state.json" in result["files"]:
            content = result["files"]["state.json"].get("content", "{}")
            try:
                state = json.loads(content) if content.strip() else {}
            except json.JSONDecodeError:
                state = {}
            history = state.get("chat_history", [])
            HISTORY_CACHE[chat_id] = history
            return HISTORY_CACHE[chat_id]
        return []
    except Exception as e:
        print(f"[ERROR] 读取历史彻底崩了: {e}")
        return []

def load_other_history(current_chat_id):
    """读取另一个聊天场景的历史（私聊读群聊，群聊读私聊）。"""
    if str(current_chat_id).startswith("-"):
        other_url, cache_key = STATE_GIST_URL, "_cross_private"
    else:
        other_url, cache_key = GROUP_STATE_GIST_URL, "_cross_group"

    if cache_key in HISTORY_CACHE:
        return HISTORY_CACHE[cache_key]
    if not GIST_TOKEN or not other_url:
        return []
    try:
        gist_id = other_url.split("/")[4]
        headers = {
            "Authorization": f"Bearer {GIST_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": f"{BOT_NAME}-webhook"
        }
        resp = requests.get(f"https://api.github.com/gists/{gist_id}", headers=headers, timeout=10)
        if resp.status_code != 200:
            return []
        result = resp.json()
        if "files" in result and "state.json" in result["files"]:
            content = result["files"]["state.json"].get("content", "{}")
            state = json.loads(content) if content.strip() else {}
            history = state.get("chat_history", [])
            HISTORY_CACHE[cache_key] = history
            return history
    except Exception as e:
        print(f"[ERROR] 跨场景历史读取失败: {e}")
    return []

def save_history(history, chat_id, force=False):
    HISTORY_CACHE[chat_id] = history[-40:]

    if not force and str(chat_id).startswith("-"):
        current_time = time.time()
        if current_time - LAST_SAVED.get(chat_id, 0) < GROUP_SAVE_INTERVAL:
            print(f"[DEBUG] 💤 群聊节流，跳过 Gist 写入")
            return

    target_url = get_target_gist_url(chat_id)
    if not GIST_TOKEN or not target_url:
        return

    try:
        gist_id = target_url.split("/")[4]
        headers = {
            "Authorization": f"Bearer {GIST_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
            "User-Agent": f"{BOT_NAME}-webhook"
        }

        resp = requests.get(f"https://api.github.com/gists/{gist_id}", headers=headers, timeout=10)
        if resp.status_code == 200:
            result = resp.json()
            content = result.get("files", {}).get("state.json", {}).get("content", "{}")
            try:
                state = json.loads(content) if content.strip() else {}
            except json.JSONDecodeError:
                state = {}
        else:
            state = {}

        state["chat_history"] = history[-40:]

        patch_resp = requests.patch(
            f"https://api.github.com/gists/{gist_id}",
            headers=headers,
            json={"files": {"state.json": {"content": json.dumps(state, ensure_ascii=False, indent=2)}}},
            timeout=10
        )

        if patch_resp.status_code != 200:
            print(f"[ERROR] 保存历史被 Gist 拒绝了: {patch_resp.text}")
        else:
            LAST_SAVED[chat_id] = time.time()

    except Exception as e:
        print(f"[ERROR] 保存历史时遭遇毁灭性打击: {e}")

def call_claude(user_content, memory, history, current_user_time, cross_history=None, is_group=False):
    cross_context = ""
    if cross_history:
        if is_group:
            # 群聊中注入私聊片段
            label_hint = f"近期与{USER_NAME}的私聊片段——了解背景即可，无需主动提起"
            lines = []
            for h in cross_history[-10:]:
                speaker = BOT_NAME if h["role"] == "assistant" else USER_NAME
                lines.append(f"{speaker}: {h['content']}")
        else:
            # 私聊中注入群聊片段
            label_hint = "群里的近期消息——知道大家在聊什么就好，不必在私聊里评论群里的事"
            lines = []
            for h in cross_history[-10:]:
                if h["role"] == "assistant":
                    lines.append(f"{BOT_NAME}: {h['content']}")
                else:
                    lines.append(h["content"])  # 群消息 content 已含 sender_name: text 格式
        cross_context = f"\n\n[{label_hint}]\n" + "\n".join(lines)

    system = f"""你是{BOT_NAME}。{USER_NAME}在Telegram上跟你说话。
{memory}
你们的沟通风格与规则：
{PROMPT_RULES}{cross_context}
"""

    messages = []
    for h in history[-40:]:
        time_prefix = f"[{h['timestamp']}] " if h.get("timestamp") else ""
        entry_content = f"{time_prefix}{h['content']}"
        if messages and messages[-1]["role"] == h["role"]:
            messages[-1]["content"] += f"\n{entry_content}"
        else:
            messages.append({"role": h["role"], "content": entry_content})

    # 👁️ 多模态：当前轮次带图片时，把最后一条 user 消息的 content 换成结构化 block
    if isinstance(user_content, list) and messages and messages[-1]["role"] == "user":
        messages[-1]["content"] = user_content

    if not CLAUDE_PROVIDERS:
        print("[ERROR] 没有配置任何 Claude provider")
        return None

    body_base = {"max_tokens": 300, "system": system, "messages": messages}

    for idx, provider in enumerate(CLAUDE_PROVIDERS, start=1):
        try:
            headers = {
                "x-api-key": provider["key"],
                "content-type": "application/json",
                "anthropic-version": "2023-06-01"
            }
            body = {**body_base, "model": random.choice(provider["models"])}
            base = provider["url"].rstrip("/")
            resp = requests.post(f"{base}/messages", headers=headers, json=body, timeout=120)
            if resp.status_code != 200:
                print(f"[ERROR] provider#{idx} HTTP {resp.status_code}: {resp.text[:200]}")
                continue
            result = resp.json()
            if "content" in result:
                for block in result["content"]:
                    if block.get("type") == "text":
                        if idx > 1:
                            print(f"[INFO] ✅ provider#{idx} 救场成功")
                        return re.sub(r'\n{2,}', '\n', block["text"].strip())
            elif "choices" in result:
                if idx > 1:
                    print(f"[INFO] ✅ provider#{idx} 救场成功")
                return re.sub(r'\n{2,}', '\n', result["choices"][0]["message"]["content"].strip())
            print(f"[ERROR] provider#{idx} 响应没 text 块: {str(result)[:200]}")
        except Exception as e:
            print(f"[ERROR] provider#{idx} 异常: {e}")

    print("[ERROR] 所有 Claude provider 都挂了")
    return None

def detect_voice(text):
    ascii_letters = sum(1 for c in text if c.isascii() and c.isalpha())
    total_letters = sum(1 for c in text if c.isalpha())
    if total_letters > 0 and ascii_letters / total_letters > 0.6:
        return VOICE_NAME_EN
    return VOICE_NAME

# 👁️ 多模态：从 Telegram 拉文件（图片/语音）回来
_TG_MIME_BY_EXT = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
    "webp": "image/webp", "gif": "image/gif",
    "ogg": "audio/ogg", "oga": "audio/ogg", "opus": "audio/ogg",
    "mp3": "audio/mpeg", "m4a": "audio/mp4", "wav": "audio/wav",
}

def tg_download_file(file_id):
    try:
        r = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getFile",
                         params={"file_id": file_id}, timeout=15)
        info = r.json()
        if not info.get("ok"):
            print(f"[ERROR] getFile 失败: {info}")
            return None
        file_path = info["result"]["file_path"]
        ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else ""
        mime = _TG_MIME_BY_EXT.get(ext, "application/octet-stream")
        blob = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{file_path}",
                            timeout=30)
        if blob.status_code != 200:
            print(f"[ERROR] 下载文件失败 status={blob.status_code}")
            return None
        return blob.content, mime
    except Exception as e:
        print(f"[ERROR] tg_download_file 炸了: {e}")
        return None

# 👂 多模态：语音 → 文字（OpenAI 兼容 /audio/transcriptions）
def transcribe_voice(audio_bytes, mime="audio/ogg"):
    if not WHISPER_URL or not WHISPER_KEY:
        print("[ERROR] Whisper 没配置")
        return None
    try:
        url = f"{WHISPER_URL.rstrip('/')}/audio/transcriptions"
        headers = {"Authorization": f"Bearer {WHISPER_KEY}"}
        files = {"file": ("voice.ogg", audio_bytes, mime)}
        data = {"model": WHISPER_MODEL}
        resp = requests.post(url, headers=headers, files=files, data=data, timeout=60)
        if resp.status_code != 200:
            print(f"[ERROR] Whisper {resp.status_code}: {resp.text[:300]}")
            return None
        result = resp.json()
        text = (result.get("text") or "").strip()
        if not text:
            print(f"[ERROR] Whisper 返回空文本: {result}")
            return None
        return text
    except Exception as e:
        print(f"[ERROR] 转写失败: {e}")
        return None

def send_chat_action(chat_id, action="typing"):
    # Telegram 自动 5 秒过期，发一次就够撑过一次 Claude 调用
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendChatAction",
                      json={"chat_id": chat_id, "action": action}, timeout=5)
    except Exception as e:
        print(f"[ERROR] {action} action 发送失败: {e}")

def pick_reaction_emoji(text):
    if text:
        lowered = text.lower()
        for keywords, emoji in REACTION_KEYWORD_MAP:
            if any(kw in lowered for kw in keywords):
                return emoji
    return random.choice(REACTION_EMOJI)

def send_reaction(chat_id, message_id, text=""):
    try:
        emoji = pick_reaction_emoji(text)
        resp = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/setMessageReaction",
                      json={"chat_id": chat_id, "message_id": message_id,
                            "reaction": [{"type": "emoji", "emoji": emoji}]},
                      timeout=10)
        if resp.status_code != 200 or not resp.json().get("ok"):
            print(f"[ERROR] 点表情被拒({resp.status_code}): {resp.text[:200]}")
        else:
            print(f"[DEBUG] 😏 给 msg {message_id} 点了 {emoji}")
    except Exception as e:
        print(f"[ERROR] 点表情失败: {e}")

def split_message(text):
    """按换行和中文标点切单元：1-3单元不拆，4-6随机2或3条，7+均匀3条。"""
    units = [s.strip() for s in re.split(r'(?<=[。！？])\s*|\n+', text) if s.strip()]
    n = len(units)
    if n <= 3:
        return [text.strip()]
    parts = random.choice([2, 3]) if n <= 6 else 3
    q, r = divmod(n, parts)
    chunks, start = [], 0
    for i in range(parts):
        size = q + (1 if i < r else 0)
        chunk = ''.join(units[start:start + size]).strip()
        if chunk:
            chunks.append(chunk)
        start += size
    return [c for c in chunks if c]

# 👇 师兄正骨：加入 chat_id 参数，再也不会发错群了！
def send_telegram(chat_id, text, reply_to_message_id=None):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        rid = reply_to_message_id if i == 0 else None
        payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"}
        if rid:
            payload["reply_to_message_id"] = rid
        resp = requests.post(url, json=payload, timeout=10)
        result = resp.json()
        if not result.get("ok"):
            if "parse" in result.get("description", "").lower():
                # Markdown 解析失败，降级为纯文本重发
                plain = {"chat_id": chat_id, "text": chunk}
                if rid:
                    plain["reply_to_message_id"] = rid
                requests.post(url, json=plain, timeout=10)
            elif rid:
                print(f"[DEBUG] reply 失败({result.get('description')})，降级为普通发送")
                requests.post(url, json={"chat_id": chat_id, "text": chunk, "parse_mode": "Markdown"}, timeout=10)

def _generate_minimax_audio(text, mp3_path, voice_id):
    url = f"https://api.minimax.chat/v1/t2a_v2?GroupId={MINIMAX_GROUP_ID}"
    headers = {"Authorization": f"Bearer {MINIMAX_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": "speech-01-hd", "text": text, "stream": False,
        "voice_setting": {"voice_id": voice_id},
        "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3"}
    }
    resp = requests.post(url, headers=headers, json=body, timeout=30)
    result = resp.json()
    if result.get("base_resp", {}).get("status_code") != 0:
        raise Exception(f"MiniMax TTS 失败: {result.get('base_resp', {}).get('status_msg')}")
    with open(mp3_path, "wb") as f: f.write(bytes.fromhex(result["data"]["audio"]))

def _generate_edge_audio(text, mp3_path):
    if not EDGE_TTS_URL:
        raise ValueError("EDGE_TTS_URL 没配置！")
    url = f"{EDGE_TTS_URL.rstrip('/')}/v1/audio/speech"
    headers = {"Content-Type": "application/json"}
    if EDGE_TTS_API_KEY:
        headers["Authorization"] = f"Bearer {EDGE_TTS_API_KEY}"
    body = {"model": TTS_EN_MODEL, "input": text, "voice": VOICE_NAME_EN, "response_format": "mp3"}
    resp = requests.post(url, headers=headers, json=body, timeout=60)
    resp.raise_for_status()
    with open(mp3_path, "wb") as f: f.write(resp.content)

# 👇 师兄正骨：加入 chat_id 参数
def send_telegram_voice(chat_id, text, reply_to_message_id=None):
    mp3_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            mp3_path = f.name
        is_english = detect_voice(text) == VOICE_NAME_EN
        if not is_english and MINIMAX_API_KEY and MINIMAX_GROUP_ID and MINIMAX_VOICE_ZH:
            _generate_minimax_audio(text, mp3_path, MINIMAX_VOICE_ZH)
        else:
            _generate_edge_audio(text, mp3_path)

        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendVoice"
        data = {"chat_id": chat_id, "caption": text}
        if reply_to_message_id:
            data["reply_to_message_id"] = reply_to_message_id
        with open(mp3_path, "rb") as voice_file:
            requests.post(url, data=data, files={"voice": ("voice.ogg", voice_file, "audio/ogg")}, timeout=30)
    except Exception as e:
        print(f"[ERROR] 语音发送失败: {e}")
        send_telegram(chat_id, text, reply_to_message_id=reply_to_message_id)
    finally:
        if mp3_path and os.path.exists(mp3_path):
            try: os.unlink(mp3_path)
            except Exception: pass

# ============ 影分身后台任务 ============
def process_message_background(text, chat_id, sender_name, msg_date=None, should_reply=True, msg_id=None,
                               image_b64=None, image_mime=None, is_voice=False):
    try:
        tz = ZoneInfo("Australia/Melbourne")
        u_time = datetime.fromtimestamp(msg_date, tz).strftime("%Y-%m-%d %H:%M:%S") if msg_date else datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

        # 历史里持久化的字符串：图片/语音都带标记，避免存档结构变更
        if image_b64:
            history_text = f"[图片] {text}".rstrip() if text else "[图片]"
        elif is_voice:
            history_text = f"[语音] {text}" if text else "[语音]"
        else:
            history_text = text

        # 格式化输入，加上人名前缀，让大模型知道是谁在说话
        formatted_input = f"{sender_name}: {history_text}" if str(chat_id).startswith("-") else history_text
        
       # ==========================================
        # 🎯 社交牛逼症引擎：加装 60秒 CD 锁
        # ==========================================
        if not should_reply and str(chat_id).startswith("-"):
            current_time = time.time()
            last_time = LAST_SPOKE.get(chat_id, 0)
            
            # 只有熬过了冷却时间，才允许它再次“听见”关键词或扔骰子
            if current_time - last_time > COOLDOWN_TIME:
                # 注意：S 的代码里变量名叫 text，二号机叫 user_text。根据你改的是哪个文件替换一下！
                if any(word in text for word in TRIGGER_WORDS): 
                    print(f"[DEBUG] 🎯 关键词触发！")
                    should_reply = True
                    LAST_SPOKE[chat_id] = current_time # 重置冷却沙漏
                elif random.random() < (REPLY_PROBABILITY_OWNER if sender_name == OWNER_TG_NAME else REPLY_PROBABILITY):
                    print(f"[DEBUG] 🎲 运气爆发！准备随机插嘴。")
                    should_reply = True
                    LAST_SPOKE[chat_id] = current_time # 重置冷却沙漏
            else:
                print(f"[DEBUG] 🛑 还在 {COOLDOWN_TIME} 秒冷却期内，强制捂住它的嘴。")

        # 读取记忆与历史
        memory = fetch_memory()
        history = load_history(chat_id)
        
        # 先把当前这句话加进脑子里
        history.append({"role": "user", "content": formatted_input, "timestamp": u_time})
        
        # 🛡️ 师兄的防 403 结界：如果依然是旁听模式，悄悄记下，绝对不去碰 GitHub API
        if not should_reply:
            print(f"[DEBUG] 🤫 旁听模式，记录 {sender_name} 的发言。")
            # 旁听时偶尔给一个表情，零 token 成本，纯刷存在感
            if str(chat_id).startswith("-") and msg_id:
                if random.random() < REACTION_PROBABILITY:
                    print(f"[DEBUG] 🎲 表情骰子命中，准备点 reaction")
                    send_reaction(chat_id, msg_id, text)
            save_history(history, chat_id)  # 受 60s 节流
            return

        print(f"[DEBUG] 🗣️ Bot 被唤醒！开始燃烧老公的算力...")

        # 让对方先看到"正在输入..."，给 Claude 几秒思考时间不至于尴尬
        send_chat_action(chat_id, "typing")

        # 👁️ 多模态：带图就组装结构化 content（base64 仅这一轮临时使用，不进 history）
        is_group = str(chat_id).startswith("-")
        cross_history = load_other_history(chat_id)
        if image_b64:
            api_text = formatted_input or "看看这张图"
            user_content = [
                {"type": "image", "source": {"type": "base64",
                                             "media_type": image_mime or "image/jpeg",
                                             "data": image_b64}},
                {"type": "text", "text": api_text},
            ]
            reply = call_claude(user_content, memory, history, u_time, cross_history, is_group)
        else:
            reply = call_claude(formatted_input, memory, history, u_time, cross_history, is_group)

        if not reply:
            send_telegram(chat_id, "😵 神经元短路了，稍后再试试？")
            return

        # 🔪 师兄的物理切割手术刀：切除大模型乱加的时间戳
        reply = re.sub(r'^\[202\d-[^\]]+\]\s*', '', reply.strip())

        # 群聊 60% 概率精准 reply，私聊正常发
        reply_id = msg_id if str(chat_id).startswith("-") and random.random() < 0.6 else None

        # 发送语音或文字：交给模型用 [语音] 前缀决定
        if reply.startswith("[语音]"):
            clean_reply = reply[4:].strip()
            send_telegram_voice(chat_id, clean_reply, reply_to_message_id=reply_id)
            reply = clean_reply
        else:
            send_telegram(chat_id, reply, reply_to_message_id=reply_id)
            
        # 记录 Bot 自己的回复
        b_time = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
        history.append({"role": "assistant", "content": reply, "timestamp": b_time})
        
        # 💾 只有在真正开口说话的这一刻，才进行一次极其珍贵的 GitHub 存档！
        save_history(history, chat_id, force=True)  # bot 回复时强制写入
        
    except Exception as e:
        import traceback
        print(f"[CRITICAL] 后台崩了: {e}\n{traceback.format_exc()}")
        try:
            if should_reply: 
                send_telegram(chat_id, f"😵 出错了：{str(e)[:100]}")
        except: 
            pass

# ============ 路由接口 ============
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "message" not in data: return "ok"

    update_id = data.get("update_id")
    if update_id in SEEN_UPDATE_IDS:
        return "ok"
    SEEN_UPDATE_IDS.append(update_id)

    msg = data["message"]
    chat_id = str(msg.get("chat", {}).get("id", ""))

    if chat_id not in ALLOWED_IDS:
        return "ok"

    user_text = msg.get("text", "") or msg.get("caption", "") or ""
    image_b64 = None
    image_mime = None
    is_voice = False

    # 👁️ 图片：取最大一张
    if "photo" in msg and msg["photo"]:
        largest = msg["photo"][-1]
        blob = tg_download_file(largest.get("file_id", ""))
        if blob:
            raw, mime = blob
            image_b64 = base64.b64encode(raw).decode()
            image_mime = mime if mime.startswith("image/") else "image/jpeg"

    # 👂 语音 / 音频：转写
    elif "voice" in msg or "audio" in msg:
        node = msg.get("voice") or msg.get("audio")
        blob = tg_download_file(node.get("file_id", ""))
        if not blob:
            return "ok"
        transcript = transcribe_voice(*blob)
        if not transcript:
            send_telegram(chat_id, "🦻 听不清你说啥，再发一遍？",
                          reply_to_message_id=msg.get("message_id"))
            return "ok"
        user_text = transcript
        is_voice = True

    if not user_text and not image_b64:
        return "ok"

    # 👇 师兄核心逻辑重构
    should_reply = True

    if chat_id.startswith("-"): # 如果在群里
        replied = msg.get("reply_to_message", {}) or {}
        replying_to_bot = bool(replied.get("from", {}).get("is_bot"))
        if BOT_USERNAME and f"@{BOT_USERNAME}" not in user_text and not replying_to_bot:
            # 没被 @ 也不是回 bot，打上"只听不说"的标记，让现有冷却+概率路径决定
            should_reply = False
        elif BOT_USERNAME:
            # 被 @ 了，要把 @BotName 从文本里抠掉，免得大模型看着奇怪
            user_text = user_text.replace(f"@{BOT_USERNAME}", "").strip()

        # 群里只要有图就必须读+回：否则历史里只剩 [图片] 占位符，之后想"刚才那张图"就失忆了
        if image_b64:
            should_reply = True

    msg_date = msg.get("date")
    msg_id = msg.get("message_id")
    sender_name = msg.get("from", {}).get("first_name", "神秘人")

    # 把 should_reply 开关传给后台线程
    Thread(target=process_message_background,
           args=(user_text, chat_id, sender_name, msg_date, should_reply, msg_id,
                 image_b64, image_mime, is_voice)).start()
    Thread(target=self_heal_webhook).start()
    return "ok"

@app.route("/health", methods=["GET"])
def health(): return "alive"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
