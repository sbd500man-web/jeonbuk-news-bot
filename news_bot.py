"""전북 뉴스 알림: 네이버 공식 API + 다음 뉴스 검색 페이지 → 텔레그램."""
from __future__ import annotations

import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

KEYWORDS = ["전북", "전북자치도", "전라북도", "전북 경찰", "전북 소방"]
KST = timezone(timedelta(hours=9))
STATE_PATH = Path("data/state.json")
LOOKBACK_HOURS = 24
FIRST_RUN_LIMIT = 5  # 포털별 첫 실행에 보낼 최대 기사 수
MAX_SEND_PER_RUN = 30  # 초과분은 다음 실행에서 전송
NAVER_MAX_PAGES = 10  # 검색어당 최대 1,000건
DAUM_MAX_PAGES = 10  # 검색어당 최대 약 100건
NO_NEWS_MESSAGE = "관련 키워드로 최근 10분간 올라온 기사가 없네요 ㅠㅠ, 그냥 웹서핑이나 하시죠"

# 앞뒤가 다른 글자와 붙은 '단독주택', '속보치', '특종보험' 등은 제외합니다.
# 붙여 쓰는 언론 표현만 별도로 허용합니다.
IMPORTANT_PATTERNS = {
    "속보": re.compile(r"(?<!\w)(?:긴급속보|속보)(?!\w)"),
    "단독": re.compile(r"(?<!\w)단독(?:보도|취재)?(?!\w)"),
    "특종": re.compile(r"(?<!\w)특종(?:보도|취재)?(?!\w)"),
}
IMPORTANT_ICONS = {"속보": "🚨", "단독": "🔴", "특종": "⭐"}
# 띄어 쓴 일반 표현도 제외합니다. [단독]처럼 명시한 기사 표시는 유지합니다.
NON_NEWS_PHRASES = re.compile(
    r"(?<!\w)단독\s+(?:주택|주거|가구|세대|명의|소유|상속|후보|입후보|출마|"
    r"공천|추천|선두|1위|우승|승리|대표|대표이사|운영|경영|입찰|응찰|계약|"
    r"개최|공연|콘서트|전시|전시회|출연|인터뷰|면담|회담|방문|행동|범행|"
    r"사고|처리|심리|판사|재판부|재판)|(?<!\w)특종\s+보험"
)


def important_labels(title, summary=""):
    """기존 전북 기사 필터를 통과한 제목·요약에서 중요 표시를 찾습니다."""
    text = NON_NEWS_PHRASES.sub(" ", title + "\n" + summary)
    return [label for label, pattern in IMPORTANT_PATTERNS.items() if pattern.search(text)]


class BotError(Exception):
    """토큰이나 인증 URL을 포함하지 않는 사용자용 오류."""


def clean(text):
    # <단독>을 HTML 태그로 오인해 지우지 않습니다.
    text = re.sub(r"<(속보|단독|특종)>", r"[\1]", text or "")
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def canonical_url(url):
    p = urlsplit(html.unescape(url))
    if p.scheme not in ("http", "https") or not p.hostname or p.username:
        raise BotError("기사 링크 형식이 올바르지 않습니다.")
    host = p.hostname.lower()
    # 네이버의 구형/신형 기사 링크를 같은 주소로 정규화합니다.
    q = dict(parse_qsl(p.query))
    if host.endswith(".naver.com") and q.get("oid") and q.get("aid"):
        return f"https://n.news.naver.com/mnews/article/{q['oid']}/{q['aid']}"
    pairs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")]
    scheme = "https" if host in ("v.daum.net", "news.naver.com", "n.news.naver.com") else p.scheme
    return urlunsplit((scheme, p.netloc.lower(), p.path, urlencode(sorted(pairs)), ""))


def article_id(url):
    return hashlib.sha256(canonical_url(url).encode()).hexdigest()


