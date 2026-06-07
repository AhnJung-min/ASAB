"""관세청 '수출입 실적(품목별)' 과거 CSV → 섹터별 월별 수출 시계열.

tradedata.go.kr 에서 받은 6자리 HS 월별 CSV(들)를 읽어, 4자리 HS 접두로
우리 섹터(반도체·자동차·선박 ...)에 매핑·합산한다. C(섹터 틸트) 검증의 입력.

CSV 컬럼: 기간(YYYY-MM), HS코드(6자리), 품목명, 수출 중량, 수출 금액(천불), ...

사용:  python -m src.trade_history "a.csv" "b.csv"            # 미리보기
       python -m src.trade_history "a.csv" "b.csv" --save     # DB 적재
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 섹터 → 4자리 HS 접두(들). trade_stats.SECTOR_STOCKS 의 섹터명과 일치시킨다.
SECTOR_HS = {
    "반도체": {"8541", "8542"},
    "자동차": {"8703"},
    "자동차부품": {"8708"},
    "선박": {"8901", "8905", "8906"},
    "철강": {"7208", "7209", "7210", "7211", "7212"},
    "이차전지": {"8507"},
    "석유제품": {"2710"},
    "컴퓨터": {"8471"},
    "휴대폰": {"8517"},
    "바이오": {"3002", "3004"},
    "화장품": {"3304"},
}
# 4자리 접두 → 섹터 역색인
_PREFIX = {p: sec for sec, ps in SECTOR_HS.items() for p in ps}


def load_csv(paths: list[str]) -> dict[tuple[str, str], float]:
    """여러 CSV → {(month, sector): 수출금액합(천불)}. 6자리 HS를 4자리로 섹터 매핑."""
    agg: dict[tuple[str, str], float] = defaultdict(float)
    for path in paths:
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                month = (row.get("기간") or "").strip()
                hs = (row.get("HS코드") or "").strip()
                if not hs or month == "총계" or "-" not in month:
                    continue
                sector = _PREFIX.get(hs[:4])
                if not sector:
                    continue
                try:
                    val = float(row.get("수출 금액") or 0)
                except ValueError:
                    val = 0.0
                agg[(month, sector)] += val
    return dict(agg)


def to_rows(agg: dict[tuple[str, str], float]) -> list[dict[str, Any]]:
    """정렬된 [{month, sector, export_kusd}] 리스트."""
    return [{"month": m, "sector": s, "export_kusd": v}
            for (m, s), v in sorted(agg.items())]


def main() -> None:
    ap = argparse.ArgumentParser(description="수출 실적 CSV → 섹터별 월별 시계열")
    ap.add_argument("csv", nargs="+", help="수출입 실적(품목별) CSV 경로(들)")
    ap.add_argument("--save", action="store_true", help="DB(export_history) 적재")
    args = ap.parse_args()

    agg = load_csv(args.csv)
    months = sorted({m for m, _ in agg})
    sectors = sorted({s for _, s in agg})
    print(f"적재 대상: {len(months)}개월 ({months[0]}~{months[-1]}) · {len(sectors)}섹터 "
          f"· {len(agg)}행")

    # 최신월 섹터별 수출(억$로 환산: 천불 → 억$ = /100000) 검증용
    latest = months[-1]
    print(f"\n[{latest}] 섹터별 수출(억$)")
    snap = sorted(((s, agg.get((latest, s), 0)) for s in sectors), key=lambda kv: -kv[1])
    for s, v in snap:
        print(f"  {s:<8} {v/100_000:>7.1f}억$")

    if args.save:
        from .data.store import DataStore
        store = DataStore()
        n = store.save_export_history(to_rows(agg))
        store.close()
        print(f"\n[DB 적재 완료] export_history {n}행")


if __name__ == "__main__":
    main()
