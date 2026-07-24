#!/usr/bin/env python3
"""
summarize_feed.py

每天由 GitHub Actions 定時執行：
1. 讀取 JSON 檔
2. 使用 trafilatura 對沒有 summary 的項目抓取頁面正文
3. 從正文挑出資訊密度最高的原句，按原文順序拼接
   - 繁體中文原文 → 直接使用
   - 簡體中文原文 → OpenCC 規則式轉換為台灣繁體（非模型）
   - 其他語言     → 先抽取原句，再經 Google 翻譯網頁端點譯為繁體中文
                    （傳統 NMT 翻譯服務，免費、無需 API key、非 LLM；
                     可用 TRANSLATE=off 關閉，關閉時外語摘要保留原文）
4. 寫回 "summary" 欄位

正文不可得而退回 meta description 時，摘要尾端標記 ↛。

環境變數：
  ITEMS_FILE     JSON 檔路徑，預設 archive.json          （--items-file）
  MAX_ITEMS      單次最多處理幾筆，預設 50             （--max-items）
  TRANSLATE      "on"（預設）/ "off"：是否翻譯外語摘要 （--translate/--no-translate）
  SUMMARY_RATIO  摘要預算 = 正文長度 × 此比例，預設 0.9（--summary-ratio）
  SUMMARY_MAX    預算上限，預設 60000                  （--summary-max）
  RESCORE_ALL    "1" 時全量重算資訊價值      （--rescore-all）
"""

import json
import math
import os
import re
import sys
import time
from collections import Counter

import requests
import trafilatura
try:
    import translate as _tr
except ModuleNotFoundError:
    _tr = None

ITEMS_FILE = os.environ.get("ITEMS_FILE", "archive.json")
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "50"))
TRANSLATE = os.environ.get("TRANSLATE", "on").lower() != "off"
RESCORE_ALL = os.environ.get("RESCORE_ALL", "").lower() in ("1", "true", "yes", "on")

# 摘要長度採「與正文長度成比例」的動態預算：
# budget = clamp( 正文字數 × SUMMARY_RATIO, 1, SUMMARY_MAX )
# 預設 RATIO=0.9。啟動 MMR 冗餘剔除與語篇修剪。
SUMMARY_RATIO = float(os.environ.get("SUMMARY_RATIO", "0.9"))
SUMMARY_MAX = int(os.environ.get("SUMMARY_MAX", "60000"))
FOREIGN_BUDGET_RATIO = 1.2                                       # [tune]

# ---- 句子評分權重（extractive_summary）------------------------------------
ENTITY_WEIGHT_BASE = 1.2      # [tune] 含實體句的基礎加乘
ENTITY_WEIGHT_STEP = 0.08     # [tune] 每多一個實體再加乘（封頂 5 個）
ENTITY_WEIGHT_CAP = 5         # [tune]
FLUFF_PENALTY = 0.25          # [tune] 無實體修辭句的分數折損
LEAD_BIAS = 0.5               # [tune] 文章前段句子的位置加權上限
# ---- MMR 選句 -------------------------------------------------------------
MMR_LAMBDA = 0.7              # [tune] 冗餘懲罰強度
MMR_DUP_THRESHOLD = 0.65      # [tune] 相似度 ≥ 此值的句子直接淘汰

# ---- 資訊價值量化 ---------------------------------------------------------
INFO_VALUE_SATURATION = 4.0   # [tune] 每 100 單位長度幾個事實載體算「飽和(=100分)」
NOVELTY_DUP_COVERAGE = 0.7    # [tune] 事實被覆蓋比例 ≥ 此值 → 標記為轉載

# ---- 抓取／連線 -----------------------------------------------------------
FETCH_TIMEOUT = 30            # [tune] 單次請求逾時（秒）
FETCH_RETRIES = 3            # [tune] 暫時性錯誤（429/5xx/連線）自動重試次數
SLEEP_BETWEEN_ITEMS = 1.5     # [tune] 項目間禮貌延遲（秒）
FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