def request(url, *, headers=None, payload=None, label="요청"):
    """GET만 자동 재시도. 전송 POST는 중복 위험 때문에 429일 때만 재시도."""
    body = None if payload is None else json.dumps(payload).encode()
    h = {"User-Agent": "JeonbukNewsNotifier/1.0"}
    if payload is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    for attempt in range(3):
        try:
            with urlopen(Request(url, data=body, headers=h), timeout=25) as response:
                return response.read().decode("utf-8-sig")
        except HTTPError as exc:
            delay = 2 ** (attempt + 1)
            if exc.code == 429:
                try:
                    details = json.loads(exc.read())
                    delay = int(details.get("parameters", {}).get("retry_after", delay))
                except (ValueError, TypeError):
                    pass
            retryable = exc.code == 429 or (payload is None and exc.code >= 500)
            if retryable and attempt < 2 and delay <= 45:
                time.sleep(delay)
                continue
            raise BotError(f"{label}: HTTP {exc.code}. 인증·접근 제한·서비스 상태를 확인하세요.") from None
        except (URLError, OSError):
            if payload is None and attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
            raise BotError(f"{label}: 연결 실패 또는 시간 초과. 다음 실행에서 재시도합니다.") from None
    raise BotError(f"{label}: 재시도 한도 초과")


def telegram(message):
    token = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    response = json.loads(request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        payload={"chat_id": os.environ["TELEGRAM_CHAT_ID"].strip(),
                 "text": message,
                 "link_preview_options": {"is_disabled": True}}, label="텔레그램 전송"))
    if not response.get("ok"):
        raise BotError("텔레그램이 전송을 거절했습니다. 토큰·채팅 ID·봇 차단 여부를 확인하세요.")


def make_article(source, title, summary, url, date, keyword, original=None):
    title, summary = clean(title), clean(summary)
    normalized = re.sub(r"\s+", "", title + " " + summary)
    matches = [k for k in KEYWORDS if re.sub(r"\s+", "", k) in normalized]
    if not matches:
        return None
    return {"id": article_id(original or url), "source": source,
            "title": title, "summary": summary, "url": canonical_url(url),
            "published": date.astimezone(timezone.utc).isoformat(),
            "keywords": matches}


def collect_naver(now, warnings):
    cutoff = now - timedelta(hours=LOOKBACK_HOURS)
    headers = {"X-NCP-APIGW-API-KEY-ID": os.environ["NAVER_CLIENT_ID"].strip(),
               "X-NCP-APIGW-API-KEY": os.environ["NAVER_CLIENT_SECRET"].strip()}
    articles = {}
    for keyword in KEYWORDS:
        for page in range(NAVER_MAX_PAGES):
            query = urlencode({"query": keyword, "sort": "date", "display": 100,
                               "start": page * 100 + 1, "format": "json"})
            data = json.loads(request("https://naverapihub.apigw.ntruss.com/search/v1/news?" + query,
                                      headers=headers, label="네이버 검색"))
            if not isinstance(data.get("items"), list):
                raise BotError("네이버 검색 응답 형식이 바뀌었습니다.")
            items = data["items"]
            dates = []
            for item in items:
                date = parsedate_to_datetime(item["pubDate"])
                dates.append(date)
                if not cutoff <= date <= now + timedelta(minutes=10):
                    continue
                a = make_article("네이버", item["title"], item.get("description", ""),
                                 item["link"], date, keyword, item.get("originallink"))
                if a:
                    articles[a["id"]] = a
            if len(items) < 100 or (dates and min(dates) < cutoff):
                break
            if page == NAVER_MAX_PAGES - 1:
                warnings.append(f"네이버 '{keyword}': 조회 상한 도달. 일부 기사 누락 가능.")
            time.sleep(0.3)
    return list(articles.values())


def daum_date(text, now):
    text = re.sub(r"\s+", " ", text).strip()
    if "방금" in text:
        return now
    m = re.search(r"(\d+)\s*(초|분|시간|일)\s*전", text)
    if m:
        seconds = int(m[1]) * {"초": 1, "분": 60, "시간": 3600, "일": 86400}[m[2]]
        return now - timedelta(seconds=seconds)
    m = re.search(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})\.?", text)
    if m:
        return datetime(*map(int, m.groups()), tzinfo=KST)
    raise BotError("다음 기사 날짜를 읽지 못했습니다. 검색 페이지 구조 확인이 필요합니다.")


