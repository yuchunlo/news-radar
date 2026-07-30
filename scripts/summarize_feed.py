#!/usr/bin/env python3

import glob
import html as html_mod
import json
import math
import os
import tempfile
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

# ---- Subtitle summarization ---------------------------------------
SUBTITLES_DIR = os.environ.get("SUBTITLES_DIR", "data/subtitles")
SUMMARY_RATIO = float(os.environ.get("SUMMARY_RATIO", "0.9"))
SUMMARY_MAX = int(os.environ.get("SUMMARY_MAX", "60000"))
FOREIGN_BUDGET_RATIO = 1.2    # [tune]

# ---- Sentence scoring weights (extractive_summary) -------------------------
ENTITY_WEIGHT_BASE = 1.2      # [tune] base multiplier for sentences with entities
ENTITY_WEIGHT_STEP = 0.08     # [tune] extra multiplier per additional entity (capped at 5)
ENTITY_WEIGHT_CAP = 5         # [tune]
FLUFF_PENALTY = 0.25          # [tune] score penalty for entity-free filler sentences
LEAD_BIAS = 0.5               # [tune] max positional boost for sentences near the top
# ---- MMR sentence selection -------------------------------------------------
MMR_LAMBDA = 0.7              # [tune] redundancy penalty strength
MMR_DUP_THRESHOLD = 0.65      # [tune] sentences with Jaccard similarity >= this are dropped outright

# ---- Information-value scoring ----------------------------------------------
INFO_VALUE_SATURATION = 4.0   # [tune] fact-carrier density per 100 chars counted as "saturated" (=100)
NOVELTY_DUP_COVERAGE = 0.7    # [tune] fact-overlap ratio >= this is flagged as a reprint

# ---- Fetching / connection ---------------------------------------------------
FETCH_TIMEOUT = 30            # [tune] per-request timeout (seconds)
FETCH_TIMEOUT_SLOW = 60       # [tune] timeout for hosts known to be slow to first byte
FETCH_RETRIES = 3             # [tune] auto-retry count for transient errors (429/5xx/connection)
SLEEP_BETWEEN_ITEMS = 1.5     # [tune] polite delay between items (seconds)
TIME_BUDGET_SECONDS = int(os.environ.get("TIME_BUDGET_SECONDS", "0"))
TIME_BUDGET_STOP_RATIO = 0.5  # [tune]
FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    # Enterprise CDNs (Akamai in particular) stall or drop requests whose
    # header set doesn't look like a real navigation, which is what made
    # www.mckinsey.com time out rather than answer.
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="126", "Not:A-Brand";v="24", "Google Chrome";v="126"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Connection": "keep-alive",
}
try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except ModuleNotFoundError:
    BeautifulSoup = None
    _HAS_BS4 = False

# curl_cffi ships with the project already (requirements.txt pulls it in via
# yt-dlp[curl-cffi]). It replays a real Chrome TLS/HTTP2 fingerprint, which is
# what gets past the fingerprint checks that make plain `requests` hang or get
# a 403 on some sites.
try:
    from curl_cffi import requests as curl_requests
    _HAS_CURL_CFFI = True
except Exception:
    curl_requests = None
    _HAS_CURL_CFFI = False

CURL_IMPERSONATE = os.environ.get("CURL_IMPERSONATE", "chrome")

# Hosts whose first byte legitimately takes a long time; they get the longer
# timeout and go straight to the impersonating client.
SLOW_HOSTS = (
    "mckinsey.com",
    "bcg.com",
    "deloitte.com",
    "hbr.org",
)

# Text-extraction proxy and archive mirror, used only after the direct
# strategies have failed.
READER_PROXY = os.environ.get("READER_PROXY", "https://r.jina.ai/")
USE_READER_PROXY = os.environ.get("USE_READER_PROXY", "on").lower() != "off"
USE_WAYBACK = os.environ.get("USE_WAYBACK", "on").lower() != "off"
# When a page yields only a meta description, also ask the reader proxy for the
# real body. Off by default: correct but expensive, since a lot of pages are
# meta-only and each one becomes an extra third-party request.
READER_ON_META = os.environ.get("READER_ON_META", "").lower() in ("1", "true", "yes", "on")
SUMMARY_SKIP_HOSTS = ("news.google.com",)


def is_summary_skip_host(url: str) -> bool:
    host = host_of(url)
    return any(host == h or host.endswith("." + h) for h in SUMMARY_SKIP_HOSTS)


FEED_FIRST_HOSTS = (
    "abei.club",
    "ageofinvention.xyz",
    "artincontext.org",
    "attlin.com",
    "beartalking.com",
    "bituzi.com",
    "blogspot.com",
    "buttondown.com",
    "caffes.me",
    "careher.net",
    "cashchou.com",
    "chaidarun.com",
    "cityofsound.com",
    "cocktail4party.com",
    "coolshell.cn",
    "curtismchale.ca",
    "davidoks.blog",
    "devtang.com",
    "esence.travel",
    "firstround.com",
    "fs.blog",
    "gilifedesigner.com",
    "honest-broker.com",
    "huli.tw",
    "hunterwalk.com",
    "kopu.chat",
    "limboy.me",
    "lipperalpha.refinitiv.com",
    "lostmagazine.org",
    "louie.lu",
    "lutaonan.com",
    "matters.town",
    "medium.com",
    "meiguinfo.com",
    "mickzh.com",
    "noswag.tw",
    "personaljournal.ca",
    "polgeonow.com",
    "pseudoyu.com",
    "readtrung.com",
    "ruanyifeng.com",
    "samaltman.com",
    "shiuncorner.com",
    "sirupsen.com",
    "sive.rs",
    "smallbooks.com.tw",
    "soidid.tw",
    "starrocket.io",
    "steveblank.com",
    "substack.com",
    "tiaodao.typlog.io",
    "travelwithbook.com",
    "unchartedterritories.tomaspueyo.com",
    "uselessetymology.com",
    "vox.com",
    "waitbutwhy.com",
    "werner.wiki",
    "wordpress.com",
    "yuanyu.idv.tw",
    "zmonster.me",
)
FEED_FIRST_SAVE_EVERY = 50
WAYBACK_LOOKUP = "https://archive.org/wayback/available"
MIN_USABLE_BODY = 200         # [tune] chars below which a body isn't worth keeping

# ---- Boilerplate / non-content removal --------------------------------------
# Everything from one of these markers to the END of the text is site
# furniture rather than article content (author sign-offs, comment and
# review sections, ...), so the text is truncated at the earliest match.
CUT_TO_END_RE = re.compile(
    r"Cite this work"
    r"|Advertise here with Carbon Ads"
    r"|謝謝你閱讀到這裡"
    r"|（本文由 MoneyDJ新聞 授權轉載"
    r"|相關報導"
    r"|「食驗室」是《食力foodNEXT》推出的全台最大飲食新品試用平台"
)
MIN_KEEP_AFTER_CUT = 80
REMOVE_BLOCKS = [
    "Matrix 是少数派的写作社区，我们主张分享真实的产品体验，有实用价值的经验与思考。我们会不定期挑选 Matrix 最优质的文章，展示来自用户的最真实的体验和观点。",
    "文章代表作者个人观点，少数派仅对标题和排版略作修改。",
    "欢迎收看本期《派评》。你可以通过文章目录快速跳转到你感兴趣的内容。如果发现了其它感兴趣的 App 或者关注的话题，也欢迎在评论区和我们讨论。",
]
assert not isinstance(REMOVE_BLOCKS, str), "REMOVE_BLOCKS must be a list of whole blocks"
# Filler lead-ins that carry no information of their own, plus inline site
# furniture (related-article plugs, tag lines) that trafilatura keeps.
# NOTE: no \b here -- CJK characters are word characters to Python's re, so
# \b before a CJK character only matches after punctuation or at the start of
# the string, silently missing most real occurrences.
REMOVE_PHRASE_RE = re.compile(
    r"最核心的一句話[:：]"
    r"|綜合外媒報導，"
    r"|結果顯示，"
    r"|換言之，"
    r"|相較之下，"
    r"|更?值得注意的是，"
    r"|事實上，情況比這更糟——"
    r"|（前情提要：[^）]*）"
    r"|（背景補充：[^）]*）"
    r"|（首圖來源：[^）]*）"
    r"|美股探路客 PressPlay.*?訂閱！"
    r"|美股探路客推薦.*訂閱專案"
    # Anchored to a full line on purpose: an unanchored [^\n]* would run to
    # the end of the text whenever the body has been flattened to one line.
    r"|^[ \t]*標籤[:：][^\n]*$",
    re.M,
)

