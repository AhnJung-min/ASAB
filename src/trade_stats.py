"""산업부 '수출입 동향' 월간 보도자료(PDF) 파서 + 브리핑.

매달 발표되는 MOTIE 수출입동향 PDF에서 정형 수치를 추출한다:
  - 총괄: 총수출/총수입/무역수지 (+ YoY 증감률)
  - 15대 주력 품목별 수출액·증감률 (반도체·자동차·선박 ...)
  - 메모리 반도체 고정가격 (DDR4/DDR5/NAND)

추출 결과는 store.save_trade_stats() 로 DB(trade_stats)에 적재해
대시보드 거시 패널·섹터 틸트 분석에 사용한다.

사용:  python -m src.trade_stats "다운로드/2026년 4월 수출입동향_3보_최종.pdf"
       python -m src.trade_stats <pdf> --save     # DB 적재까지
"""
from __future__ import annotations

import argparse
import re
import sys
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 15대 주력 품목 (MOTIE 표 순서) + '전체'. 표가 이 순서로 고정 출력됨.
ITEMS = [
    "반도체", "디스플레이", "무선통신기기", "컴퓨터", "자동차",
    "자동차부품", "일반기계", "선박",
    "석유제품", "석유화학", "바이오헬스", "가전", "섬유", "철강", "이차전지", "전체",
]

# 품목 → 대표 KRX 종목(코드). 섹터 틸트 분석(C) 및 브리핑 코멘트의 토대.
# 정밀 섹터분류 대신 대표 대형주로 시작(향후 업종코드 수집 시 확장).
SECTOR_STOCKS = {
    "반도체": [("005930", "삼성전자"), ("000660", "SK하이닉스")],
    "자동차": [("005380", "현대차"), ("000270", "기아")],
    "자동차부품": [("012330", "현대모비스"), ("161390", "한국타이어앤테크놀로지")],
    "선박": [("329180", "HD현대중공업"), ("042660", "한화오션"), ("010140", "삼성중공업")],
    "철강": [("005490", "POSCO홀딩스"), ("004020", "현대제철")],
    "이차전지": [("373220", "LG에너지솔루션"), ("006400", "삼성SDI")],
    "바이오헬스": [("207940", "삼성바이오로직스"), ("068270", "셀트리온")],
    "디스플레이": [("034220", "LG디스플레이")],
    "석유화학": [("051910", "LG화학"), ("011170", "롯데케미칼")],
    "가전": [("066570", "LG전자")],
    "일반기계": [("042670", "HD현대인프라코어")],
}


def _read_text(path: str) -> str:
    import pypdf
    reader = pypdf.PdfReader(path)
    return "\n".join((p.extract_text() or "") for p in reader.pages)


def _vals(seg: str) -> list[float]:
    """소수 1자리로 붙어 나오는 수치들 추출. 예) '319.012.9' -> [319.0, 12.9]."""
    return [float(x) for x in re.findall(r"\d+\.\d", seg)]


def _signed(seg: str) -> list[float]:
    """부호 포함 증감률. △=음수. 예) '+173.5△2.7' -> [173.5, -2.7]."""
    out = []
    for m in re.findall(r"[+△]\d+\.\d", seg):
        v = float(m[1:])
        out.append(-v if m[0] == "△" else v)
    return out


