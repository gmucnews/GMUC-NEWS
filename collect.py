#!/usr/bin/env python3
"""
구미 뉴스 모니터링 수집기

- 네이버 뉴스 검색 API + 다음 뉴스 검색 결과를 키워드별로 수집
- 같은 기사(네이버/다음 중복) 합치기, 같은 보도자료를 받아쓴 기사끼리 묶기
- 제목/요약 단어 기반 논조(긍정/중립/부정) 표시
- 새 기사 텔레그램/이메일 알림
- 결과를 data/news.json, data/status.json 으로 저장 (index.html 이 읽음)

설정은 config.json, 비밀값은 환경변수(GitHub Secrets)로 받습니다.
"""
import hashlib
import html
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

KST = ZoneInfo("Asia/Seoul")
ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.json")
NEWS_PATH = os.path.join(ROOT, "data", "news.json")
STATUS_PATH = os.path.join(ROOT, "data", "status.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

# 도메인 → 언론사 이름 (config.json 의 press_map 으로 추가/덮어쓰기 가능)
DEFAULT_PRESS = {
    "yna.co.kr": "연합뉴스", "newsis.com": "뉴시스", "news1.kr": "뉴스1",
    "imaeil.com": "매일신문", "yeongnam.com": "영남일보", "idaegu.co.kr": "대구일보",
    "idaegu.com": "대구신문", "kyongbuk.co.kr": "경북일보", "kbmaeil.com": "경북매일",
    "hidomin.com": "경북도민일보", "dkilbo.com": "대경일보", "ksmnews.co.kr": "경상매일신문",
    "newsmin.co.kr": "뉴스민", "tbc.co.kr": "TBC", "dgmbc.com": "대구MBC",
    "kbs.co.kr": "KBS", "imbc.com": "MBC", "sbs.co.kr": "SBS", "ytn.co.kr": "YTN",
    "jtbc.co.kr": "JTBC", "mbn.co.kr": "MBN", "ichannela.com": "채널A", "tvchosun.com": "TV조선",
    "chosun.com": "조선일보", "joongang.co.kr": "중앙일보", "donga.com": "동아일보",
    "hani.co.kr": "한겨레", "khan.co.kr": "경향신문", "hankookilbo.com": "한국일보",
    "kmib.co.kr": "국민일보", "seoul.co.kr": "서울신문", "segye.com": "세계일보",
    "munhwa.com": "문화일보", "mk.co.kr": "매일경제", "hankyung.com": "한국경제",
    "sedaily.com": "서울경제", "edaily.co.kr": "이데일리", "asiae.co.kr": "아시아경제",
    "fnnews.com": "파이낸셜뉴스", "heraldcorp.com": "헤럴드경제", "mt.co.kr": "머니투데이",
    "newspim.com": "뉴스핌", "nocutnews.co.kr": "노컷뉴스", "ohmynews.com": "오마이뉴스",
    "pressian.com": "프레시안", "dailian.co.kr": "데일리안", "ajunews.com": "아주경제",
    "etnews.com": "전자신문", "zdnet.co.kr": "지디넷코리아", "inews24.com": "아이뉴스24",
    "kukinews.com": "쿠키뉴스", "breaknews.com": "브레이크뉴스", "sisajournal.com": "시사저널",
    "newdaily.co.kr": "뉴데일리", "wikitree.co.kr": "위키트리", "dt.co.kr": "디지털타임스",
    "naeil.com": "내일신문", "kpinews.kr": "KPI뉴스", "kwnews.co.kr": "강원일보",
}

TAG_RE = re.compile(r"<[^>]+>")
LEAD_TAG_RE = re.compile(r"^\s*[\[【(<〈][^\]】)>〉]{1,12}[\]】)>〉]\s*")
REL_TIME_RE = re.compile(r"(\d+)\s*(분|시간|일)\s*전")
ABS_DATE_RE = re.compile(r"(20\d{2})\.\s*(\d{1,2})\.\s*(\d{1,2})\.?")


def now_kst():
    return datetime.now(KST)


def log(*a):
    print(f"[{now_kst():%H:%M:%S}]", *a, flush=True)


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


# ---------- 텍스트/URL 정리 ----------

def clean_text(s):
    if not s:
        return ""
    s = html.unescape(TAG_RE.sub("", s))
    return re.sub(r"\s+", " ", s).strip()


def strip_host(host):
    host = host.lower().split(":")[0]
    for pre in ("www.", "m.", "mobile."):
        if host.startswith(pre):
            host = host[len(pre):]
    return host


def norm_url(u):
    try:
        p = urlparse(u.strip())
    except Exception:
        return u
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")]
    q.sort()
    return urlunparse(("https", strip_host(p.netloc), p.path.rstrip("/"), "", urlencode(q), ""))


def strip_lead_tags(t):
    prev = None
    while prev != t:
        prev, t = t, LEAD_TAG_RE.sub("", t)
    return t


def norm_title(t):
    return re.sub(r"[^0-9a-z가-힣]", "", strip_lead_tags(t).lower())


def press_from_url(url, press_map):
    host = strip_host(urlparse(url).netloc)
    for dom, name in press_map.items():
        if host == dom or host.endswith("." + dom):
            return name
    return host or "알 수 없음"


def make_id(url):
    return hashlib.sha1(norm_url(url).encode("utf-8")).hexdigest()[:16]


# ---------- 수집: 네이버 ----------

def fetch_naver(query, cfg, cid, secret):
    out = []
    for page in range(cfg.get("naver_pages", 1)):
        display = min(int(cfg.get("naver_display", 100)), 100)
        r = requests.get(
            "https://openapi.naver.com/v1/search/news.json",
            params={"query": query, "display": display, "start": 1 + page * display, "sort": "date"},
            headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret},
            timeout=15,
        )
        if r.status_code != 200:
            raise RuntimeError(f"네이버 API {r.status_code}: {r.text[:150]}")
        items = r.json().get("items", [])
        for it in items:
            link = it.get("link", "")
            orig = it.get("originallink") or link
            try:
                pub = parsedate_to_datetime(it["pubDate"]).astimezone(KST)
            except Exception:
                pub = now_kst()
            out.append({
                "title": clean_text(it.get("title")),
                "url": orig,
                "portal_url": link if "naver.com" in link else "",
                "desc": clean_text(it.get("description")),
                "published": pub,
                "time_exact": True,
                "press": None,
                "source": "naver",
            })
        if len(items) < display:
            break
    return out


