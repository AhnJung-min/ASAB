"""국내 종목 유니버스 빌더.

한국투자증권이 제공하는 종목 마스터 파일(kospi_code.mst / kosdaq_code.mst)을
내려받아 파싱하고, 증권그룹구분코드(ST=주권)만 추려 'ETF/ETN/리츠 제외 개별주'
유니버스를 만든다. 결과는 stock_master 테이블에 저장한다.

마스터 레코드는 고정폭(EUC-KR 문자 기준)이며 뒷부분 길이가 시장마다 달라,
알려진 그룹코드 집합에 가장 잘 맞는 컷 길이를 자동 탐색해 견고하게 파싱한다.
"""
from __future__ import annotations

import argparse
import io
import sys
import urllib.request
import zipfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from .data.store import DataStore

MASTERS = {
    "KOSPI": "https://new.real.download.dws.co.kr/common/master/kospi_code.mst.zip",
    "KOSDAQ": "https://new.real.download.dws.co.kr/common/master/kosdaq_code.mst.zip",
}

# 증권그룹구분코드 (마스터 뒷부분 첫 2자)
# ST=주권, EF=ETF, EN=ETN, RT=리츠, BC=수익증권, FS=외국주권, DR=예탁증서 ...
KNOWN_GROUPS = {
    "ST", "MF", "RT", "SC", "IF", "DR", "EW", "EF", "SW", "SR",
    "BC", "FE", "FS", "EN", "EI", "IC", "TC", "GD", "PF",
}
INDIVIDUAL_GROUP = "ST"  # 개별 보통주


def _download(url: str) -> bytes:
    data = urllib.request.urlopen(url, timeout=30).read()
    z = zipfile.ZipFile(io.BytesIO(data))
    return z.read(z.namelist()[0])


def _detect_cut(lines: list[str]) -> int:
    """뒷부분 컷 길이 자동 탐색(그룹코드 매칭 최대화)."""
    best = (227, -1)
    for n in range(215, 235):
        ok = sum(1 for s in lines if len(s) > n and s[-n:][0:2] in KNOWN_GROUPS)
        if ok > best[1]:
            best = (n, ok)
    return best[0]


def parse_master(market: str) -> list[dict]:
    """한 시장의 마스터를 파싱해 전체 종목 리스트 반환."""
    raw = _download(MASTERS[market])
    lines = [
        b.rstrip(b"\r").decode("euc-kr", errors="replace")
        for b in raw.split(b"\n")
        if len(b) > 50
    ]
    n = _detect_cut(lines)
    rows = []
    for s in lines:
        if len(s) <= n:
            continue
        rows.append({
            "symbol": s[0:9].strip(),
            "name": s[21:-n].strip(),
            "market": market,
            "group": s[-n:][0:2],
        })
    return rows


def individual_stocks() -> list[dict]:
    """ETF/ETN/리츠 등을 제외한 개별 보통주(ST)만."""
    out = []
    for market in MASTERS:
        out.extend(r for r in parse_master(market) if r["group"] == INDIVIDUAL_GROUP)
    return out


def build(store: DataStore | None = None) -> list[dict]:
    own = store is None
    store = store or DataStore()
    stocks = individual_stocks()
    store.save_master(stocks)
    if own:
        store.close()
    return stocks


def rank_liquidity(store: DataStore, log_every: int = 100) -> None:
    """모든 개별주의 시가총액을 조회해 stock_master.liquidity 갱신(수집 우선순위용)."""
    from datetime import datetime
    from .kis.client import KISApiError, KISClient
    from .kis.config import load_config
    from .kis.marketdata import MarketData

    md = MarketData(KISClient(load_config()))
    rows = store.master_symbols()
    total = len(rows)
    batch: list[tuple[float, str]] = []
    for i, r in enumerate(rows, 1):
        try:
            cap = md.market_cap(r["symbol"])
        except KISApiError:
            cap = 0
        batch.append((float(cap), r["symbol"]))
        store.add_collect_log(datetime.now().strftime("%H:%M:%S"), "liquidity",
                              r["symbol"], r["name"], f"시총 {cap/10000:,.1f}조 ({i}/{total})")
        if len(batch) >= 50:
            store.set_master_liquidity(batch)
            batch = []
        if i % log_every == 0:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] 시가총액 조사 {i}/{total} ...", flush=True)
    if batch:
        store.set_master_liquidity(batch)
    print(f"시가총액 조사 완료: {total}종목. 이제 collect 가 대형주부터 수집합니다.", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="국내 종목 유니버스 빌더")
    ap.add_argument("--all-groups", action="store_true",
                    help="ST 외 그룹도 모두 저장(분포 확인용)")
    ap.add_argument("--liquidity", action="store_true",
                    help="개별주 시가총액 조사 → 수집 우선순위(대형주 우선) 설정")
    args = ap.parse_args()

    store = DataStore()
    if args.liquidity:
        rank_liquidity(store)
        store.close()
        return
    if args.all_groups:
        rows = []
        for m in MASTERS:
            rows.extend(parse_master(m))
        from collections import Counter
        dist = Counter(r["group"] for r in rows)
        print("전체 그룹코드 분포:")
        for g, c in dist.most_common():
            print(f"  {g}: {c}")
    stocks = build(store)
    kospi = sum(1 for s in stocks if s["market"] == "KOSPI")
    kosdaq = sum(1 for s in stocks if s["market"] == "KOSDAQ")
    print(f"개별주 유니버스 저장: 총 {len(stocks)}개 (KOSPI {kospi} / KOSDAQ {kosdaq})")
    store.close()


if __name__ == "__main__":
    main()