# ---- Marking & detection ----------------------------------------------------
FALLBACK_MARK = "↛"
TABLE_NOTE = "請參閱所附表格 " + FALLBACK_MARK
TABLE_TAG_RE = re.compile(r"<table[\s>]", re.I)
BLOCKED_SUMMARY = "無法取得頁面內容（來源網站封鎖自動化存取）" + FALLBACK_MARK
GONE_SUMMARY = "無法取得頁面內容（原始頁面已移除，且無存檔）" + FALLBACK_MARK
# Techmeme uses these leads for stories built on its own sourcing.
TECHMEME_STAR_PREFIXES = ("Source", "Report", "Documents:")
CHALLENGE_PATTERN = re.compile(
    r"安全验证|安全驗證|验证码|驗證碼|禁止访问|禁止訪問|访问异常|異常流量|异常流量|"
    r"Just a moment|Checking your browser|Verify you are human|"
    r"[Ee]nable JavaScript and cookies|Access [Dd]enied|cf-challenge"
)

JUNK_BODY_RE = re.compile(
    r"Just a moment"
    r"|Checking your browser"
    r"|Verify you are human"
    r"|Enable JavaScript and cookies"
    r"|Please enable (?:JS|JavaScript|cookies)"
    r"|(?:Access|Permission) [Dd]enied"
    r"|You don'?t have permission to access"
    r"|Why have I been blocked"
    r"|Cloudflare Ray ID"
    r"|Attention Required"
    r"|Request unsuccessful"
    r"|cf-challenge|cf_chl"
    r"|needs to review the security of your connection"
    r"|protect itself from (?:online attacks|malicious bots)"
    r"|verif(?:y|ies) (?:that )?you are not a bot"
    r"|[Mm]aking sure you'?re not a bot"
    r"|Anubis (?:to protect|has protected)"
    r"|Comprehensive up-to-date news coverage, aggregated from sources"
    r"|豆瓣[\sa-zA-Z.]{0,24}載入中|豆瓣[\sa-zA-Z.]{0,24}加载中"
    r"|安全验证|安全驗證|验证码|驗證碼"
    r"|禁止访问|禁止訪問|访问异常|異常流量|异常流量"
    r"|正在驗證您的請求|正在验证您的请求"
    r"|該網站使用安全服務|该网站使用安全服务"
    r"|正在確認你是不是機器人|正在确认你是不是机器人",
    re.I,
)
JUNK_SCAN_CHARS = 4000


def is_junk_body(text: str) -> bool:
    """True when the extracted text is an interstitial / generic site blurb
    rather than the article."""
    return bool(text) and bool(JUNK_BODY_RE.search(text[:JUNK_SCAN_CHARS]))


BLANK_SUMMARY = " "
DOUBAN_MARK_PREFIXES = ("想读", "想看", "想听")
def is_douban_mark(url: str, title: str) -> bool:
    return ("douban.com" in (url or "")
            and (title or "").strip().startswith(DOUBAN_MARK_PREFIXES))


TRACKING_PARAM_EXACT = {
    "ref", "spm", "fbclid", "gclid", "igshid", "mkt_tok",
    "mc_cid", "mc_eid", "_hsenc", "_hsmi",
    "oc",
}

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


def _to_twp(text: str) -> str:
    return _tr.to_traditional(text) if _tr else text


# ---------------------------------------------------------------- Connection / URL / encoding

_SESSION: requests.Session | None = None


def get_session() -> requests.Session:
    """Singleton requests.Session: reuses connection pools + auto-retries
    (backoff on 429/5xx/connection errors). Replaces per-item requests.get(),
    speeds up fetching multiple pages from the same site, and the translate
    endpoint reuses the same session too."""
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
    """Strip tracking parameters (utm_*, fbclid, gclid, spm, ref, ...) and
    normalize, so different tracking links to the same page collapse into
    the same key, improving dedup and reprint-detection accuracy."""
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
    """Fix mojibake caused by "UTF-8 bytes misdecoded as Latin-1/CP1252"
    (common in RSS/web pages). Only attempted when mojibake signatures are
    detected, to avoid touching normal text."""
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


# ---------------------------------------------------------------- YouTube subtitles

def is_youtube_url(url: str) -> bool:
    return "youtube.com" in (url or "") or "youtu.be" in (url or "")


def youtube_video_id(url: str) -> str | None:
    m = (re.search(r"[?&]v=([^&]+)", url or "") or
         re.search(r"/shorts/([^?&/]+)", url or "") or
         re.search(r"/live/([^?&/]+)", url or "") or
         re.search(r"youtu\.be/([^?&/]+)", url or ""))
    return m.group(1) if m else None


def youtube_thumbnail_url(url: str) -> str | None:
    vid = youtube_video_id(url)
    return f"https://img.youtube.com/vi/{vid}/mqdefault.jpg" if vid else None


# Subtitle filename format (produced by download_sub.py):
# "{item_id}.{original language or NA}.{subtitle language}.vtt"
VTT_NAME_RE = re.compile(r"^(?P<orig>[^.]+)\.(?P<sub>[^.]+)\.vtt$")


def _subtitle_candidates(item_id: str):
    """Return every subtitle file for an item_id, as (path, original_lang, subtitle_lang)."""
    out = []
    if not item_id:
        return out
    for path in glob.glob(os.path.join(SUBTITLES_DIR, f"{item_id}.*.vtt")):
        m = VTT_NAME_RE.match(os.path.basename(path)[len(item_id) + 1:])
        if not m:
            continue
        out.append((path, m.group("orig"), m.group("sub")))
    return out


def pick_subtitle(item_id: str):
    """The same item_id often has several language variants of a subtitle
    file; pick one using the following priority order:

    0. Subtitle language == the video's original language (manual caption,
       or otherwise the auto-generated caption in that language — only a
       single "speech -> text" pass, closest to the original meaning)
    1. Chinese (zh / zh-Hans / zh-Hant / zh-HK / zh-TW, etc.), excluding
       the double machine-translated variants (see item 3) — only a single
       "ASR + translation" pass, and no further translation is needed
       downstream (zh-Hans just gets converted to Traditional)
    2. A single-hop translated caption in any other language (e.g. original
       language French, caption language English) — still just one
       translation pass, decent quality, handed to the existing Google
       Translate endpoint afterwards
    3. "Double machine-translated" variants come last: filenames like
       zh-Hant-xx / zh-Hans-xx (a batch of extra variants YouTube produces
       when it translates the auto-generated caption "again" into other
       target directions — these only get downloaded because --sub-langs
       used the zh.* wildcard). These went through one extra hop of machine
       translation, wording often drifts, and they occasionally carry an
       "AI-translated" watermark line; use them only when nothing else is
       available.
    On ties, pick whichever sorts first by filename, for deterministic
    results across runs.
    """
    cands = _subtitle_candidates(item_id)
    if not cands:
        return None

    def rank(cand):
        path, orig, sub = cand
        orig_l, sub_l = orig.lower(), sub.lower()
        chained = bool(re.match(r"^zh-(hant|hans)-.+", sub_l))
        if chained:
            return (3, path)
        if sub_l == orig_l:
            return (0, path)
        if sub_l in ("zh", "zh-hant", "zh-hans", "zh-hk", "zh-tw"):
            return (1, path)
        return (2, path)

    cands.sort(key=rank)
    return cands[0]


VTT_TAG_RE = re.compile(r"<[^>]+>")
VTT_WATERMARK_RE = re.compile(r"\[[^\]]*(?:人工智慧翻譯|AI\s*翻譯|criblate\.com)[^\]]*\]", re.I)

CAPTION_ANNOTATION_RE = re.compile(
    r"\[\s*(?:_+|\*+|\s)*\s*\]"
    r"|\[[^\]\n]{1,30}\]"
    r"|\(\s*(?:music|applause|laughter|inaudible|crosstalk|silence)\s*\)",
    re.I,
)
SPEAKER_ARROW_RE = re.compile(r"(?:&gt;\s*){2,}|>{2,}")