# ---------- 수집: 다음 ----------
# 다음은 공식 뉴스 API가 없어 검색 결과 페이지를 읽습니다.
# 다음이 페이지 구조를 바꾸면 이 부분만 손보면 됩니다.

def parse_daum_time(text, now):
    if "방금" in text:
        return now
    m = REL_TIME_RE.search(text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"분": timedelta(minutes=n), "시간": timedelta(hours=n), "일": timedelta(days=n)}[unit]
        return now - delta
    m = ABS_DATE_RE.search(text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), 9, 0, tzinfo=KST)
        except ValueError:
            return None
    return None


def _first(el, selectors):
    for sel in selectors:
        found = el.select_one(sel)
        if found:
            return found
    return None


def parse_daum_item(li, now):
    a = _first(li, [".item-title a", "strong.tit-g a", ".tit-g a", "a.tit_main", "a.f_link_b"])
    if not a:
        cands = [x for x in li.find_all("a", href=True)
                 if x["href"].startswith("http") and len(x.get_text(strip=True)) >= 10]
        a = cands[0] if cands else None
    if not a:
        return None
    title = clean_text(a.get_text(" "))
    if len(title) < 6:
        return None
    desc_el = _first(li, [".item-contents p", "p.conts-desc", ".conts-desc", "p.desc", ".desc"])
    press_el = _first(li, [".item-writer .txt_info", ".c-tit-doc .txt_info", ".tit_item .txt_info",
                           "a.txt_info", "span.txt_info"])
    text = li.get_text(" ", strip=True)
    pub = parse_daum_time(text, now)
    href = a["href"]
    return {
        "title": title,
        "url": href,
        "portal_url": href if "daum.net" in href else "",
        "desc": clean_text(desc_el.get_text(" ")) if desc_el else "",
        "published": pub or now,
        "time_exact": False,
        "press": clean_text(press_el.get_text()) if press_el else None,
        "source": "daum",
    }