def parse_daum(page_html, keyword, now):
    soup = BeautifulSoup(page_html, "html.parser")
    cards = soup.select("#dnsColl li[data-docid]")
    if not cards:
        empty = soup.select_one("#noResult, .no_result, .wrap_no_result")
        if empty and ("검색결과가 없습니다" in empty.get_text(" ", strip=True)
                      or "검색 결과가 없습니다" in empty.get_text(" ", strip=True)):
            return [], []
        # 빈 페이지와 차단/구조 변경을 혼동해 조용히 정상 종료하지 않습니다.
        raise BotError("다음 검색 결과를 읽지 못했습니다. 차단 또는 페이지 구조 변경 가능성이 있습니다.")
    articles, dates = [], []
    for card in cards:
        anchor = card.select_one(".item-title a[href]")
        stamp = card.select_one(".gem-subinfo")
        summary = card.select_one(".conts-desc")
        if anchor is None or stamp is None:
            raise BotError("다음 뉴스 제목·날짜 구조가 바뀌었습니다.")
        date = daum_date(stamp.get_text(" ", strip=True), now)
        dates.append(date)
        a = make_article("다음", anchor.get_text("", strip=True),
                         summary.get_text("", strip=True) if summary else "",
                         anchor["href"], date, keyword)
        if a and now - timedelta(hours=LOOKBACK_HOURS) <= date <= now + timedelta(minutes=10):
            articles.append(a)
    return articles, dates


def collect_daum(now, warnings):
    articles = {}
    for keyword in KEYWORDS:
        prior = None
        for page in range(1, DAUM_MAX_PAGES + 1):
            query = urlencode({"w": "news", "q": keyword, "sort": "recency", "p": page})
            page_html = request("https://search.daum.net/search?" + query, label="다음 검색")
            batch, dates = parse_daum(page_html, keyword, now)
            signature = tuple(a["id"] for a in batch)
            if signature and signature == prior:
                warnings.append(f"다음 '{keyword}': 같은 페이지 반복. 일부 기사 누락 가능.")
                break
            prior = signature
            articles.update({a["id"]: a for a in batch})
            if len(dates) < 10 or min(dates) < now - timedelta(hours=LOOKBACK_HOURS):
                break
            if page == DAUM_MAX_PAGES:
                warnings.append(f"다음 '{keyword}': 조회 상한 도달. 일부 기사 누락 가능.")
            time.sleep(1)
        time.sleep(1)
    return list(articles.values())


def fresh_state():
    return {"version": 1, "initialized": [], "seen": {}, "pending": {}, "notices": {}}


def load_state(path=STATE_PATH):
    if not path.exists():
        return fresh_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if (state["version"] != 1 or not isinstance(state["initialized"], list)
                or not all(isinstance(state[k], dict) for k in ("seen", "pending", "notices"))):
            raise ValueError()
        for article in state["pending"].values():
            for key in ("id", "source", "title", "summary", "url", "published", "keywords"):
                article[key]
            for flag in ("normal_sent", "priority_sent"):
                if flag in article and not isinstance(article[flag], bool):
                    raise ValueError()
        return state
    except (ValueError, KeyError, TypeError):
        raise BotError("data/state.json이 손상되었습니다. 중복 발송 방지를 위해 중단합니다.") from None