def clean_caption_text(text: str) -> str:
    """Strip non-speech annotation and entity escapes from caption text."""
    text = html_mod.unescape(text)
    text = SPEAKER_ARROW_RE.sub("\n", text)
    text = CAPTION_ANNOTATION_RE.sub(" ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"^[ \t]+|[ \t]+$", "", text, flags=re.M)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


# ---- Markdown (reader-proxy output) -----------------------------------------
# r.jina.ai renders pages to Markdown, so its text arrives full of image
# embeds, link targets, heading hashes and emphasis markers. trafilatura's
# output is plain text, so this only ever runs on reader-proxy text.
MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
MD_IMAGE_LABEL_RE = re.compile(
    r"\[\s*!?\s*\[?\s*(?:Image|圖片|图片|圖像|图像|Figure|插圖|插图)"
    r"\s*\d*\s*[:：]?[^\]]*\](?:\([^)]*\))?\s*\]?",
    re.I,
)
MD_LINK_RE = re.compile(r"\[([^\]\n]*?)\]\((?:https?:|/|#|mailto:)[^)\s]*\)")
MD_BARE_URL_RE = re.compile(r"<?https?://[^\s)\]<>，。）]+>?")
MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
MD_RULE_RE = re.compile(r"^\s*(?:[-*_]\s*){3,}$", re.M)
MD_BULLET_RE = re.compile(r"^\s{0,6}(?:[-*+]|\d{1,2}[.)])\s+", re.M)
MD_EMPH_RE = re.compile(r"\*\*|__|\*|`|~~")
MD_ORPHAN_BRACKET_RE = re.compile(r"^[\s\]\[)(|:-]+|[\s\[(|]+$", re.M)


def clean_markdown(text: str) -> str:
    """Reduce Markdown to the prose inside it, keeping link anchor text."""
    if not text:
        return text
    text = html_mod.unescape(text)
    text = MD_IMAGE_RE.sub("", text)
    text = MD_IMAGE_LABEL_RE.sub("", text)
    for _ in range(3):                      # nested [a](b) inside [c](d)
        new = MD_LINK_RE.sub(r"\1", text)
        if new == text:
            break
        text = new
    text = MD_RULE_RE.sub("", text)
    text = MD_HEADING_RE.sub("", text)
    text = MD_BULLET_RE.sub("", text)
    text = MD_BARE_URL_RE.sub("", text)
    text = MD_EMPH_RE.sub("", text)
    text = MD_ORPHAN_BRACKET_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---- PTT --------------------------------------------------------------------
PTT_META_RE = re.compile(r"^\s*(?:作者|標題|時間|看板)[\s:：].*$", re.M)
PTT_SIG_RE = re.compile(r"^\s*※\s*(?:發信站|文章網址|編輯\s*[:：]|伸謝)[^\n]*$", re.M)
PTT_PUSH_PREFIX_RE = re.compile(
    r"(?:^|(?<=[\s。！？]))\s*(?:推|噓|嘘|→)\s*[A-Za-z0-9_]{2,20}\s*[:：]\s*"
)
PTT_IP_TIME_RE = re.compile(
    r"\s*(?:\d{1,3}(?:\.\d{1,3}){3})?\s*\d{2}/\d{2}\s+\d{2}:\d{2}\s*"
)
PTT_BARE_IP_RE = re.compile(r"\s*\d{1,3}(?:\.\d{1,3}){3}\s*")


def clean_ptt(text: str) -> str:
    """Normalise a PTT page: drop the metadata header and signature block,
    strip the 推/噓/→ tag and the commenter id in front of each comment, and
    strip the ip + date + time that trails it.

    The ip/date/time removal matters for more than tidiness: every push line
    ends in an ip address and a timestamp, which entity_count() reads as
    fact-dense, so leaving them in makes the extractive scorer prefer comment
    lines over the article body.
    """
    if not text:
        return text
    text = PTT_META_RE.sub("", text)
    text = PTT_SIG_RE.sub("", text)
    text = PTT_PUSH_PREFIX_RE.sub("\n", text)
    text = PTT_IP_TIME_RE.sub("\n", text)
    text = PTT_BARE_IP_RE.sub(" ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{2,}", "\n", text).strip()


def is_ptt(url: str) -> bool:
    return "ptt.cc" in (url or "")

CJK_TERMINAL_RE = re.compile(r"[。！？!?；;]")
UNPUNCTUATED_CHARS_PER_TERMINAL = 60
ZH_CONNECTIVE_RE = re.compile(
    r"^(但是|但|不過|可是|然而|所以|因為|因此|於是|結果|其實|如果|要是|"
    r"而且|另外|同時|然後|之後|後來|首先|接著|最後|即是|反而|不然|"
    r"例如|譬如|比如|總之|換言之)"
)
MIN_CAPTION_CHARS = 30       # [tune] below this a caption track carries no speech
SUBTITLE_MERGE_TARGET = 45   # [tune] aim for pseudo-sentences of about this many chars
SUBTITLE_MERGE_MAX = 75      # [tune] never let one grow past this
SUBTITLE_CONNECTIVE_MIN = 15 # [tune] don't break at a connective below this length
ZH_CONTINUATION_RE = re.compile(
    r"^(的|地|得|了|著|過|嗎|呢|吧|啊|喔|嘛|就|才|也|都|還|再|又|並|"
    r"和|與|或|至|到|給|把|被|從|對|向|以及)"
)


def chars_per_terminal(text: str) -> float:
    n = len(CJK_TERMINAL_RE.findall(text))
    return float("inf") if n == 0 else len(text) / n


def merge_caption_lines(text: str) -> str:
    """Rebuild sentence-like units from sparsely punctuated caption cues.

    Consecutive cue lines are joined with commas until the unit reaches
    SUBTITLE_MERGE_TARGET characters, breaking early when the next cue opens
    with a discourse connective, and every finished unit is terminated with a
    full stop. That gives the scorer units large enough to carry signal, and
    makes the final summary readable rather than one long unbroken run.

    Punctuation already present is respected rather than doubled up: a cue
    that ends in a sentence-final mark closes the unit there, and no comma or
    full stop is inserted next to an existing mark. This keeps the function
    safe to run on transcripts that carry occasional punctuation, not just on
    ones with none at all.
    """
    units: list[str] = []
    cur = ""

    def flush():
        nonlocal cur
        if cur:
            units.append(cur if CJK_TERMINAL_RE.search(cur[-1]) else cur + "。")
            cur = ""

    for line in (l.strip() for l in text.split("\n")):
        if not line:
            continue
        # Connectives are checked first, so a word that begins with a
        # continuation particle but actually opens a clause still breaks.
        is_conn = bool(ZH_CONNECTIVE_RE.match(line))
        is_cont = (not is_conn) and bool(ZH_CONTINUATION_RE.match(line))
        can_break = bool(cur) and (not is_cont or len(cur) >= SUBTITLE_MERGE_MAX)
        if can_break and (
            len(cur) >= SUBTITLE_MERGE_TARGET
            or len(cur) + len(line) > SUBTITLE_MERGE_MAX
            or (len(cur) >= SUBTITLE_CONNECTIVE_MIN and is_conn)
        ):
            flush()
        if not cur:
            cur = line
        elif is_cont or cur[-1] in "，,、。！？!?；;":
            cur += line          # already punctuated, or a mid-phrase continuation
        else:
            cur += "，" + line
        # A cue that ends on a sentence-final mark is a natural boundary.
        if cur and CJK_TERMINAL_RE.search(cur[-1]):
            flush()
    flush()
    return "\n".join(units)


def vtt_to_text(path: str) -> str:
    """Convert a .vtt file into clean, sentence-by-sentence text.

    YouTube's auto-generated captions commonly use a "rolling" word-by-word
    animation: a single sentence is split across several cues played in
    sequence, and each cue's text = "the previous line (finalized, repeated
    verbatim)" + "the line currently being typed out". So we only take the
    "last line" of each cue block as that cue's contribution, and skip any
    line that's identical to the previous one — this reconstructs normal,
    sentence-by-sentence text. Manual captions / non-rolling captions
    already have exactly one line per cue, so the same logic is a no-op
    for them.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            raw = f.read()
    except Exception:
        return ""

    raw = VTT_WATERMARK_RE.sub("", raw)
    cues: list[list[str]] = []
    for line in raw.splitlines():
        if "-->" in line:
            cues.append([])
        elif cues:
            cues[-1].append(line)

    lines_out = []
    last = None
    for payload in cues:
        cleaned = []
        for tl in payload:
            tl = VTT_TAG_RE.sub("", tl).strip()
            if tl:
                cleaned.append(tl)
        if not cleaned:
            continue
        candidate = cleaned[-1]
        if candidate != last:
            lines_out.append(candidate)
            last = candidate

    text = "\n".join(lines_out)
    text = clean_caption_text(text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    if (text and cjk_ratio(text) >= 0.25
            and chars_per_terminal(text) > UNPUNCTUATED_CHARS_PER_TERMINAL):
        text = merge_caption_lines(text)
    return text
# ---------------------------------------------------------------- Thumbnails

IMAGE_EXT_RE = re.compile(r"\.(?:png|jpe?g|gif|webp|avif|svg)(?:$|[?#])", re.I)

# Stock libraries and wire services.
THUMB_STOCK_RE = re.compile(
    r"shutterstock[_-]?\d*"
    r"|istock(?:photo)?"
    r"|gettyimages?[-_]?\d*|gyi\d{6,}"
    r"|depositphotos|adobestock|dreamstime|alamy|123rf|bigstock|stockphoto"
    r"|unsplash"
    r"|photo-\d{10,}-[0-9a-f]{8,}"          # unsplash's own filename shape
    r"|pexels(?:-photo)?[-_]?\d*"
    r"|\bap[-_]?photo\b|associated[-_]press",
    re.I,
)
# Wire-service filenames: Reuters' 2026-07-29T143000Z_123_RTX..., RTS/RTX ids,
# and the bare-timestamp shapes agencies use.
THUMB_WIRE_RE = re.compile(
    r"\d{4}-?\d{2}-?\d{2}t\d{6}z"
    r"|\brt[sxr][a-z0-9]{5,}"
    r"|\bafp[-_]?\d{6,}"
    r"|\bepa[-_]?(?:efe[-_]?)?\d{6,}",
    re.I,
)
# Layout furniture: the same file on every article.
THUMB_TEMPLATE_RE = re.compile(
    r"og[-_]?image|og[-_]?default|social[-_]?(?:card|share|image|preview)"
    r"|twitter[-_]?card|share[-_]?(?:image|card)|card[-_]?bg"
    r"|cover[-_]?template|template[-_]?cover|[-_]template\b|^template"
    # "default" must stand alone as a token: YouTube's own video thumbnails are
    # named mqdefault.jpg / hqdefault.jpg and are perfectly good images.
    r"|(?:^|[-_])default(?:[-_](?:image|thumb|cover|banner))?(?:[-_]|$)"
    r"|placeholder|fallback"
    r"|no[-_]?image|dummy|generic[-_]?(?:image|cover)"
    r"|\blogo\b|wordmark|favicon|avatar|profile[-_]?pic|headshot|portrait[-_]?shot"
    r"|\bbanner\b|\bheader[-_]?(?:image|bg)?\b|hero[-_]?(?:image|bg)"
    r"|watermark|spacer|pixel|blank|transparent|1x1",
    re.I,
)
# Screenshots, product demos, and poster/artwork collages.
THUMB_SCREENSHOT_RE = re.compile(
    r"screen[-_ ]?shot|screenshot|screencap|scrn"
    r"|\bcapture\b|\bdemo\b|\bpreview\b|\bmockup\b|\bui[-_]"
    r"|collage|montage|grid[-_]?of|poster[-_]?(?:grid|collage|set)"
    r"|line[-_]?up\b|\bcombo\b|side[-_]by[-_]side[-_]?photos?",
    re.I,
)
# Words that say "this is a chart / map / diagram", which is exactly what we
# want. A name containing any of these is kept even if it is long.
THUMB_CHART_WORDS = frozenset("""
axis axes chart charts graph graphs plot plotted map maps mapped mapping
diagram schematic figure fig table matrix
index indices ratio ratios rate rates share shares percent percentage pct
scale scaled distribution breakdown composition split spread
trend trends trending trajectory curve curves growth decline change delta
monitor monitoring tracker tracking dashboard scorecard
comparison compare compared versus vs flip gap gaps spread
timeline history historical forecast projection projected outlook
heatmap treemap sankey waterfall scatter histogram bubble radar donut
quarterly annual monthly yearly ytd yoy qoq cagr
bloomberg reuters-graphics ft economist statista ourworldindata visualcapitalist
""".split())
# Words typical of a photograph, whether hand-captioned or machine-described.
THUMB_PHOTO_WORDS = frozenset("""
photo photograph photographed picture pic image img shot shots snapshot
close closeup up view viewing views viewed angle aerial overhead
portrait portraits standing sitting seated walking holding wearing smiling
posing poses posed looking facing gesturing speaking talking waving
exterior interior facade storefront skyline streetscape landscape
location located site situated near outside inside
man woman men women people person crowd worker workers employee
background backdrop foreground blurred bokeh
attends attending arrives arriving during ceremony conference press
generic stock illustrative illustration decorative
""".split())
# Above this many word-like tokens, a name that has no chart vocabulary is
# taken to be a description of a photograph rather than a chart title.
THUMB_DESCRIPTIVE_MIN_TOKENS = 5
THUMB_MIN_STEM = 3


def _thumb_tokens(stem: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", stem.lower()) if t]


def thumbnail_is_usable(url: str) -> tuple[bool, str]:
    """Whether an image url looks like a chart/map worth keeping.

    Returns (verdict, reason) so the reason can be logged -- the rules are
    heuristic and being able to see which one fired is what makes them
    tunable.
    """
    if not url or not url.lower().startswith(("http://", "https://")):
        return False, "not an absolute url"
    if url.startswith("data:"):
        return False, "data uri"
    path = url.split("?", 1)[0].split("#", 1)[0]
    name = path.rstrip("/").rsplit("/", 1)[-1]
    if not IMAGE_EXT_RE.search(path) and not re.search(r"\.(?:png|jpe?g|webp|avif)$", name, re.I):
        return False, "no image extension"
    if name.lower().endswith(".svg"):
        return False, "svg (usually a logo or icon)"
    stem = re.sub(r"\.[a-z0-9]+$", "", name, flags=re.I)
    # Some CMSes append the rendition size: name-1024x576.jpg
    stem = re.sub(r"[-_]\d{2,4}x\d{2,4}$", "", stem)
    if len(stem) < THUMB_MIN_STEM:
        return False, "filename too short to judge"

    haystack = f"{stem} {url}"
    if THUMB_STOCK_RE.search(haystack):
        return False, "stock library filename"
    if THUMB_WIRE_RE.search(haystack):
        return False, "wire-service filename"
    if THUMB_TEMPLATE_RE.search(stem):
        return False, "layout template / site furniture"
    if THUMB_SCREENSHOT_RE.search(stem):
        return False, "screenshot / demo / collage"

    tokens = _thumb_tokens(stem)
    if not tokens:
        return False, "no readable filename"
    # An opaque hash or bare id says nothing; assume it is not a chart.
    if len(tokens) == 1 and (len(tokens[0]) >= 16 or tokens[0].isdigit()):
        return False, "opaque id / hash filename"

    if THUMB_CHART_WORDS & set(tokens):
        return True, "chart vocabulary in filename"
    photo_hits = THUMB_PHOTO_WORDS & set(tokens)
    if photo_hits:
        return False, f"photographic wording ({', '.join(sorted(photo_hits))})"
    if len(tokens) >= THUMB_DESCRIPTIVE_MIN_TOKENS:
        return False, f"descriptive phrase ({len(tokens)} tokens)"
    return True, "short topical filename"


META_IMAGE_RE = re.compile(
    r"""<meta[^>]+(?:property|name)\s*=\s*["'](?:og:image(?::url)?|twitter:image(?::src)?)["']"""
    r"""[^>]+content\s*=\s*["']([^"']+)["']"""
    r"""|<meta[^>]+content\s*=\s*["']([^"']+)["'][^>]+(?:property|name)\s*=\s*"""
    r"""["'](?:og:image(?::url)?|twitter:image(?::src)?)["']""",
    re.I,
)
BODY_IMG_RE = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.I)
IMG_DIM_RE = re.compile(r"""\b(?:width|height)\s*=\s*["']?(\d+)""", re.I)
THUMB_MIN_DIMENSION = 200