# ---- 標記與偵測 -----------------------------------------------------------
FALLBACK_MARK = "↛"
# 站方封鎖時寫入的佔位摘要（填入 summary 欄位以停止每日重試）
BLOCKED_SUMMARY = "無法取得頁面內容（來源網站封鎖自動化存取）" + FALLBACK_MARK
# 防爬驗證頁特徵（僅在正文近乎空白時比對，避免誤傷談論這些主題的正常文章）
CHALLENGE_PATTERN = re.compile(
    r"安全验证|安全驗證|验证码|驗證碼|禁止访问|禁止訪問|访问异常|異常流量|异常流量|"
    r"Just a moment|Checking your browser|Verify you are human|"
    r"[Ee]nable JavaScript and cookies|Access [Dd]enied|cf-challenge"
)

# 追蹤參數：正規化 URL 時剝除，避免同一頁的不同追蹤連結被當成不同項目
TRACKING_PARAM_EXACT = {
    "ref", "spm", "fbclid", "gclid", "igshid", "mkt_tok",
    "mc_cid", "mc_eid", "_hsenc", "_hsmi",
}

# 廠商別名／型號正規化：讓「Claude / Anthropic / claude-opus」等指向同一實體，
# 使跨篇轉載偵測不因寫法差異而漏判。非 AI 領域可自行增刪。
VENDOR_ALIASES = {
    "openai": "openai", "chatgpt": "openai",
    "anthropic": "anthropic", "claude": "anthropic",
    "google": "google", "deepmind": "google", "gemini": "google",
    "microsoft": "microsoft", "copilot": "microsoft",
    "github": "github", "huggingface": "huggingface", "hugging face": "huggingface",
    "meta": "meta", "llama": "meta",
    "deepseek": "deepseek", "mistral": "mistral",
    "xai": "xai", "grok": "xai",
    "nvidia": "nvidia", "spacex": "spacex",
}
MODEL_RE = re.compile(
    r"(?i)\b("
    r"gpt[-\s]?\d+(?:\.\d+)?[a-z]*|"
    r"claude(?:[-\s]?(?:opus|sonnet|haiku))?[-\s]?\d+(?:\.\d+)?|"
    r"gemini[-\s]?\d+(?:\.\d+)?|"
    r"llama[-\s]?\d+(?:\.\d+)?|"
    r"deepseek[-\s]?[a-z0-9.]+|"
    r"grok[-\s]?\d+(?:\.\d+)?|"
    r"mistral[-\s]?[a-z0-9.]+"
    r")\b"
)

# 中文（zh-TW）字元密度較高：外語原文先抽較長的量，翻譯後長度大致落在上限內。
# FOREIGN_BUDGET_RATIO 已於上方集中設定區定義（實測校準 1.10~1.11，設 1.2 留餘裕）。


def _to_twp(text: str) -> str:
    return _tr.to_traditional(text) if _tr else text


# ---------------------------------------------------------------- 連線／URL／編碼

_SESSION: requests.Session | None = None


def get_session() -> requests.Session:
    """單例 requests.Session：連線池重用 + 自動重試（429/5xx/連線錯誤退避）。
    取代逐篇 requests.get，對同站多篇抓取有速度增益，翻譯端點亦重用同一 session。"""
    global _SESSION
    if _SESSION is None:
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        s = requests.Session()
        retry = Retry(
            total=FETCH_RETRIES, connect=FETCH_RETRIES, read=FETCH_RETRIES,
            backoff_factor=0.8,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=frozenset(["GET", "POST"]),
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=16, pool_maxsize=16)
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        s.headers.update(FETCH_HEADERS)
        _SESSION = s
    return _SESSION


def normalize_url(raw_url: str) -> str:
    """剝除追蹤參數（utm_*、fbclid、gclid、spm、ref…）並正規化，
    使同一頁面的不同追蹤連結收斂成同一個 key，提升去重與轉載偵測準確度。"""
    from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
    try:
        parsed = urlparse((raw_url or "").strip())
        if not parsed.scheme:
            return (raw_url or "").strip()
        query = [
            (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAM_EXACT
        ]
        parsed = parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            fragment="",
            query=urlencode(query, doseq=True),
        )
        return urlunparse(parsed).rstrip("/")
    except Exception:
        return (raw_url or "").strip()


