# -*- coding: utf-8 -*-
"""
네이버 시리즈 다운로드수 수집기
--------------------------------
config.json 에 등록된 작품 페이지에서 '다운로드' 수치를 파싱해
data/records.json 에 날짜별로 누적 기록하고,
대시보드가 읽는 data/data.js 를 다시 생성합니다.

외부 라이브러리 없이 파이썬 표준 라이브러리만 사용합니다. (Python 3.8+)

사용법:
    python collect.py            # 수집 + 기록 + data.js 생성
    python collect.py --debug    # 파싱 실패 시 원본 HTML을 debug_*.html 로 저장
    python collect.py --dry-run  # 수집만 하고 기록하지 않음 (동작 확인용)
"""

import json
import re
import sys
import time
import gzip
import io
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
RECORDS_PATH = BASE_DIR / "data" / "records.json"
DATAJS_PATH = BASE_DIR / "data" / "data.js"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip",
    "Referer": "https://series.naver.com/",
}

# 콘솔 한글 깨짐 방지 (Windows cmd 대응)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def fetch_html(url: str, retries: int = 3, timeout: int = 15) -> str:
    """페이지 HTML을 가져온다. 실패 시 재시도."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                # 네이버 시리즈는 UTF-8. 혹시 몰라 fallback 처리.
                for enc in ("utf-8", "cp949", "euc-kr"):
                    try:
                        return raw.decode(enc)
                    except UnicodeDecodeError:
                        continue
                return raw.decode("utf-8", errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"페이지 요청 실패: {last_err}")


def parse_korean_number(text: str):
    """'1,234,567' / '1.2만' / '3.4억' / '12만 3,456' 형태를 정수로 변환."""
    text = text.strip()
    # 억/만 단위 조합 (예: 1억 2,345만)
    m = re.match(r"^([\d,\.]+)\s*억(?:\s*([\d,\.]+)\s*만)?", text)
    if m:
        val = float(m.group(1).replace(",", "")) * 100_000_000
        if m.group(2):
            val += float(m.group(2).replace(",", "")) * 10_000
        return int(val)
    m = re.match(r"^([\d,\.]+)\s*만(?:\s*([\d,]+))?", text)
    if m:
        val = float(m.group(1).replace(",", "")) * 10_000
        if m.group(2):
            val += int(m.group(2).replace(",", ""))
        return int(val)
    m = re.match(r"^[\d,]+", text)
    if m:
        return int(m.group(0).replace(",", ""))
    return None


def parse_downloads(html: str):
    """
    HTML에서 다운로드 수를 찾는다. 페이지 구조 변경에 대비해
    여러 전략을 순서대로 시도한다.
    반환: (정수값, 사용된 전략 이름) 또는 (None, None)
    """
    # 전략 1: '다운로드' 키워드 뒤 가까운 위치의 숫자 (태그 사이 허용)
    #   예: <li><span>다운로드</span><em>12,345,678</em></li>
    for m in re.finditer(r"다운로드", html):
        window = html[m.end(): m.end() + 300]
        text = re.sub(r"<[^>]+>", " ", window)          # 태그 제거
        text = re.sub(r"&nbsp;?", " ", text)
        num_m = re.search(r"([\d][\d,\.]*\s*(?:억\s*[\d,\.]*\s*만?|만\s*[\d,]*)?)", text)
        if num_m:
            val = parse_korean_number(num_m.group(1))
            # 다운로드수가 두 자리 이하로 나오면 다른 숫자를 잘못 잡았을 가능성이 큼
            if val is not None and val >= 10:
                return val, "keyword-proximity"

    # 전략 2: JSON 데이터 내장형 (downloadCount 류 키)
    m = re.search(r'"(?:downloadCount|download_cnt|downloadCnt)"\s*:\s*"?([\d,]+)"?', html)
    if m:
        return int(m.group(1).replace(",", "")), "embedded-json"

    return None, None


def parse_title(html: str):
    """페이지 제목에서 작품명 추출 (config에 이름이 없을 때 대비)."""
    m = re.search(r"<meta\s+property=\"og:title\"\s+content=\"([^\"]+)\"", html)
    if m:
        return m.group(1).strip()
    m = re.search(r"<title>([^<]+)</title>", html)
    if m:
        return re.sub(r"\s*[:|\-]\s*네이버\s*시리즈.*$", "", m.group(1)).strip()
    return None


def load_json(path: Path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def write_datajs(records: dict, works: list):
    """대시보드가 file:// 환경에서도 읽을 수 있도록 JS 파일로 내보낸다."""
    payload = {
        "sample": False,
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "works": [{"name": w["name"], "url": w.get("url", "")} for w in works],
        "records": records,   # { "YYYY-MM-DD": { "작품명": 다운로드수, ... }, ... }
    }
    DATAJS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(DATAJS_PATH, "w", encoding="utf-8") as f:
        f.write("window.SERIES_DATA = ")
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write(";\n")


def main():
    debug = "--debug" in sys.argv
    dry_run = "--dry-run" in sys.argv

    config = load_json(CONFIG_PATH, None)
    if not config or not config.get("works"):
        print("[오류] config.json 에 작품이 등록되어 있지 않습니다.")
        print('        예: {"works": [{"name": "작품명", "url": "https://series.naver.com/novel/detail.series?productNo=..."}]}')
        sys.exit(1)

    today = datetime.now().strftime("%Y-%m-%d")
    store = load_json(RECORDS_PATH, {"records": {}})
    records = store.get("records", {})
    day = records.get(today, {})

    ok, fail = 0, 0
    print(f"=== 네이버 시리즈 다운로드수 수집 · {today} ===")

    for i, work in enumerate(config["works"]):
        name = work.get("name") or f"작품{i+1}"
        url = work.get("url", "").strip()
        if not url:
            print(f"  [건너뜀] {name}: url 이 비어 있습니다.")
            continue
        try:
            html = fetch_html(url)
            if not work.get("name"):
                t = parse_title(html)
                if t:
                    name = t
            value, strategy = parse_downloads(html)
            if value is None:
                fail += 1
                print(f"  [실패] {name}: 다운로드 수치를 찾지 못했습니다.")
                if debug:
                    p = BASE_DIR / f"debug_{i+1}.html"
                    p.write_text(html, encoding="utf-8")
                    print(f"         원본 HTML 저장 → {p.name}")
                continue
            day[name] = value
            ok += 1
            print(f"  [성공] {name}: {value:,}  (방식: {strategy})")
        except Exception as e:
            fail += 1
            print(f"  [실패] {name}: {e}")
        time.sleep(1.5)  # 서버 부담을 줄이기 위한 요청 간 간격

    if ok and not dry_run:
        records[today] = day
        store["records"] = records
        save_json(RECORDS_PATH, store)
        write_datajs(records, config["works"])
        print(f"기록 완료: 성공 {ok} / 실패 {fail} → data/records.json, data/data.js 갱신")
    elif dry_run:
        print(f"(dry-run) 성공 {ok} / 실패 {fail} — 기록하지 않았습니다.")
    else:
        print("수집에 모두 실패하여 기록하지 않았습니다. --debug 옵션으로 다시 실행해 HTML을 확인해 보세요.")
        sys.exit(2)


if __name__ == "__main__":
    main()