def set_thumbnail_from_feed(it: dict, feed_html: str) -> str | None:
    """Thumbnail from the feed copy alone, for paths that never fetch the page.

    The feed-first pass and any item summarised straight from feed_content never
    see the article HTML, so og:image is out of reach -- but RSS
    <content:encoded> and <description> routinely carry <img> tags, and those
    are the same images the page would show.
    """
    if it.get("thumbnail") or not feed_html:
        return None
    found = extract_thumbnail(html_mod.unescape(feed_html), it.get("url") or "")
    if found:
        it["thumbnail"] = found
    return found


def extract_thumbnail(html: str, base_url: str, log: bool = True):
    """First usable image from the page: og:image / twitter:image, then the
    first <img> in the body. Returns None when nothing passes the filter."""
    if not html:
        return None
    candidates = []
    for m in META_IMAGE_RE.finditer(html[:60000]):
        candidates.append((m.group(1) or m.group(2), "meta"))
    for m in BODY_IMG_RE.finditer(html):
        tag = html[m.start():m.end() + 120]
        dims = [int(d) for d in IMG_DIM_RE.findall(tag)]
        if dims and max(dims) < THUMB_MIN_DIMENSION:
            continue                      # an icon or a tracking pixel
        candidates.append((m.group(1), "body"))
        if len(candidates) > 24:
            break
    for raw, where in candidates:
        url = html_mod.unescape((raw or "").strip())
        if url.startswith("//"):
            url = "https:" + url
        elif url.startswith("/"):
            parts = base_url.split("/")
            if len(parts) > 2:
                url = f"{parts[0]}//{parts[2]}{url}"
        ok, reason = thumbnail_is_usable(url)
        if ok:
            if log:
                print(f"    thumbnail ({where}): {url[:90]}  [{reason}]")
            return url
    return None


# ---------------------------------------------------------------- Fetching