def maybe_fix_mojibake(text: str) -> str:
    """修復「UTF-8 位元組被 Latin-1/CP1252 誤解碼」的亂碼（RSS/網頁常見）。
    僅在偵測到亂碼特徵時才嘗試，避免動到正常文字。"""
    s = (text or "").strip()
    if not s or re.search(r"[Ãâåèæïð]|[\x80-\x9f]|æ|ç|å|é", s) is None:
        return s
    for enc in ("latin1", "cp1252"):
        try:
            fixed = s.encode(enc).decode("utf-8")
            if fixed and fixed != s:
                return fixed
        except Exception:
            continue
    return s


# ---------------------------------------------------------------- 抓取

def extract_meta_description(html: str) -> str:
    patterns = [
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+content=["\'](.*?)["\'][^>]+property=["\']og:description["\']',
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']description["\']',
        r'<meta[^>]+name=["\']twitter:description["\'][^>]+content=["\'](.*?)["\']',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        if m and m.group(1).strip():
            return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""


def fetch_content(url: str):
    """回傳 (text, source_type): "body" / "meta" / (None, "blocked") / (None, None)

    - "body"    : 正文抽取成功
    - "meta"    : 正文不可得，退回 meta description（摘要會標 ↛）
    - "blocked" : 站方永久性封鎖自動抓取（403/418/451 或防爬驗證頁），
                  呼叫端應寫入 ↛ 佔位摘要，避免每天無限重試
    - None,None : 暫時性失敗（逾時、5xxs），保留 pending 下次再試
    """
    BLOCK_STATUS = {401, 403, 407, 418, 451}
    session = get_session()
    try:
        # 429/5xx/連線錯誤的退避重試由 session 的 Retry adapter 處理
        resp = session.get(url, timeout=FETCH_TIMEOUT)
        if resp.status_code in BLOCK_STATUS:
            print(f"    blocked by site (HTTP {resp.status_code})")
            return None, "blocked"
        resp.raise_for_status()
        html = resp.text
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        if code in BLOCK_STATUS:
            print(f"    blocked by site (HTTP {code})")
            return None, "blocked"
        print(f"    fetch failed: HTTP {code}")
        return None, None
    except Exception as e:
        print(f"    fetch failed: {e}")
        return None, None

    body = trafilatura.extract(
        html, include_comments=False, include_tables=True, favor_recall=True
    )
    body = maybe_fix_mojibake(body.strip()) if body else ""
    meta = maybe_fix_mojibake(extract_meta_description(html))

    if len(body) < 200 and CHALLENGE_PATTERN.search(html[:20000]):
        print("    blocked by site (challenge page)")
        return None, "blocked"

    if len(body) >= 200:
        return body, "body"
    if body and re.search(r"[。！？.!?]", body) and len(body) >= max(80, len(meta)):
        return body, "body"

    if meta:
        return meta, "meta"
    if body:
        return body, "meta"
    return None, None


# ---------------------------------------------------------------- 語言偵測

def cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    letters = sum(1 for ch in text if ch.isalpha() or "\u4e00" <= ch <= "\u9fff")
    return cjk / letters if letters else 0.0


def detect_lang(text: str) -> str:
    if cjk_ratio(text) < 0.25:
        return "other"
    variant = _tr.detect_variant(text) if _tr else "hant"
    return "zh-hant" if variant == "hant" else "zh-hans"


# ---------------------------------------------------------------- 摘要

ZH_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*")
EN_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\u00c0-\u024f\"'(])")

EN_STOPWORDS = frozenset("""
a an the and or but if then than that this these those of in on at to for from
with by as is are was were be been being it its it's he she they them his her
their we you your i not no so do does did done have has had will would can
could should may might must about into over under between after before during
what which who whom whose when where why how all any both each few more most
other some such only own same very s t just don now also there here out up
""".split())


def split_sentences(text: str, is_cjk: bool):
    text = re.sub(r"\n{2,}", "\n", text)
    parts = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        segs = ZH_SPLIT.split(line) if is_cjk else EN_SPLIT.split(line)
        parts.extend(s.strip() for s in segs if s.strip())
    out = []
    for s in parts:
        n = len(s) if is_cjk else len(s.split())
        if (is_cjk and n >= 8) or (not is_cjk and n >= 5):
            out.append(s)
    return out


def tokenize(sentence: str, is_cjk: bool):
    if is_cjk:
        chars = re.sub(r"[^\u4e00-\u9fff0-9A-Za-z]", "", sentence)
        # 以中文字元 bigram 作為詞單位（免斷詞器）
        toks = [chars[i:i + 2] for i in range(len(chars) - 1)]
        toks += re.findall(r"[0-9]+(?:\.[0-9]+)?%?", sentence)
        return toks
    toks = re.findall(r"[a-zA-Z][a-zA-Z'-]+|[0-9]+(?:\.[0-9]+)?%?", sentence.lower())
    return [t for t in toks if t not in EN_STOPWORDS]


NUM_PATTERN = re.compile(r"[0-9０-９][0-9０-９,.:%０-９]*|[一二三四五六七八九十百千萬億兆]{2,}")
# 實體訊號：連續大寫詞（人名/機構）、《》「」『』引號內容、百分比
ENTITY_PATTERN = re.compile(
    r"[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)+|《[^》]{1,30}》|「[^」]{1,20}」|『[^』]{1,20}』|[0-9.]+\s*(?:%|per cent|percent)"
)
# 語篇標記修剪：只刪句首連接詞，句子主體原封不動（不構成改寫）
DISCOURSE_ZH = re.compile(
    r"^(然而|不過|此外|另外|同時|當然|其實|事實上|值得一提的是|換句話說|"
    r"也就是說|總而言之|總的來說|不僅如此|除此之外|与此同时|与此同時|"
    r"然后|然後|接著|接着|因此|所以|而且|并且|並且|首先|其次|最後|最后|再者)[，,、]"
)
DISCOURSE_EN = re.compile(
    r"^(However|Moreover|Furthermore|In addition|Additionally|Of course|"
    r"In fact|Indeed|Meanwhile|Nevertheless|Nonetheless|Besides|"
    r"That said|To be sure|As a result|Therefore|Thus|So|Also|And|But|Yet),?\s+",
    re.IGNORECASE,
)
PRONOUN_ONLY_ZH = re.compile(r"^[我你他她它我們你們他們這那些的了是也都很就會能不沒有和與跟得地嗎呢吧啊，。！？\s]+$")


def trim_discourse(s: str, is_cjk: bool) -> str:
    pat = DISCOURSE_ZH if is_cjk else DISCOURSE_EN
    prev = None
    while prev != s:
        prev = s
        s = pat.sub("", s).lstrip()
    return s


def entity_count(s: str) -> int:
    return len(ENTITY_PATTERN.findall(s)) + len(NUM_PATTERN.findall(s))


def is_fluff(s: str, is_cjk: bool) -> bool:
    """無實體的修辭問句／純代詞感嘆句 → 資訊量趨近於零"""
    if entity_count(s) > 0:
        return False
    if s.rstrip().endswith(("？", "?")):
        return True
    if is_cjk and PRONOUN_ONLY_ZH.match(s):
        return True
    return False


def extractive_summary(text: str, is_cjk: bool, char_budget: int) -> str:
    sents = split_sentences(text, is_cjk)
    if not sents:
        return text[:char_budget]

    tok_sets = [set(tokenize(s, is_cjk)) for s in sents]
    freq = Counter()          # 詞在全文出現次數（詞頻 TF）
    doc_freq = Counter()      # 詞出現在幾個句子中（文件頻率 DF）
    for ts in tok_sets:
        freq.update(ts)
        for t in ts:
            doc_freq[t] += 1
    n_sents = len(sents)

    scored = []
    for idx, s in enumerate(sents):
        ts = tok_sets[idx]
        if not ts:
            continue
        # TF-IDF 風格評分：sqrt(TF) 抑制主題詞灌水，log(1+N/DF) 獎勵稀有詞。
        # 專有名詞與數字這類「事實載體」通常全文只出現一兩次，DF 低、IDF 高，
        # 因此扛事實的句子分數會被系統性拉高——直接針對資訊密度而非表面熱度。
        base = sum(
            math.sqrt(freq[t]) * math.log(1.0 + n_sents / doc_freq[t]) for t in ts
        ) / (len(ts) ** 0.5)
        ents = entity_count(s)
        if ents:
            base *= ENTITY_WEIGHT_BASE + min(ents, ENTITY_WEIGHT_CAP) * ENTITY_WEIGHT_STEP
        if is_fluff(s, is_cjk):
            base *= FLUFF_PENALTY
        base *= 1.0 + LEAD_BIAS * max(0.0, 1.0 - idx / max(len(sents), 1))
        scored.append([base, idx, s, ts])

    if not scored:
        return sents[0][:char_budget]

    # MMR 貪婪選句：每輪選「基礎分 − λ×與已選內容的最大重疊」最高者
    # max_sim 採增量更新：每加入一句，只需拿新句與所有剩餘句比對一次，
    # 整體 O(n²)，6 萬字（約 600 句）內在一秒級完成。
    LAMBDA = MMR_LAMBDA
    DUP_THRESHOLD = MMR_DUP_THRESHOLD
    remaining = scored[:]          # 每項: [base, idx, s, ts]
    max_sim = [0.0] * len(remaining)
    chosen, used = [], 0
    max_base = max(x[0] for x in scored) or 1.0

    while remaining:
        best_i, best_val = -1, float("-inf")
        for i, (base, idx, s, ts) in enumerate(remaining):
            if max_sim[i] >= DUP_THRESHOLD:
                continue
            cost = len(trim_discourse(s, is_cjk)) + 1
            if used + cost > char_budget:
                continue
            val = base / max_base - LAMBDA * max_sim[i]
            if val > best_val:
                best_val, best_i = val, i
        if best_i < 0:
            break
        base, idx, s, ts = remaining.pop(best_i)
        max_sim.pop(best_i)
        chosen.append((idx, trim_discourse(s, is_cjk)))
        used += len(chosen[-1][1]) + 1
        if used >= char_budget * 0.97:
            break
        # 增量更新剩餘句與已選內容的最大相似度
        for i, (_, _, _, rts) in enumerate(remaining):
            union = rts | ts
            if union:
                sim = len(rts & ts) / len(union)
                if sim > max_sim[i]:
                    max_sim[i] = sim

    if not chosen:
        return sents[0][:char_budget]

    chosen.sort(key=lambda x: x[0])
    joiner = "" if is_cjk else " "
    return joiner.join(s for _, s in chosen)


# ---------------------------------------------------------------- NMT 翻譯

def translate_to_zhtw(text: str) -> str | None:
    if _tr is None:
        return None
    try:
        return _tr.translate(text, target="zh-TW", source="auto", session=get_session())
    except Exception as e:
        print(f"    translate failed: {e}")
        return None


# ---------------------------------------------------------------- 組裝

def summary_budget(content_len: int) -> int:
    return max(1, min(SUMMARY_MAX, int(content_len * SUMMARY_RATIO)))


def build_summary(content: str, source_type: str) -> str:
    content = re.sub(r"\((?:\d{1,2}:)?\d{1,2}:\d{2}\)\s*[:：]?", ": ", content)
    lang = detect_lang(content)
    budget = summary_budget(len(content))
    marks = []

    if lang == "zh-hant":
        summary = extractive_summary(content, True, budget)
    elif lang == "zh-hans":
        summary = _to_twp(extractive_summary(content, True, budget))
    else:
        raw = extractive_summary(
            content, False, int(budget * FOREIGN_BUDGET_RATIO)
        )
        summary = None
        if TRANSLATE:
            translated = translate_to_zhtw(raw)
            if translated:
                summary = _to_twp(translated)
        if summary is None:
            summary = raw

    if source_type == "meta":
        marks.append(FALLBACK_MARK)

    if detect_lang(summary) == "other":
        summary = re.sub(r"\s+", " ", summary).strip()
    else:
        summary = re.sub(
            r"(?<=[\u4e00-\u9fff，。！？；：、（）「」])\s+|\s+(?=[\u4e00-\u9fff，。！？；：、（）「」])",
            "", summary)
        summary = re.sub(r"\s+", " ", summary).strip()

    suffix = (" " + " ".join(marks)) if marks else ""
    limit = budget - len(suffix)
    if len(summary) > limit:
        summary = summary[:limit].rstrip()
    return summary + suffix


# ---------------------------------------------------------------- 資訊價值量化

# 分數欄位；一旦某項目已具備 SCORE_MARK 欄位即視為「已計算」，不再重算
SCORE_MARK = "novelty"


def fact_tokens(text: str):
    """語言中立的事實載體集合：實體 (專名/引號) + 數字/金額/百分比。"""
    toks = set()
    low = (text or "").lower()
    # 廠商別名 → 正規實體
    for alias, canonical in VENDOR_ALIASES.items():
        if alias in low:
            toks.add(f"@{canonical}")     # 前綴避免與一般 token 撞名
    # 型號正規化（空白轉連字號）
    for m in MODEL_RE.finditer(text or ""):
        toks.add("#" + re.sub(r"\s+", "-", m.group(1).lower()))
    for m in ENTITY_PATTERN.findall(text or ""):
        toks.add(re.sub(r"\s+", " ", m).strip().lower())
    for m in NUM_PATTERN.findall(text or ""):
        t = m.strip()
        if re.fullmatch(r"\d{1,3}", t):
            continue
        toks.add(t.lower())
    return toks


def info_value_self(text: str) -> float:
    """單篇自身資訊密度（0–100）：每單位長度的事實載體濃度。與語料無關，可獨立快取。"""
    is_cjk = detect_lang(text) != "other"
    sents = split_sentences(text, is_cjk)
    unit = len(text) if is_cjk else len(text.split())
    if not sents or not unit:
        return 0.0
    facts = sum(entity_count(x) for x in sents)
    return min(100.0 * facts / unit / INFO_VALUE_SATURATION, 1.0) * 100.0


def score_corpus_novelty(items):
    """對缺少分數的項目計算跨篇新穎性；已計算過的項目凍結不動，
    但其事實仍納入語料基準，使新進轉載稿能被對照出來。

    寫入欄位：info_value（單篇密度）、novelty（跨篇新穎度 0–100）、
    unique_facts（全語料獨有事實數）、duplicate_of（疑似轉載來源的 url，否則 null）、
    value_adjusted（= info_value ×（1 − 被覆蓋度），語料調整後總價值）。
    """
    scored = [it for it in items if isinstance(it, dict) and it.get("summary")]
    if not scored:
        return 0

    ft = {id(it): fact_tokens(it["summary"]) for it in scored}
    corpus_df = Counter()
    for it in scored:
        for tok in ft[id(it)]:
            corpus_df[tok] += 1
    N = len(scored)
    maxnov = math.log(N) if N > 1 else 1.0

    newly = 0
    for it in scored:
        if SCORE_MARK in it and not RESCORE_ALL:
            continue
        toks = ft[id(it)]
        if not toks:
            it["info_value"] = round(info_value_self(it["summary"]), 1)
            it["novelty"] = 0.0
            it["unique_facts"] = 0
            it["duplicate_of"] = None
            it["value_adjusted"] = 0.0
            newly += 1
            continue
        novelty = sum(math.log(N / corpus_df[t]) for t in toks) / len(toks)
        unique = sum(1 for t in toks if corpus_df[t] == 1)
        cov, twin = 0.0, None
        for other in scored:
            if other is it:
                continue
            ot = ft[id(other)]
            if not ot:
                continue
            c = len(toks & ot) / len(toks)
            if c > cov:
                cov, twin = c, other
        base = info_value_self(it["summary"])
        it["info_value"] = round(base, 1)
        it["novelty"] = round(100.0 * novelty / maxnov, 1) if maxnov else 0.0
        it["unique_facts"] = unique
        it["duplicate_of"] = twin.get("url") if (twin and cov > NOVELTY_DUP_COVERAGE) else None
        it["value_adjusted"] = round(base * (1.0 - cov), 1)
        newly += 1
    return newly


# ---------------------------------------------------------------- 主流程

def build_arg_parser():
    import argparse
    p = argparse.ArgumentParser(description="Summary")
    p.add_argument("--items-file", default=ITEMS_FILE,
                   help="JSON file path")
    p.add_argument("--max-items", type=int, default=MAX_ITEMS)
    p.add_argument("--summary-ratio", type=float, default=SUMMARY_RATIO)
    p.add_argument("--summary-max", type=int, default=SUMMARY_MAX)
    p.add_argument("--translate", dest="translate", action="store_true", default=TRANSLATE)
    p.add_argument("--no-translate", dest="translate", action="store_false")
    p.add_argument("--rescore-all", action="store_true", default=RESCORE_ALL)
    return p


def load_items(path: str):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"], data
    return None, None


def save_items(path: str, items: list, wrapper: dict | None) -> None:
    with open(path, "w", encoding="utf-8") as f:
        if wrapper is not None:
            wrapper["items"] = items
            wrapper["total_items"] = len(items)
            json.dump(wrapper, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(items, f, ensure_ascii=False, indent=2)
        f.write("\n")


def main(argv=None) -> int:
    global ITEMS_FILE, MAX_ITEMS, TRANSLATE, SUMMARY_RATIO, SUMMARY_MAX, RESCORE_ALL
    args = build_arg_parser().parse_args(argv)
    ITEMS_FILE = args.items_file
    MAX_ITEMS = args.max_items
    TRANSLATE = args.translate
    SUMMARY_RATIO = args.summary_ratio
    SUMMARY_MAX = args.summary_max
    RESCORE_ALL = args.rescore_all

    if not os.path.exists(ITEMS_FILE):
        print(f"ERROR: {ITEMS_FILE} not found.", file=sys.stderr)
        return 1

    items, wrapper = load_items(ITEMS_FILE)
    if items is None:
        print("ERROR: JSON root must be an array.", file=sys.stderr)
        return 1

    pending = [
        it for it in items
        if isinstance(it, dict) and it.get("url") and not it.get("summary")
    ]
    print(f"Total items: {len(items)}, pending: {len(pending)}, translate={'on' if TRANSLATE else 'off'}")

    processed = failed = 0
    for it in pending:
        if processed >= MAX_ITEMS:
            print(f"Reached MAX_ITEMS={MAX_ITEMS}, stopping.")
            break
        print(f"[{processed + 1}] {it.get('title', '')[:60]}")
        print(f"    {it['url']}")

        content, source_type = fetch_content(it["url"])
        if source_type == "blocked":
            # 永久性封鎖：寫入佔位摘要，停止每日無限重試
            it["summary"] = BLOCKED_SUMMARY
            processed += 1
            print(f"    blocked -> placeholder written")
            save_items(ITEMS_FILE, items, wrapper)
            time.sleep(SLEEP_BETWEEN_ITEMS)
            continue
        if not content:
            print("    no content, skipped.")
            failed += 1
            continue

        try:
            it["summary"] = build_summary(content, source_type)
        except Exception as e:
            print(f"    summarize failed: {e}")
            failed += 1
            continue

        processed += 1
        print(f"    ok ({source_type}, {len(it['summary'])} chars)")
        save_items(ITEMS_FILE, items, wrapper)
        time.sleep(SLEEP_BETWEEN_ITEMS)

    print(f"Done. processed={processed}, failed={failed}")

    newly_scored = score_corpus_novelty(items)
    if newly_scored:
        save_items(ITEMS_FILE, items, wrapper)
        print(f"Scored novelty for {newly_scored} new item(s); existing scores kept.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
