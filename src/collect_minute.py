"""EOD 분봉 수집 — 그날 급등주 스캔에 등장한 종목들의 당일 1분봉을 저장한다.

목적: 학습 라벨의 생존편향 제거. 스캔 forward-return은 'top-40에 남은' 종목만
가격을 알 수 있어(이탈=대개 하락→라벨이 승자 쪽으로 편향) 모델이 낙관적이 된다.
분봉이 있으면 이탈 종목의 실제 N분 뒤 가격도 알 수 있어 편향이 사라진다.

⚠️ KIS '당일분봉'은 오늘 데이터만 준다(과거 백필 불가). 반드시 **장 마감 후
같은 날**(15:30~자정) 실행할 것. 한 번에 ~30봉이라 하루치는 페이징한다.

  python -m src.collect_minute            # 오늘 스캔 종목의 당일 분봉 수집
  python -m src.collect_minute --limit 50 # 상위 N종목만(테스트)
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from .data.store import DataStore
from .kis.client import KISApiError, KISClient
from .kis.config import load_config
from .kis.domestic import DomesticStock

_OPEN = "090000"
_CLOSE = "153000"
_MAX_PAGES = 20   # 안전상한(하루 390분 / 30봉 ≈ 13페이지)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def _dec_minute(hhmmss: str) -> str:
    """HHMMSS 에서 1분 뺀 HHMMSS(페이징 커서)."""
    t = datetime.strptime(hhmmss, "%H%M%S")
    return (t.replace(second=0) - timedelta(minutes=1)).strftime("%H%M%S")


def fetch_day(dom: DomesticStock, symbol: str) -> list[dict]:
    """당일 분봉 전체를 페이징해 수집(시간순)."""
    bars: dict[str, dict] = {}
    cursor = _CLOSE
    for _ in range(_MAX_PAGES):
        try:
            chunk = dom.minute_bars(symbol, cursor)
        except KISApiError:
            break
        if not chunk:
            break
        new = 0
        for b in chunk:
            if b["time"] not in bars:
                bars[b["time"]] = b
                new += 1
        earliest = min(b["time"] for b in chunk)
        if earliest <= _OPEN or new == 0:
            break
        cursor = _dec_minute(earliest)
    return sorted(bars.values(), key=lambda b: b["time"])


def main() -> None:
    ap = argparse.ArgumentParser(description="EOD 분봉 수집(급등주 스캔 종목)")
    ap.add_argument("--limit", type=int, default=0, help="대상 종목 수 제한(0=전체)")
    ap.add_argument("--date", default=None, help="대상일 YYYY-MM-DD(기본 오늘)")
    args = ap.parse_args()

    store = DataStore()
    date = args.date or datetime.now().strftime("%Y-%m-%d")
    yyyymmdd = date.replace("-", "")
    symbols = store.scan_symbols_on(date)
    done = store.minute_symbols_done(yyyymmdd)
    todo = [s for s in symbols if s not in done]
    if args.limit:
        todo = todo[: args.limit]

    log(f"분봉 수집 대상일 {date} · 스캔종목 {len(symbols)} · "
        f"이미수집 {len(done)} · 이번 {len(todo)}")
    if not todo:
        log("수집할 종목이 없습니다(스캔 데이터 없음 또는 이미 완료).")
        store.close()
        return

    dom = DomesticStock(KISClient(load_config()))
    total_bars = 0
    for i, sym in enumerate(todo, 1):
        bars = fetch_day(dom, sym)
        if bars:
            total_bars += store.save_minute_bars(sym, bars)
        if i % 20 == 0 or i == len(todo):
            log(f"  [{i}/{len(todo)}] 누적 {total_bars:,}봉")
    log(f"완료. {len(todo)}종목 · {total_bars:,}봉 저장.")
    store.close()


if __name__ == "__main__":
    main()