def _bs4_fallback_extract(html: str) -> str:
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        return ""
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()

    container = soup.find("article") or soup.find("main")
    scope = container if container else soup
    paras = [p.get_text(" ", strip=True) for p in scope.find_all(["p", "li"])]
    paras = [p for p in paras if len(p) >= 20 and not p.startswith(("©", "Powered by"))]
    return "\n".join(paras)


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


BLOCK_STATUS = {401, 403, 407, 418, 429, 451}


def host_of(url: str) -> str:
    from urllib.parse import urlparse
    try:
        return urlparse(url or "").netloc.lower()
    except Exception:
        return ""


def is_slow_host(url: str) -> bool:
    host = host_of(url)
    return any(host == h or host.endswith("." + h) for h in SLOW_HOSTS)


def is_feed_first_host(url: str) -> bool:
    host = host_of(url)
    return any(host == h or host.endswith("." + h) for h in FEED_FIRST_HOSTS)


def _http_get(url: str, *, timeout: int, impersonate: bool = False):
    """Single GET. Returns (html, status) where status is
    "ok" / "blocked" / "notfound" / "fail"."""
    try:
        if impersonate:
            if not _HAS_CURL_CFFI:
                return None, "fail"
            resp = curl_requests.get(
                url, timeout=timeout, impersonate=CURL_IMPERSONATE,
                headers={"Accept-Language": FETCH_HEADERS["Accept-Language"]},
                allow_redirects=True,
            )
            code = resp.status_code
            html = resp.text
        else:
            resp = get_session().get(url, timeout=timeout)
            code = resp.status_code
            if code < 400:
                if resp.encoding is None or resp.encoding.lower() in ("iso-8859-1", "ascii"):
                    resp.encoding = resp.apparent_encoding
            html = resp.text if code < 400 else ""
        if code in BLOCK_STATUS:
            return None, "blocked"
        if code == 404 or code == 410:
            return None, "notfound"
        if code >= 400:
            return None, "fail"
        return html, "ok"
    except Exception:
        return None, "fail"


def unescape_text(text: str) -> str:
    """Resolve HTML entities. Extraction leaves them behind in a few places --
    meta tag attributes are raw attribute text, and both trafilatura's output
    and RSS bodies can carry &amp; / &#39; / &gt; through unchanged -- and an
    entity that survives to the summary also survives translation."""
    if not text:
        return text
    return html_mod.unescape(text).replace("\u00a0", " ").replace("\u200b", "")


def extract_from_html(html: str):
    """(body, meta, has_table) from a page's HTML."""
    has_table = bool(TABLE_TAG_RE.search(html))
    body = trafilatura.extract(
        html, include_comments=False, include_tables=False, favor_recall=True
    )
    body = maybe_fix_mojibake(unescape_text(body).strip()) if body else ""
    if len(body) < MIN_USABLE_BODY and _HAS_BS4:
        fallback = unescape_text(_bs4_fallback_extract(html))
        if len(fallback) > len(body):
            body = fallback
    meta = maybe_fix_mojibake(unescape_text(extract_meta_description(html)))
    return body, meta, has_table


def classify_text(body: str, meta: str, has_table: bool):
    """Decide whether what we extracted counts as a body or only a blurb."""
    if len(body) >= MIN_USABLE_BODY:
        return body, "body", has_table
    if body and re.search(r"[。！？.!?]", body) and len(body) >= max(80, len(meta)):
        return body, "body", has_table
    if meta:
        return meta, "meta", False
    if body:
        return body, "meta", False
    return None, None, False


def fetch_via_reader(url: str):
    """r.jina.ai renders the page (JavaScript included) and returns plain text.
    Used for sites that won't serve their HTML to a script at all."""
    if not USE_READER_PROXY:
        return None
    text, status = _http_get(
        READER_PROXY.rstrip("/") + "/" + url, timeout=FETCH_TIMEOUT_SLOW
    )
    if status != "ok" or not text:
        return None
    text = maybe_fix_mojibake(text.strip())
    # The reader prepends a "Title: ... / URL Source: ... / Markdown Content:"
    # preamble; drop it so it doesn't end up in the summary.
    marker = "Markdown Content:"
    if marker in text[:1000]:
        text = text.split(marker, 1)[1].strip()
    text = clean_markdown(text)
    if is_junk_body(text):
        print("    reader proxy returned an interstitial, discarded")
        return None
    return text or None


def fetch_via_wayback(url: str):
    """The newest Internet Archive snapshot. This is what recovers pages that
    now 404 because they were moved or deleted after the feed listed them."""
    if not USE_WAYBACK:
        return None
    try:
        resp = get_session().get(
            WAYBACK_LOOKUP, params={"url": url}, timeout=FETCH_TIMEOUT
        )
        resp.raise_for_status()
        snapshot = (resp.json().get("archived_snapshots") or {}).get("closest") or {}
    except Exception:
        return None
    if not snapshot.get("available") or not snapshot.get("url"):
        return None
    # "id_" asks the archive for the original bytes without its own banner.
    snap_url = re.sub(r"(/web/\d+)/", r"\1id_/", snapshot["url"], count=1)
    html, status = _http_get(snap_url, timeout=FETCH_TIMEOUT_SLOW)
    return html if status == "ok" else None


def fetch_content(url: str, feed_content: str = "",
                  meta_out: dict | None = None):
    """Get an article's text, trying every strategy before giving up.

    Returns (text, source_type, has_table) where source_type is:

    - "body"    : real article text (from the page, a URL variant, the feed's
                  own copy of the body, the reader proxy, or the archive)
    - "meta"    : only a blurb was available -> summary gets the FALLBACK_MARK
    - "blocked" : every strategy was refused; the caller writes a placeholder
                  so the item isn't retried forever
    - None      : transient failure, left pending for the next run

    Table contents are deliberately excluded from the extracted body
    (include_tables=False); has_table only records that the page had one, so
    build_summary can note it instead of inlining rows of cells.

    Order matters: the cheap direct request comes first so that the ~95% of
    URLs that just work are unaffected, and the expensive third-party
    strategies only run for the ones that failed.
    """
    def postprocess(result):
        """Per-source text cleanup, applied whichever strategy won."""
        text, source_type, has_table = result
        if text and is_ptt(url):
            text = clean_ptt(text)
            if len(text) < MIN_USABLE_BODY and source_type == "body":
                source_type = "meta"
        return text, source_type, has_table

    slow = is_slow_host(url)
    timeout = FETCH_TIMEOUT_SLOW if slow else FETCH_TIMEOUT
    best = (None, None, False)
    blocked = False
    notfound = False
    junk = False

    def consider(html: str):
        """Extract, and return a result if it is good enough to stop on."""
        nonlocal best, blocked, junk
        if meta_out is not None and not meta_out.get("thumbnail"):
            found = extract_thumbnail(html, url)
            if found:
                meta_out["thumbnail"] = found
        body, meta, has_table = extract_from_html(html)
        if len(body) < MIN_USABLE_BODY and CHALLENGE_PATTERN.search(html[:20000]):
            blocked = True
            return None
        if is_junk_body(body) or is_junk_body(meta):
            junk = True
            return None
        result = classify_text(body, meta, has_table)
        if result[1] == "body":
            return result
        if result[1] and best[1] is None:
            best = result       # remember the blurb, keep looking for a body
        return None

    # A host known to fingerprint-check its clients is not worth a plain
    # request first; go straight to the impersonating one.
    modes = [True] if (slow and _HAS_CURL_CFFI) else [False, True]
    for impersonate in modes:
        if impersonate and not _HAS_CURL_CFFI:
            continue
        html, status = _http_get(url, timeout=timeout, impersonate=impersonate)
        if status == "blocked":
            blocked = True
            continue
        if status == "notfound":
            notfound = True
            break               # a 404 is the same for both clients
        if status != "ok" or not html:
            continue
        found = consider(html)
        if found:
            if impersonate:
                print(f"    recovered via curl_cffi: {url}")
            return postprocess(found)

    # The feed usually carried the article with it; free, and no third party.
    feed_text = unescape_text(feed_content or "").strip()
    if len(feed_text) >= MIN_USABLE_BODY:
        print("    recovered via feed content")
        return postprocess((feed_text, "body", False))

    # A blurb is a poor result but it is a result. Escalating to the external
    # services for every meta-only page would mean thousands of extra requests
    # per run, so by default that only happens when there is nothing at all.
    if best[1] == "meta" and not READER_ON_META:
        return postprocess(best)

    reader_text = fetch_via_reader(url)
    if reader_text and len(reader_text) >= MIN_USABLE_BODY:
        print("    recovered via reader proxy")
        return postprocess((reader_text, "body", False))

    archived = fetch_via_wayback(url)
    if archived:
        found = consider(archived)
        if found:
            print("    recovered via web archive")
            return postprocess(found)

    if feed_text:
        print("    recovered via feed content (short)")
        return postprocess((feed_text, "meta", False))

    if best[1]:
        return postprocess(best)
    if junk:
        print("    interstitial / generic site blurb only, skipped")
        return None, "junk", False
    if blocked:
        print("    blocked by site, no copy found by any strategy")
        return None, "blocked", False
    if notfound:
        print("    page is gone (404/410) and not archived anywhere")
        return None, "gone", False
    print("    all fetch strategies failed")
    return None, None, False