def parse_daum_html(page_html, now):
    soup = BeautifulSoup(page_html, "html.parser")
    items = soup.select("ul.c-list-basic > li") or soup.select("li[data-docid]") or soup.select("div.c-item-doc")
    out = []
    for li in items:
        it = parse_daum_item(li, now)
        if it:
            out.append(it)
    if not out:  # 구조가 바뀐 경우를 위한 최소한의 대비책
        for a in soup.select("a[href*='v.daum.net/v/']"):
            t = clean_text(a.get_text(" "))
            if len(t) >= 10:
                box = a.find_parent("li") or a.parent
                pub = parse_daum_time(box.get_text(" ", strip=True), now) if box else None
                out.append({"title": t, "url": a["href"], "portal_url": a["href"], "desc": "",
                            "published": pub or now, "time_exact": False, "press": None, "source": "daum"})
    return out


def fetch_daum(query, cfg):
    out, now = [], now_kst()
    for page in range(1, int(cfg.get("daum_pages", 2)) + 1):
        r = requests.get(
            "https://search.daum.net/search",
            params={"w": "news", "q": query, "sort": "recency", "p": page, "DA": "STC"},
            headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9", "Referer": "https://search.daum.net/"},
            timeout=15,
        )
        if r.status_code != 200:
            raise RuntimeError(f"다음 검색 {r.status_code}")
        got = parse_daum_html(r.text, now)
        if not got and page == 1:
            raise RuntimeError("다음 검색 결과를 읽지 못했습니다 (페이지 구조 변경 가능성)")
        out.extend(got)
        time.sleep(1)
    return out


# ---------- 판정 ----------

def queries_of(kw):
    q = kw.get("query") or kw["name"]
    return q if isinstance(q, list) else [q]


def is_relevant(a, kw):
    compact = (a["title"] + " " + a["desc"]).replace(" ", "")
    terms = kw.get("must_include") or queries_of(kw)
    if kw.get("require_match", True) and not any(t.replace(" ", "") in compact for t in terms):
        return False
    return not any(ex.replace(" ", "") in compact for ex in kw.get("exclude", []))


def judge_sentiment(title, desc, words):
    score = 0.0
    for w in words.get("positive", []):
        score += 2 * title.count(w) + desc.count(w)
    for w in words.get("negative", []):
        score -= 3 * title.count(w) + 1.5 * desc.count(w)
    if score <= -2:
        return "negative"
    if score >= 2:
        return "positive"
    return "neutral"