def save_state(state, path=STATE_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def enqueue(state, source, articles, now):
    unique = {a["id"]: a for a in articles}
    ordered = sorted(unique.values(), key=lambda a: a["published"], reverse=True)
    initial = source not in state["initialized"]
    for index, a in enumerate(ordered):
        key = a["id"]
        if key in state["seen"] or key in state["pending"]:
            continue
        if initial and index >= FIRST_RUN_LIMIT:
            state["seen"][key] = now.isoformat()
        else:
            state["pending"][key] = a
    if initial:
        state["initialized"].append(source)


def format_article(a):
    date = datetime.fromisoformat(a["published"]).astimezone(KST).strftime("%m/%d %H:%M")
    return (f"[전북 뉴스 · {a['source']}]\n{a['title'][:300]}\n\n"
            f"{a['summary'][:500]}\n\n"
            f"검색 결과 표시 시각: {date} (한국시간)\n"
            f"키워드: {', '.join(a['keywords'])}\n{a['url'][:1500]}")


def format_priority(a, labels):
    icons = "".join(IMPORTANT_ICONS[label] for label in labels)
    return (f"{icons} [중요 알림 · {' / '.join(labels)}] {icons}\n"
            f"{format_article(a)}")


def flush_pending(state, now, sender=telegram, saver=save_state):
    sent = 0
    # 중요한 기사부터 처리하되, 동일 등급에서는 오래된 기사부터 보냅니다.
    ordered = sorted(state["pending"].values(), key=lambda a: (
        not bool(important_labels(a["title"], a["summary"])), a["published"]))
    for a in ordered[:MAX_SEND_PER_RUN]:
        labels = important_labels(a["title"], a["summary"])
        if labels and not a.get("priority_sent", False):
            sender(format_priority(a, labels))
            a["priority_sent"] = True
            saver(state)
            time.sleep(1.1)
        if not a.get("normal_sent", False):
            sender(format_article(a))
            a["normal_sent"] = True
            saver(state)
            time.sleep(1.1)
        # 각각의 성공을 저장한 뒤, 필요한 알림이 모두 끝난 기사를 완료 처리합니다.
        state["seen"][a["id"]] = now.isoformat()
        del state["pending"][a["id"]]
        saver(state)
        sent += 1
    return sent


def notice(state, key, message, now):
    day = now.astimezone(KST).date().isoformat()
    if state["notices"].get(key) != day:
        telegram(message)
        state["notices"][key] = day
        save_state(state)


def notify_no_news(state, now, sent, issues, recent_articles):
    """정상 검색·전송 완료 후에만, 같은 10분 구간에는 한 번만 안내합니다."""
    if sent or state["pending"] or issues or recent_articles:
        return False
    slot = now.replace(minute=(now.minute // 10) * 10, second=0, microsecond=0).isoformat()
    if state.get("last_empty_notice_slot") == slot:
        return False
    telegram(NO_NEWS_MESSAGE)
    state["last_empty_notice_slot"] = slot
    save_state(state)
    return True


def main():
    required = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"]
    if os.getenv("TEST_ONLY", "false").lower() == "true":
        required = required[:2]
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        raise BotError("GitHub Secrets에 다음 값을 등록하세요: " + ", ".join(missing))
    if os.getenv("TEST_ONLY", "false").lower() == "true":
        telegram("전북 뉴스 봇 연결 테스트 성공! 이제 test_only 체크를 해제하고 실행하세요.")
        return 0
    now = datetime.now(timezone.utc)
    state = load_state()
    if not state.get("welcome_sent"):
        telegram("전북 뉴스 봇이 시작됐습니다. 포털별 최근 24시간 내 최신 기사 최대 5건을 먼저 보내고, 이후 새로 발견한 기사를 알려드립니다.")
        state["welcome_sent"] = True
        save_state(state)
    warnings, failures = [], []
    recent_articles = False
    for source, collector in (("네이버", collect_naver), ("다음", collect_daum)):
        try:
            articles = collector(now, warnings)
            recent_articles = recent_articles or any(
                datetime.fromisoformat(a["published"]) >= now - timedelta(minutes=10)
                for a in articles)
            enqueue(state, source, articles, now)
            save_state(state)
            print(f"{source}: 최근 기사 {len(articles)}건 확인")
        except Exception as exc:
            # 원본 예외에는 인증 URL이 포함될 수 있어 그대로 출력하지 않습니다.
            message = str(exc) if isinstance(exc, BotError) else f"{source}: 응답 처리 실패 ({type(exc).__name__})"
            failures.append(message)
            print(message, file=sys.stderr)
    sent = flush_pending(state, now)
    expiry = (now - timedelta(days=30)).isoformat()
    state["seen"] = {k: date for k, date in state["seen"].items() if date >= expiry}
    save_state(state)
    print(f"처리 완료 기사 {sent}건 / 다음 실행 대기 {len(state['pending'])}건")
    notify_no_news(state, now, sent, failures + warnings, recent_articles)
    if failures or warnings:
        for message in warnings:
            print(message, file=sys.stderr)
        notice(state, "collection_warning", "전북 뉴스 봇 점검 알림\n" +
               "\n".join(failures + warnings)[:2500] +
               "\n정상 수집된 기사는 계속 보냅니다. GitHub Actions 실행 기록을 확인하세요.", now)
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BotError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"예상하지 못한 오류: {type(exc).__name__}. 인증정보 보호를 위해 상세 URL은 출력하지 않습니다.", file=sys.stderr)
        sys.exit(1)