# ---------------------------------------------------------------- Language detection

def cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    letters = sum(1 for ch in text if ch.isalpha() or "\u4e00" <= ch <= "\u9fff")
    return cjk / letters if letters else 0.0


KANA_RE = re.compile(r"[\u3040-\u309f\u30a0-\u30fa\u30fc]")
JA_KANA_THRESHOLD = 0.15
JA_KANA_MIN_COUNT = 8


def kana_ratio(text: str) -> float:
    if not text:
        return 0.0
    letters = sum(1 for ch in text if ch.isalpha() or "\u4e00" <= ch <= "\u9fff")
    return len(KANA_RE.findall(text)) / letters if letters else 0.0


def is_japanese(text: str) -> bool:
    return (len(KANA_RE.findall(text or "")) >= JA_KANA_MIN_COUNT
            and kana_ratio(text) >= JA_KANA_THRESHOLD)


def is_cjk_lang(lang: str) -> bool:
    """Languages written without inter-word spaces."""
    return lang in ("zh-hant", "zh-hans", "ja")


def detect_lang(text: str) -> str:
    if is_japanese(text):
        return "ja"
    if cjk_ratio(text) < 0.25:
        return "other"
    variant = _tr.detect_variant(text) if _tr else "hant"
    return "zh-hant" if variant == "hant" else "zh-hans"


# ---------------------------------------------------------------- Summarization

ZH_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*")
EN_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\u00c0-\u024f\"'(])")
BULLET_SPLIT_RE = re.compile(
    r"(?<=[\u4e00-\u9fff%）。，、])\s*[-–—•·]\s*(?=[\u4e00-\u9fff\dA-Za-z])"
)

EN_STOPWORDS = frozenset("""
a an the and or but if then than that this these those of in on at to for from
with by as is are was were be been being it its it's he she they them his her
their we you your i not no so do does did done have has had will would can
could should may might must about into over under between after before during
what which who whom whose when where why how all any both each few more most
other some such only own same very s t just don now also there here out up
""".split())


def split_sentences(text: str, is_cjk: bool):
    text = BULLET_SPLIT_RE.sub("\n", text)
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
        toks = [chars[i:i + 2] for i in range(len(chars) - 1)]
        toks += re.findall(r"[0-9]+(?:\.[0-9]+)?%?", sentence)
        return toks
    toks = re.findall(r"[a-zA-Z][a-zA-Z'-]+|[0-9]+(?:\.[0-9]+)?%?", sentence.lower())
    return [t for t in toks if t not in EN_STOPWORDS]


