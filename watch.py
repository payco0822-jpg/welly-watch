#!/usr/bin/env python3
"""웰리트립(https://www.welly.rest/) 변경 감시 → 텔레그램 알림.

- 외부 라이브러리 없이 파이썬 표준 라이브러리만 사용한다.
- 페이지에서 '링크(카드) 단위 문구'와 '그 외 문구'를 뽑아 직전 상태(state.json)와 비교한다.
- 달라진 부분만 텔레그램으로 보낸다.

환경 변수
  TELEGRAM_BOT_TOKEN  텔레그램 봇 토큰 (없으면 화면에만 출력)
  TELEGRAM_CHAT_ID    알림을 받을 채팅 ID
  WATCH_URL           감시할 주소 (기본: https://www.welly.rest/)
  WATCH_HTML_FILE     테스트용. 지정하면 사이트 대신 이 HTML 파일을 읽는다.
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

URL = os.environ.get("WATCH_URL", "https://www.welly.rest/")
STATE_FILE = Path(__file__).with_name("state.json")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# 비교에서 제외할 문구 (로딩 표시 등 내용과 무관한 것)
IGNORE_TEXTS = {"Loading...", "Loading…", "로딩 중", "로딩중"}
# 이 개수보다 적게 뽑히면 페이지를 제대로 못 읽은 것으로 보고 비교하지 않는다.
MIN_TEXT_COUNT = 10
# 연속 실패가 이 횟수에 도달하면 한 번 알린다. (30분 간격 기준 6회 = 약 3시간)
FAIL_ALERT_AT = 6
SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "head"}


class Extractor(HTMLParser):
    """화면에 보이는 글자만 뽑는다. <a> 안의 글자는 링크별로 묶는다."""

    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.skip_depth = 0
        self.link_stack = []          # 현재 열려 있는 <a>의 href
        self.cards = {}               # href -> [문구, ...]
        self.others = []              # 링크 밖 문구

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip_depth += 1
            return
        if tag == "a":
            href = dict(attrs).get("href") or ""
            href = urllib.parse.urljoin(self.base_url, href.strip())
            self.link_stack.append(href)

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if tag == "a" and self.link_stack:
            self.link_stack.pop()

    def handle_data(self, data):
        if self.skip_depth:
            return
        text = re.sub(r"\s+", " ", data).strip()
        if not text or text in IGNORE_TEXTS:
            return
        if self.link_stack and self.link_stack[-1]:
            self.cards.setdefault(self.link_stack[-1], []).append(text)
        else:
            self.others.append(text)


def fetch_html():
    test_file = os.environ.get("WATCH_HTML_FILE")
    if test_file:
        return Path(test_file).read_text(encoding="utf-8")
    req = urllib.request.Request(
        URL,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; welly-watch/1.0; personal change monitor)",
            "Accept-Language": "ko-KR,ko;q=0.9",
            "Cache-Control": "no-cache",
        },
    )
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.read().decode("utf-8", errors="replace")
        except Exception as e:  # 네트워크 오류는 잠시 뒤 다시 시도
            last_error = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"페이지 조회 실패: {last_error}")


def snapshot(html):
    parser = Extractor(URL)
    parser.feed(html)
    count = sum(len(v) for v in parser.cards.values()) + len(parser.others)
    if count < MIN_TEXT_COUNT:
        raise RuntimeError(f"페이지에서 읽은 문구가 너무 적음({count}개). 구조 변경 또는 차단 가능성.")
    return {"cards": parser.cards, "others": parser.others}


def list_diff(old, new):
    """(사라진 문구, 새로 생긴 문구). 순서만 바뀐 것은 변경으로 보지 않는다."""
    old_left = list(old)
    added = []
    for item in new:
        if item in old_left:
            old_left.remove(item)
        else:
            added.append(item)
    return old_left, added


def short(texts, limit=300):
    joined = " / ".join(texts)
    return joined if len(joined) <= limit else joined[:limit] + "…"


def compare(old, new):
    """변경 내용을 사람이 읽을 문장 목록으로 돌려준다. 없으면 빈 목록."""
    lines = []
    old_cards, new_cards = old.get("cards", {}), new["cards"]

    for href, texts in new_cards.items():
        if href not in old_cards:
            lines.append(f"🆕 새 항목\n{short(texts)}\n{href}")
    for href, texts in old_cards.items():
        if href not in new_cards:
            lines.append(f"❌ 사라진 항목\n{short(texts)}\n{href}")
    for href, texts in new_cards.items():
        if href in old_cards:
            removed, added = list_diff(old_cards[href], texts)
            if removed or added:
                title = texts[0] if texts else href
                part = [f"✏️ 변경: {title}"]
                if removed:
                    part.append(f"이전: {short(removed)}")
                if added:
                    part.append(f"현재: {short(added)}")
                part.append(href)
                lines.append("\n".join(part))

    removed, added = list_diff(old.get("others", []), new["others"])
    if removed or added:
        part = ["📄 그 외 문구 변경"]
        if removed:
            part.append(f"사라짐: {short(removed, 500)}")
        if added:
            part.append(f"추가됨: {short(added, 500)}")
        lines.append("\n".join(part))
    return lines


def send_telegram(text):
    if not (BOT_TOKEN and CHAT_ID):
        print("[텔레그램 설정 없음 - 화면 출력]\n" + text)
        return
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    # 텔레그램 메시지 길이 제한(4096자)에 맞춰 나눠 보낸다.
    for i in range(0, len(text), 3900):
        data = urllib.parse.urlencode({
            "chat_id": CHAT_ID,
            "text": text[i:i + 3900],
            "disable_web_page_preview": "true",
        }).encode()
        with urllib.request.urlopen(urllib.request.Request(api, data=data), timeout=30) as res:
            if res.status != 200:
                raise RuntimeError(f"텔레그램 전송 실패: HTTP {res.status}")


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return None


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = load_state()

    try:
        current = snapshot(fetch_html())
    except Exception as e:
        print(f"조회 실패: {e}")
        if state is None:
            return 1  # 첫 실행부터 실패하면 설정 문제이므로 실패로 표시
        state["fail_count"] = state.get("fail_count", 0) + 1
        if state["fail_count"] == FAIL_ALERT_AT:
            send_telegram(f"⚠️ [웰리트립 감시] {FAIL_ALERT_AT}회 연속 조회 실패\n{e}\n{URL}")
        save_state(state)
        return 0

    if state is None:
        save_state({**current, "checked_date": today, "fail_count": 0})
        send_telegram(
            "✅ [웰리트립 감시] 시작했습니다.\n"
            f"현재 링크 {len(current['cards'])}개, 그 외 문구 {len(current['others'])}개를 기준으로 저장했습니다.\n"
            f"이후 달라지는 내용이 있으면 알려드립니다.\n{URL}"
        )
        print("첫 실행: 기준 상태 저장")
        return 0

    changes = compare(state, current)
    if changes:
        send_telegram("🔔 [웰리트립 변경]\n\n" + "\n\n".join(changes) + f"\n\n{URL}")
        print(f"변경 {len(changes)}건 알림")
    else:
        print("변경 없음")

    # checked_date는 하루에 한 번만 바뀐다 → 저장소에 하루 1회 커밋이 남아
    # GitHub가 '60일 무활동'으로 예약 실행을 끄는 것을 막는다.
    save_state({**current, "checked_date": today, "fail_count": 0})
    return 0


if __name__ == "__main__":
    sys.exit(main())