def bigrams(s):
    s = re.sub(r"[^0-9a-z가-힣]", "", s.lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def assign_clusters(records, strip_terms, threshold=0.55, window_hours=72):
    """같은 보도자료를 받아 쓴 기사끼리 묶음. cluster = 가장 먼저 나온 기사의 id"""
    recs = sorted(records, key=lambda r: r["published"])
    grams = []
    for r in recs:
        t = strip_lead_tags(r["title"])
        for term in strip_terms:
            t = t.replace(term, " ")
        grams.append(bigrams(t))
    parent = list(range(len(recs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    window = timedelta(hours=window_hours)
    lo = 0
    for i in range(len(recs)):
        while recs[i]["published"] - recs[lo]["published"] > window:
            lo += 1
        gi = grams[i]
        if len(gi) < 6:
            continue
        for j in range(lo, i):
            gj = grams[j]
            if len(gj) < 6:
                continue
            inter = len(gi & gj)
            if inter and 2 * inter / (len(gi) + len(gj)) >= threshold:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[max(ri, rj)] = min(ri, rj)
    for i, r in enumerate(recs):
        r["cluster"] = recs[find(i)]["id"]


# ---------- 병합 ----------

def build_indexes(db):
    url_idx, title_idx = {}, {}
    for r in db.values():
        url_idx[r["id"]] = r["id"]
        for al in r.get("alias", []):
            url_idx[al] = r["id"]
        title_idx.setdefault(norm_title(r["title"]), []).append(r["id"])
    return url_idx, title_idx


def merge(db, url_idx, title_idx, a, kw_name):
    aid = make_id(a["url"])
    rec = db.get(url_idx.get(aid, ""))
    if rec is None:
        tkey = norm_title(a["title"])
        if len(tkey) >= 8:
            for other in title_idx.get(tkey, []):
                o = db.get(other)
                if o and (not a["press"] or not o["press"] or a["press"] == o["press"]):
                    rec = o
                    break
        if rec is not None and aid != rec["id"]:
            rec.setdefault("alias", []).append(aid)
            url_idx[aid] = rec["id"]
    if rec is not None:
        if a["source"] not in rec["sources"]:
            rec["sources"].append(a["source"])
        if kw_name not in rec["keywords"]:
            rec["keywords"].append(kw_name)
        if a["time_exact"] and not rec["time_exact"]:
            rec["published"], rec["time_exact"] = a["published"], True
        if len(a["desc"]) > len(rec["desc"]):
            rec["desc"] = a["desc"][:240]
        if a["portal_url"] and not rec.get("portal_url"):
            rec["portal_url"] = a["portal_url"]
        return rec, False
    rec = {
        "id": aid,
        "title": a["title"],
        "url": a["url"],
        "portal_url": a["portal_url"],
        "press": a["press"],
        "desc": a["desc"][:240],
        "published": a["published"],
        "time_exact": a["time_exact"],
        "first_seen": now_kst(),
        "sources": [a["source"]],
        "keywords": [kw_name],
    }
    db[aid] = rec
    url_idx[aid] = aid
    title_idx.setdefault(norm_title(a["title"]), []).append(aid)
    return rec, True


# ---------- 알림 ----------

def build_alert_text(items, cfg, site_url):
    title = cfg.get("site_title", "뉴스 모니터링")
    alert_kws = {k["name"] for k in cfg["keywords"] if k.get("alert")}
    lines = [f"📰 {title} 새 기사 {len(items)}건"]
    for r in items[:15]:
        mark = "⚠️ " if r["sentiment"] == "negative" else ""
        kws = ", ".join(k for k in r["keywords"] if k in alert_kws)
        lines.append(f"\n{mark}[{kws}] {r['press']}\n{r['title']}\n{r['url']}")
    if len(items) > 15:
        lines.append(f"\n외 {len(items) - 15}건")
    if site_url:
        lines.append(f"\n전체 보기: {site_url}")
    return "\n".join(lines)


def pick_alert_items(records, new_ids, cfg):
    alert_kws = {k["name"] for k in cfg["keywords"] if k.get("alert")}
    by_cluster = {}
    for r in records:
        by_cluster.setdefault(r["cluster"], []).append(r)
    picked, seen = [], set()
    for r in sorted(records, key=lambda x: x["published"], reverse=True):
        if r["id"] not in new_ids or not (set(r["keywords"]) & alert_kws) or r["cluster"] in seen:
            continue
        if cfg.get("alert_only_new_stories", True) and any(m["id"] not in new_ids for m in by_cluster[r["cluster"]]):
            continue  # 이미 알린 보도를 다른 매체가 받아 쓴 경우는 건너뜀
        seen.add(r["cluster"])
        picked.append(r)
    return picked


def send_telegram(text):
    token, chats = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chats:
        return
    for chat in [c.strip() for c in chats.split(",") if c.strip()]:
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              data={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": "true"},
                              timeout=15)
            log("텔레그램", chat, r.status_code)
        except Exception as e:
            log("텔레그램 실패", e)


def send_email(text, subject):
    host, user, pw, to = (os.getenv(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASS", "MAIL_TO"))
    if not (host and user and pw and to):
        return
    port = int(os.getenv("SMTP_PORT", "465"))
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, user, to
    try:
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            s = smtplib.SMTP(host, port, timeout=20)
            s.starttls()
        s.login(user, pw)
        s.sendmail(user, [t.strip() for t in to.split(",")], msg.as_string())
        s.quit()
        log("이메일 발송 완료")
    except Exception as e:
        log("이메일 실패", e)


# ---------- 직렬화 ----------

def to_json_rec(r):
    out = dict(r)
    out["published"] = r["published"].isoformat(timespec="minutes")
    out["first_seen"] = r["first_seen"].isoformat(timespec="minutes")
    return out


def from_json_rec(r):
    r = dict(r)
    r["published"] = datetime.fromisoformat(r["published"])
    r["first_seen"] = datetime.fromisoformat(r["first_seen"])
    return r


# ---------- 메인 ----------

def main():
    cfg = load_json(CONFIG_PATH)
    if not cfg:
        sys.exit("config.json 을 찾을 수 없습니다")
    press_map = {**DEFAULT_PRESS, **cfg.get("press_map", {})}
    kw_names = [k["name"] for k in cfg["keywords"]]
    now = now_kst()
    cutoff = now - timedelta(days=int(cfg.get("retention_days", 30)))

    old = load_json(NEWS_PATH)
    first_run = old is None
    db = {}
    for r in (old or {}).get("articles", []):
        r = from_json_rec(r)
        r["keywords"] = [k for k in r["keywords"] if k in kw_names]
        if r["keywords"] and r["published"] >= cutoff:
            db[r["id"]] = r
    url_idx, title_idx = build_indexes(db)

    cid, secret = os.getenv("NAVER_CLIENT_ID"), os.getenv("NAVER_CLIENT_SECRET")
    status = {"checked_at": now.isoformat(timespec="minutes"), "sources": {}, "keywords": {}}
    src_state = {
        "naver": {"ok": bool(cid and secret), "count": 0,
                  "error": "" if cid and secret else "네이버 API 키가 설정되지 않았습니다"},
        "daum": {"ok": bool(cfg.get("daum_enabled", True)), "count": 0,
                 "error": "" if cfg.get("daum_enabled", True) else "사용 안 함"},
    }
    new_ids = set()

    for kw in cfg["keywords"]:
        name, fetched = kw["name"], []
        for q in queries_of(kw):
            if cid and secret:
                try:
                    got = fetch_naver(q, cfg, cid, secret)
                    fetched += got
                    src_state["naver"]["count"] += len(got)
                except Exception as e:
                    src_state["naver"].update(ok=False, error=str(e)[:200])
                    log("네이버 오류", q, e)
            if cfg.get("daum_enabled", True):
                try:
                    got = fetch_daum(q, cfg)
                    fetched += got
                    src_state["daum"]["count"] += len(got)
                except Exception as e:
                    src_state["daum"].update(ok=False, error=str(e)[:200])
                    log("다음 오류", q, e)
        added = 0
        for a in fetched:
            if a["published"] < cutoff or not is_relevant(a, kw):
                continue
            a["press"] = a["press"] or press_from_url(a["url"], press_map)
            rec, is_new = merge(db, url_idx, title_idx, a, name)
            if is_new:
                new_ids.add(rec["id"])
                added += 1
        status["keywords"][name] = {"fetched": len(fetched), "new": added}
        log(f"{name}: 수집 {len(fetched)}건, 새 기사 {added}건")

    records = list(db.values())
    words = cfg.get("sentiment", {})
    for r in records:
        r["sentiment"] = judge_sentiment(r["title"], r["desc"], words)
    strip_terms = sorted({t for k in cfg["keywords"] for t in queries_of(k)}, key=len, reverse=True)
    assign_clusters(records, strip_terms, float(cfg.get("cluster_threshold", 0.55)))

    if not first_run and new_ids:
        items = pick_alert_items(records, new_ids, cfg)
        if items:
            text = build_alert_text(items, cfg, os.getenv("SITE_URL", ""))
            send_telegram(text)
            send_email(text, f"[{cfg.get('site_title', '뉴스 모니터링')}] 새 기사 {len(items)}건")

    records.sort(key=lambda r: r["published"], reverse=True)
    articles = [to_json_rec(r) for r in records]
    meta_keywords = [{"name": k["name"], "alert": bool(k.get("alert"))} for k in cfg["keywords"]]
    changed = (first_run
               or old.get("articles") != articles
               or old.get("keywords") != meta_keywords
               or old.get("site_title") != cfg.get("site_title"))
    if changed:
        save_json(NEWS_PATH, {
            "site_title": cfg.get("site_title", "뉴스 모니터링"),
            "updated_at": now.isoformat(timespec="minutes"),
            "retention_days": int(cfg.get("retention_days", 30)),
            "keywords": meta_keywords,
            "articles": articles,
        })
    status["sources"] = src_state
    status["new_count"] = len(new_ids)
    status["total"] = len(articles)
    save_json(STATUS_PATH, status)
    log(f"완료: 전체 {len(articles)}건, 새 기사 {len(new_ids)}건, 파일 갱신 {'예' if changed else '아니오'}")


if __name__ == "__main__":
    main()