NUM_PATTERN = re.compile(r"[0-9０-９][0-9０-９,.:%０-９]*|[一二三四五六七八九十百千萬億兆]{2,}")
ENTITY_PATTERN = re.compile(
    r"[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)+|《[^》]{1,30}》|「[^」]{1,20}」|『[^』]{1,20}』|[0-9.]+\s*(?:%|per cent|percent)"
)
DISCOURSE_ZH = re.compile(
    r"^(然而|不過|此外|另外|同時|當然|其實|事實上|值得一提的是|換句話說|"
    r"也就是說|總而言之|總的來說|不僅如此|除此之外|與此同時|与此同时|与此同時|"
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
    """Entity-free rhetorical questions / pure-pronoun exclamations carry
    close to zero information."""
    if entity_count(s) > 0:
        return False
    if s.rstrip().endswith(("？", "?")):
        return True
    if is_cjk and PRONOUN_ONLY_ZH.match(s):
        return True
    return False


# Splits after a sentence terminator, keeping the terminator with the sentence
# it ends so that re-joining the pieces reproduces the input exactly. The Latin
# arm needs the "whitespace then an opening character" lookahead, or it would
# break on decimals and abbreviations such as "U.S." or "3.5".
TRIM_SPLIT_RE = re.compile(
    r"(?<=[。！？；;])(?![」』”\"'）)])\s*"
    r"|(?<=[.!?])\s+(?=[A-Z0-9\u00c0-\u024f\u4e00-\u9fff\"'(])"
)


def sentence_pieces(text: str) -> list[str]:
    """Split into sentences by slicing at boundary offsets, so concatenating
    the pieces reproduces the input character for character (the whitespace a
    boundary swallows stays attached to the sentence before it)."""
    pieces: list[str] = []
    prev = 0
    for m in TRIM_SPLIT_RE.finditer(text):
        if m.end() > prev:
            pieces.append(text[prev:m.end()])
            prev = m.end()
    if prev < len(text):
        pieces.append(text[prev:])
    return [p for p in pieces if p.strip()]


def trim_to_whole_sentences(text: str, limit: int) -> str:
    """Shorten `text` to at most `limit` characters by dropping whole
    sentences from the end.

    A hard slice at the limit leaves the reader with half a sentence, so
    sentences are removed one at a time instead. If even the first sentence is
    over the limit, it is returned whole: the budget is derived from the length
    of the source text, so that only happens when the source is a single
    sentence, and returning it intact beats cutting it in the middle.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    pieces = sentence_pieces(text)
    kept: list[str] = []
    used = 0
    for piece in pieces:
        if used + len(piece.rstrip()) > limit:
            break
        kept.append(piece)
        used += len(piece)
    if kept:
        return "".join(kept).strip()
    return pieces[0].strip() if pieces else text


def extractive_summary(text: str, is_cjk: bool, char_budget: int) -> str:
    sents = split_sentences(text, is_cjk)
    if not sents:
        return trim_to_whole_sentences(text, char_budget)

    # Drop verbatim repeats up front. Pages commonly restate the same line in
    # a lead-in, a bullet list and a closing recap; keeping only the first
    # occurrence costs one pass and shrinks the work MMR has to do later.
    seen, uniq = set(), []
    for s in sents:
        key = re.sub(r"\W+", "", s.lower())
        if key and key not in seen:
            seen.add(key)
            uniq.append(s)
    sents = uniq

    tok_sets = [set(tokenize(s, is_cjk)) for s in sents]
    freq = Counter()          # token frequency across the whole text (TF)
    doc_freq = Counter()      # number of sentences a token appears in (DF)
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
        return sents[0]

    LAMBDA = MMR_LAMBDA
    DUP_THRESHOLD = MMR_DUP_THRESHOLD
    remaining = scored[:]          # each entry: [base, idx, s, ts]
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
        # Incrementally update, against what was just chosen, both the
        # Jaccard similarity (penalty) and the containment ratio (hard drop)
        # of every remaining sentence.
        for i, (_, _, _, rts) in enumerate(remaining):
            union = rts | ts
            if union:
                sim = len(rts & ts) / len(union)
                if sim > max_sim[i]:
                    max_sim[i] = sim

    if not chosen:
        # Nothing fit the budget; one whole sentence beats half of one.
        return sents[0]

    chosen.sort(key=lambda x: x[0])
    joiner = "" if is_cjk else " "
    return joiner.join(s for _, s in chosen)


# ---------------------------------------------------------------- NMT translation

def translate_to_zhtw(text: str) -> str | None:
    if _tr is None:
        return None
    try:
        return _tr.translate(text, target="zh-TW", source="auto", session=get_session())
    except Exception as e:
        print(f"    translate failed: {e}")
        return None


# ---------------------------------------------------------------- Assembly

def strip_boilerplate(text: str) -> str:
    """Remove site furniture that body extraction keeps but which isn't
    article content: fixed promo blocks, related-article plugs, tag lines,
    filler lead-ins, and everything from a CUT_TO_END marker onwards
    (sign-offs, comment/review sections).

    The truncation is skipped when the marker sits within the first
    MIN_KEEP_AFTER_CUT characters, so a stray early match can't wipe out
    the whole body.
    """
    for block in REMOVE_BLOCKS:
        text = text.replace(block, "")
    text = REMOVE_PHRASE_RE.sub("", text)
    m = CUT_TO_END_RE.search(text)
    if m and m.start() >= MIN_KEEP_AFTER_CUT:
        text = text[:m.start()]
    return text.strip()


def summary_budget(content_len: int) -> int:
    return max(1, min(SUMMARY_MAX, int(content_len * SUMMARY_RATIO)))


def build_summary(content: str, source_type: str, has_table: bool = False) -> str:
    """Extractive summary plus any trailing marks.

    Sentences are picked by importance, not by position: each one is scored on
    TF-IDF weight, named-entity/number density and how near the top it sits,
    then selected with MMR so a near-duplicate of an already-chosen sentence
    loses out to a sentence that adds something new (see extractive_summary).
    Whichever sentences are chosen are kept whole and re-emitted in their
    original reading order.

    The marks (`↛` when only a meta description was available, the table note)
    are budgeted for before selection starts, so making room for them can never
    truncate the last sentence into a fragment.
    """
    content = re.sub(r"\((?:\d{1,2}:)?\d{1,2}:\d{2}\)\s*[:：]?", ": ", content)
    content = strip_boilerplate(content)
    if not content:
        return ""

    marks = []
    if has_table:
        marks.append(TABLE_NOTE)
    # Only a blurb was available, so mark the summary as not coming from the
    # article body itself.
    if source_type == "meta" and not any(FALLBACK_MARK in m for m in marks):
        marks.append(FALLBACK_MARK)
    suffix = (" " + " ".join(marks)) if marks else ""

    lang = detect_lang(content)
    budget = summary_budget(len(content))
    text_budget = max(1, budget - len(suffix))

    if lang == "zh-hant":
        summary = extractive_summary(content, True, text_budget)
    elif lang == "zh-hans":
        summary = _to_twp(extractive_summary(content, True, text_budget))
    elif lang == "ja":
        raw = extractive_summary(
            content, True, int(text_budget * FOREIGN_BUDGET_RATIO)
        )
        translated = translate_to_zhtw(raw) if TRANSLATE else None
        summary = _to_twp(translated) if translated else raw
    else:
        raw = extractive_summary(
            content, False, int(text_budget * FOREIGN_BUDGET_RATIO)
        )
        summary = None
        if TRANSLATE:
            translated = translate_to_zhtw(raw)
            if translated:
                summary = _to_twp(translated)
        if summary is None:
            summary = raw

    if not is_cjk_lang(detect_lang(summary)):
        summary = re.sub(r"\s+", " ", summary).strip()
    else:
        summary = re.sub(
            r"(?<=[\u4e00-\u9fff，。！？；：、（）「」])\s+|\s+(?=[\u4e00-\u9fff，。！？；：、（）「」])",
            "", summary)
        summary = re.sub(r"\s+", " ", summary).strip()

    # Translation and whitespace normalisation both change the length, so the
    # budget is enforced once more here — by dropping whole sentences.
    summary = trim_to_whole_sentences(summary, text_budget)
    return (summary + suffix) if summary else ""


# ---------------------------------------------------------------- Information-value scoring

SCORE_MARK = "novelty"


def fact_tokens(text: str):
    """Language-neutral set of fact carriers: entities (proper nouns/quotes)
    + numbers/amounts/percentages."""
    toks = set()
    low = (text or "").lower()
    for alias, canonical in VENDOR_ALIASES.items():
        if alias in low:
            toks.add(f"@{canonical}")     # prefix avoids colliding with regular tokens
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
    """A single article's own information density (0-100): fact-carrier
    concentration per unit length. Independent of the corpus, so it can be
    cached on its own."""
    is_cjk = detect_lang(text) != "other"
    sents = split_sentences(text, is_cjk)
    unit = len(text) if is_cjk else len(text.split())
    if not sents or not unit:
        return 0.0
    facts = sum(entity_count(x) for x in sents)
    return min(100.0 * facts / unit / INFO_VALUE_SATURATION, 1.0) * 100.0


def score_corpus_novelty(items):
    """Compute cross-article novelty for items that don't have a score yet;
    already-scored items are left frozen, but their facts still count toward
    the corpus baseline, so newly-arrived reprints can still be detected
    against them.

    Fields written: info_value (this article's own density), novelty
    (cross-article novelty, 0-100), unique_facts (facts unique across the
    whole corpus), duplicate_of (suspected source-of-reprint url, or null),
    value_adjusted (= info_value * (1 - overlap), corpus-adjusted total value).
    """
    scored = [it for it in items if isinstance(it, dict) and it.get("summary")]
    if not scored:
        return 0

    # Oldest publication date first. The corpus baseline (corpus_df) is built
    # from every scored item before any of them is written, so the numbers do
    # not depend on this order — but the write order does, and going
    # oldest-first means a run that is cut short has finished the backlog
    # rather than a random slice of it.
    scored.sort(key=lambda it: (
        _parse_ts(it.get("published_at")),
        _parse_ts(it.get("first_seen_at")),
        str(it.get("id") or ""),
    ))

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


# ---------------------------------------------------------------- Main flow

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
    p.add_argument("--time-budget-seconds", type=int, default=TIME_BUDGET_SECONDS,
                   help="Total run-time budget in seconds; stops the fetch "
                        "loop once 70%% of this has elapsed (0 = no limit), "
                        "leaving the rest of the budget for saving/scoring")
    return p


def load_items(path: str):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"], data
    return None, None


def strip_feed_content(items: list) -> int:
    """Remove every trace of feed_content.

    feed_content is scratch space: update_news.py writes it, this script is the
    only consumer, and once a summary exists there is nothing left to read it
    for. Anything still carrying the field by the end of a run either got its
    summary from somewhere else or couldn't be summarized from the feed copy at
    all — in both cases keeping up to FEED_CONTENT_MAX_CHARS per item in a
    committed file buys nothing.
    """
    dropped = 0
    for it in items:
        if isinstance(it, dict) and it.pop("feed_content", None) is not None:
            dropped += 1
    return dropped


def save_items(path: str, items: list, wrapper: dict | None) -> None:
    if wrapper is not None:
        wrapper["items"] = items
        wrapper["total_items"] = len(items)
        payload = wrapper
    else:
        payload = items
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory,
                               prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _parse_ts(value) -> float:
    if not value:
        return float("-inf")
    s = str(value).strip()
    try:
        from datetime import datetime
        s2 = s.replace("Z", "+00:00") if s.endswith("Z") else s
        return datetime.fromisoformat(s2).timestamp()
    except Exception:
        pass
    try:
        import email.utils as eut
        dt = eut.parsedate_to_datetime(s)
        return dt.timestamp() if dt else float("-inf")
    except Exception:
        return float("-inf")


def main(argv=None) -> int:
    global ITEMS_FILE, MAX_ITEMS, TRANSLATE, SUMMARY_RATIO, SUMMARY_MAX, RESCORE_ALL
    global TIME_BUDGET_SECONDS
    args = build_arg_parser().parse_args(argv)
    ITEMS_FILE = args.items_file
    MAX_ITEMS = args.max_items
    TRANSLATE = args.translate
    SUMMARY_RATIO = args.summary_ratio
    SUMMARY_MAX = args.summary_max
    RESCORE_ALL = args.rescore_all
    TIME_BUDGET_SECONDS = args.time_budget_seconds

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
    pending.sort(key=lambda it: _parse_ts(it.get("published_at")), reverse=True)

    print(f"Total items: {len(items)}, pending: {len(pending)}, "
          f"translate={'on' if TRANSLATE else 'off'} (newest-first)")
    if TIME_BUDGET_SECONDS > 0:
        print(f"Time budget: {TIME_BUDGET_SECONDS}s "
              f"(stop fetching after {TIME_BUDGET_SECONDS * TIME_BUDGET_STOP_RATIO:.0f}s elapsed)")

    start_time = time.monotonic()
    time_cut_off = False
    ok = blocked_n = failed = blank_n = junk_n = 0
    attempted = 0
    junk_hosts: set[str] = set()
    junk_skipped: Counter = Counter()

    # ---- Pass 1: feed-first hosts -------------------------------------------
    feed_first = [it for it in pending
                  if is_feed_first_host(it.get("url", ""))
                  and (it.get("feed_content") or "").strip()]
    if feed_first:
        print(f"\nFeed-first pass: {len(feed_first)} item(s) on "
              f"{', '.join(FEED_FIRST_HOSTS)} (not counted against MAX_ITEMS)")
        ff_ok = ff_failed = 0
        pending_save = 0
        for idx, it in enumerate(feed_first, 1):
            if TIME_BUDGET_SECONDS > 0:
                elapsed = time.monotonic() - start_time
                if elapsed > TIME_BUDGET_SECONDS * TIME_BUDGET_STOP_RATIO:
                    print(f"  time budget reached ({elapsed:.0f}s), "
                          f"{len(feed_first) - idx + 1} item(s) stay pending.")
                    time_cut_off = True
                    break
            if is_summary_skip_host(it.get("url") or ""):
                continue
            feed_html = it.get("feed_content") or ""
            content = feed_html.strip()
            source_type = "body" if len(content) >= MIN_USABLE_BODY else "meta"
            print(f"  [{idx}/{len(feed_first)}] "
                  f"({it.get('published_at') or 'no date'}) {it.get('title', '')[:60]}")
            print(f"      {it['url']}")
            try:
                summary = build_summary(content, source_type)
            except Exception as e:
                print(f"      summarize failed: {e}")
                ff_failed += 1
                continue
            if not summary:
                print("      empty after boilerplate removal, skipped")
                ff_failed += 1
                continue
            it["summary"] = summary
            set_thumbnail_from_feed(it, feed_html)
            it.pop("feed_content", None)
            ff_ok += 1
            pending_save += 1
            print(f"      ok (feed, {len(content)} chars)")
            if pending_save >= FEED_FIRST_SAVE_EVERY:
                save_items(ITEMS_FILE, items, wrapper)
                pending_save = 0
        if pending_save:
            save_items(ITEMS_FILE, items, wrapper)
        ok += ff_ok
        failed += ff_failed
        print(f"Feed-first pass done: ok={ff_ok}, failed={ff_failed}\n")

    # ---- Pass 2: everything else, one page fetch at a time ------------------
    pending = [it for it in pending if not it.get("summary")]
    for it in pending:
        if is_feed_first_host(it.get("url", "")):
            continue
        if attempted >= MAX_ITEMS:
            print(f"Reached MAX_ITEMS={MAX_ITEMS}, stopping.")
            break
        if time_cut_off:
            break
        if TIME_BUDGET_SECONDS > 0:
            elapsed = time.monotonic() - start_time
            if elapsed > TIME_BUDGET_SECONDS * TIME_BUDGET_STOP_RATIO:
                print(f"Time budget {TIME_BUDGET_STOP_RATIO:.0%} reached "
                      f"({elapsed:.0f}s elapsed) — stopping fetch loop, "
                      f"remaining {len(pending) - attempted} item(s) stay pending "
                      f"for next run. Proceeding to save + scoring.")
                time_cut_off = True
                break

        url = it["url"]

        host = host_of(url)
        if is_summary_skip_host(url):
            continue
        if host in junk_hosts:
            junk_skipped[host] += 1
            continue

        if is_douban_mark(url, it.get("title")):
            it["summary"] = BLANK_SUMMARY
            it.pop("feed_content", None)
            blank_n += 1
            save_items(ITEMS_FILE, items, wrapper)
            continue

        # YouTube videos: always summarize from subtitles instead of
        # fetching the page (video pages have no article body anyway).
        # If no matching subtitle is found yet, skip this item and leave it
        # pending until download_sub.py fetches one.
        if is_youtube_url(url):
            if not it.get("thumbnail"):
                thumb = youtube_thumbnail_url(url)
                if thumb:
                    it["thumbnail"] = thumb

            picked = pick_subtitle(it.get("id", ""))
            if not picked:
                continue
            attempted += 1
            path, orig_lang, sub_lang = picked
            print(f"[{attempted}/{min(len(pending), MAX_ITEMS)}] "
                  f"({it.get('published_at') or 'no date'}) {it.get('title', '')[:60]}")
            print(f"    {url}")
            print(f"    subtitle: {os.path.basename(path)} (orig={orig_lang}, sub={sub_lang})")

            text = vtt_to_text(path)
            if len(text) < MIN_CAPTION_CHARS:
                print(f"    no usable speech in subtitle ({len(text)} chars), "
                      f"marked blank")
                it["summary"] = BLANK_SUMMARY
                it.pop("feed_content", None)
                blank_n += 1
                save_items(ITEMS_FILE, items, wrapper)
                continue
            try:
                summary = build_summary(text, "body")
            except Exception as e:
                print(f"    summarize failed: {e}")
                failed += 1
                continue
            if not summary:
                print("    empty after boilerplate removal, skipped")
                failed += 1
                continue
            it["summary"] = summary
            it.pop("feed_content", None)
            ok += 1
            print(f"    ok (subtitle, {len(text)} chars)")
            save_items(ITEMS_FILE, items, wrapper)
            time.sleep(SLEEP_BETWEEN_ITEMS)
            continue

        if "techmeme.com" in url:
            title = (it.get("title") or "").strip()
            # Techmeme prefixes a headline with "Sources:", "Report:" or
            # "Documents:" when the story rests on reporting it has obtained
            # rather than on a public announcement — worth starring, and worth
            # reading the page for instead of settling for the headline.
            if title.startswith(TECHMEME_STAR_PREFIXES):
                it["star"] = True
                attempted += 1
                print(f"[{attempted}/{min(len(pending), MAX_ITEMS)}] "
                      f"({it.get('published_at') or 'no date'}) {title[:60]}")
                print(f"    {url}  (starred)")
                summary = ""
                tm_feed_html = it.get("feed_content") or ""
                tm_meta: dict = {}
                content, source_type, has_table = fetch_content(
                    url, feed_content=tm_feed_html, meta_out=tm_meta
                )
                if content:
                    try:
                        summary = build_summary(content, source_type, has_table)
                    except Exception as e:
                        print(f"    summarize failed: {e}")
                        summary = ""
                if summary:
                    it["summary"] = summary
                    if not it.get("thumbnail"):
                        if tm_meta.get("thumbnail"):
                            it["thumbnail"] = tm_meta["thumbnail"]
                        else:
                            set_thumbnail_from_feed(it, tm_feed_html)
                    it.pop("feed_content", None)
                    ok += 1
                    print(f"    ok ({source_type}, {len(content)} chars)")
                else:
                    translated = translate_to_zhtw(title)
                    if translated:
                        it["summary"] = _to_twp(translated) + " " + FALLBACK_MARK
                        ok += 1
                        print("    fell back to translated title")
                    else:
                        failed += 1
                        print("    no content and translation failed, kept pending.")
                save_items(ITEMS_FILE, items, wrapper)
                time.sleep(SLEEP_BETWEEN_ITEMS)
                continue

            translated = translate_to_zhtw(title)
            if translated:
                it["summary"] = _to_twp(translated) + " " + FALLBACK_MARK
                it.pop("feed_content", None)
            continue

        attempted += 1
        print(f"[{attempted}/{min(len(pending), MAX_ITEMS)}] "
              f"({it.get('published_at') or 'no date'}) {it.get('title', '')[:60]}")
        print(f"    {url}")

        feed_html = it.get("feed_content") or ""
        meta_out: dict = {}
        content, source_type, has_table = fetch_content(
            url, feed_content=feed_html, meta_out=meta_out
        )

        if source_type == "junk":
            junk_n += 1
            junk_hosts.add(host)
            print(f"    junk page -> skipped, {host} skipped for the rest "
                  f"of this run")
            continue
        if source_type in ("blocked", "gone"):
            it["summary"] = BLOCKED_SUMMARY if source_type == "blocked" else GONE_SUMMARY
            it.pop("feed_content", None)
            blocked_n += 1
            print(f"    {source_type} -> placeholder written")
            save_items(ITEMS_FILE, items, wrapper)
            time.sleep(SLEEP_BETWEEN_ITEMS)
            continue
        if not content:
            print("    fetch failed, skipped (kept pending for next run).")
            failed += 1
            continue

        try:
            summary = build_summary(content, source_type, has_table)
        except Exception as e:
            print(f"    summarize failed: {e}")
            failed += 1
            continue
        if not summary:
            print("    empty after boilerplate removal, skipped")
            failed += 1
            continue
        it["summary"] = summary
        if not it.get("thumbnail"):
            if meta_out.get("thumbnail"):
                it["thumbnail"] = meta_out["thumbnail"]
            else:
                set_thumbnail_from_feed(it, feed_html)
        it.pop("feed_content", None)

        ok += 1
        print(f"    ok ({source_type}, {len(content)} chars)")
        save_items(ITEMS_FILE, items, wrapper)
        time.sleep(SLEEP_BETWEEN_ITEMS)

    if junk_hosts:
        detail = ", ".join(f"{h}×{junk_skipped[h]}"
                           for h, _ in junk_skipped.most_common())
        print(f"Skipped {sum(junk_skipped.values())} item(s) on "
              f"{len(junk_hosts)} host(s) serving a template: {detail}"
              + (f"  (also: {', '.join(sorted(junk_hosts - set(junk_skipped)))})"
                 if junk_hosts - set(junk_skipped) else ""))
    print(f"Done. attempted={attempted}, ok={ok}, blocked={blocked_n}, "
          f"blank={blank_n}, junk={junk_n}, failed={failed}"
          f"{', stopped early: time budget reached' if time_cut_off else ''}")

    newly_scored = score_corpus_novelty(items)
    if newly_scored:
        print(f"Scored novelty for {newly_scored} new item(s); existing scores kept.")

    dropped = strip_feed_content(items)
    if dropped:
        print(f"Dropped feed_content from {dropped} item(s) (scratch data).")

    save_items(ITEMS_FILE, items, wrapper)

    leftover = sum(1 for it in items if isinstance(it, dict) and "feed_content" in it)
    if leftover:
        print(f"WARNING: {leftover} item(s) still carry feed_content after strip.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