def parse_pdf(path: str) -> dict[str, Any]:
    text = _read_text(path)

    # --- 기준 연월 ---
    ym = re.search(r"(\d{4})년\s*(\d{1,2})월\s*수출입", text)
    year, month = (int(ym.group(1)), int(ym.group(2))) if ym else (None, None)
    ym_str = f"{year}-{month:02d}" if year else "unknown"

    # --- 총괄(보도자료 본문) ---
    # 주의: 증감률은 △(감소)·+(증가) 모두 등장, 무역수지는 적자(△)도 있음.
    def _pct(s: str) -> float:
        neg = "△" in s or "▲" in s or "-" in s
        num = re.search(r"[\d.]+", s)
        v = float(num.group()) if num else 0.0
        return -v if neg else v

    out: dict[str, Any] = {"month": ym_str, "items": {}, "memory": {}}
    m = re.search(r"수출\s*([\d,]+\.\d)억\s*달러\s*\(\s*([+\-△▲]?\s*[\d.]+)\s*%\)", text)
    if m:
        out["export_usd_bil"] = float(m.group(1).replace(",", ""))
        out["export_yoy"] = _pct(m.group(2))
    m = re.search(r"수입\s*([\d,]+\.\d)억\s*달러\s*\(\s*([+\-△▲]?\s*[\d.]+)\s*%\)", text)
    if m:
        out["import_usd_bil"] = float(m.group(1).replace(",", ""))
        out["import_yoy"] = _pct(m.group(2))
    m = re.search(r"수지\s*(△?▲?)\s*([\d,]+(?:\.\d)?)억\s*달러", text)
    if m:
        v = float(m.group(2).replace(",", ""))
        out["balance_usd_bil"] = -v if m.group(1) else v
    # 폴백: 무역수지 = 수출 − 수입 (정수표기 등으로 못 잡았을 때 항상 정확)
    if "balance_usd_bil" not in out and "export_usd_bil" in out and "import_usd_bil" in out:
        out["balance_usd_bil"] = round(out["export_usd_bil"] - out["import_usd_bil"], 1)

    # --- 15대 품목 표: '수출액...증감률...' 블록 2개(앞 8 + 뒤 8) ---
    blocks = re.findall(r"수출액(.*?)증감률(.*?)(?:역대순위|구\s*분|$)", text, re.S)
    amounts: list[float] = []
    rates: list[float] = []
    for amt_seg, rate_seg in blocks:
        a, r = _vals(amt_seg), _signed(rate_seg)
        if a and r and len(a) == len(r):     # 정합성: 두 줄 길이 일치할 때만 채택
            amounts += a
            rates += r
    if len(amounts) >= len(ITEMS):
        for i, name in enumerate(ITEMS):
            out["items"][name] = {"usd_bil": amounts[i], "yoy": rates[i]}

    # --- 메모리 고정가격(현재가 + YoY) : 'DDR4 8Gb1.65 → 16.0(+870%)' 형태 ---
    for key in ("DDR4", "DDR5", "NAND"):
        mm = re.search(rf"{key}.*?→\s*([\d.]+)\s*\(\+?(\d+)", text)
        if mm:
            out["memory"][key] = {"price_usd": float(mm.group(1)), "yoy": float(mm.group(2))}

    return out


def briefing(d: dict[str, Any]) -> str:
    """추출 결과 → 사람이 읽는 월간 브리핑(매매 관점 코멘트 포함)."""
    L = [f"📊 {d['month']} 수출입동향 브리핑", "=" * 44]
    if "export_usd_bil" in d:
        L.append(f"총수출 {d['export_usd_bil']:,.1f}억$ ({d['export_yoy']:+.1f}%) · "
                 f"수입 {d.get('import_usd_bil',0):,.1f}억$ ({d.get('import_yoy',0):+.1f}%) · "
                 f"무역수지 {d.get('balance_usd_bil',0):+,.1f}억$")
    items = d.get("items", {})
    if items:
        ranked = sorted(((n, v) for n, v in items.items() if n != "전체"),
                        key=lambda kv: kv[1]["yoy"], reverse=True)
        def _stocks(name: str) -> str:
            s = SECTOR_STOCKS.get(name)
            return ("  → " + ", ".join(nm for _, nm in s)) if s else ""
        L.append("\n[품목별 수출 증감률 — 강세 → 수혜 대표주]")
        for n, v in ranked[:5]:
            L.append(f"  🟢 {n:<8} {v['usd_bil']:>6.1f}억$  {v['yoy']:+.1f}%{_stocks(n)}")
        L.append("[품목별 수출 증감률 — 약세 → 역풍 대표주]")
        for n, v in ranked[-5:]:
            L.append(f"  🔴 {n:<8} {v['usd_bil']:>6.1f}억$  {v['yoy']:+.1f}%{_stocks(n)}")
    if d.get("memory"):
        mem = " · ".join(f"{k} {v['price_usd']}$({v['yoy']:+.0f}%)" for k, v in d["memory"].items())
        L.append(f"\n[메모리 고정가] {mem}")
    # 매매 관점 코멘트
    L.append("\n[매매 관점]")
    semi = items.get("반도체", {}).get("yoy")
    if semi is not None:
        tone = "초강세 → 삼성전자·SK하이닉스 펀더멘털 뒷받침" if semi > 30 else \
               ("둔화 신호 — 반도체주 경계" if semi < 0 else "완만")
        L.append(f"  · 반도체 {semi:+.1f}% : {tone}")
    if "export_yoy" in d:
        L.append(f"  · 총수출 {d['export_yoy']:+.1f}% : "
                 + ("수출 확장 → 강세장 거시 확인(regime risk-on과 일치)"
                    if d["export_yoy"] > 5 else "수출 둔화 — 거시 역풍 주의"))
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="수출입동향 PDF 파서/브리핑")
    ap.add_argument("pdf", help="수출입동향 PDF 경로")
    ap.add_argument("--save", action="store_true", help="DB(trade_stats)에 적재")
    args = ap.parse_args()

    d = parse_pdf(args.pdf)
    print(briefing(d))

    if args.save:
        from .data.store import DataStore
        store = DataStore()
        n = store.save_trade_stats(d)
        store.close()
        print(f"\n[DB 적재 완료] {d['month']} · {n}개 지표")


if __name__ == "__main__":
    main()
