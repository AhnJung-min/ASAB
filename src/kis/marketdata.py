"""국내 시세/순위/투자자/재무 조회 (학습용 데이터 수집).

모든 엔드포인트는 모의투자 서버에서 동작 확인됨(probe_api.py).
"""
from __future__ import annotations

from typing import Any

from .client import KISClient


class MarketData:
    def __init__(self, client: KISClient):
        self.c = client

    # --- 일봉 기간 차트 (과거 백필) -----------------------------------------
    def daily_candles(
        self, symbol: str, start: str, end: str, adjusted: bool = True
    ) -> list[dict[str, Any]]:
        """일봉 OHLCV. start/end: 'YYYYMMDD'. 한 번에 최대 ~100영업일.

        반환: [{date, open, high, low, close, volume, value}, ...] (과거→최근)
        """
        data = self.c.get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            tr_id="FHKST03010100",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": start,
                "FID_INPUT_DATE_2": end,
                "FID_PERIOD_DIV_CODE": "D",   # D일 W주 M월
                "FID_ORG_ADJ_PRC": "0" if adjusted else "1",  # 0=수정주가
            },
        )
        rows = []
        for r in data.get("output2", []):
            if not r.get("stck_bsop_date"):
                continue
            rows.append(
                {
                    "date": r["stck_bsop_date"],
                    "open": int(r["stck_oprc"]),
                    "high": int(r["stck_hgpr"]),
                    "low": int(r["stck_lwpr"]),
                    "close": int(r["stck_clpr"]),
                    "volume": int(r["acml_vol"]),
                    "value": int(r.get("acml_tr_pbmn", 0)),  # 거래대금
                }
            )
        rows.sort(key=lambda x: x["date"])  # 과거 -> 최근
        return rows

    # --- 시가총액 (유동성/품질 프록시) -------------------------------------
    def market_cap(self, symbol: str) -> int:
        """시가총액(억원). 종목 우선순위·품질 기준으로 사용."""
        data = self.c.get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            tr_id="FHKST01010100",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
        )
        try:
            return int(data["output"].get("hts_avls", 0) or 0)
        except (ValueError, TypeError):
            return 0

    # --- 거래량/거래대금 순위 (종목 유니버스/스크리닝) ----------------------
    def volume_rank(self, by_value: bool = True, top: int = 30) -> list[dict[str, Any]]:
        """거래 상위 종목. by_value=True 면 거래대금 기준, False 면 거래량 기준.

        반환: [{rank, symbol, name, price, change_pct, volume}, ...]
        """
        # FID_BLNG_CLS_CODE: 0=거래량 ... 거래대금은 정렬코드로 처리되나
        # 단순화를 위해 거래량 순위를 받아 거래대금으로 재정렬한다.
        data = self.c.get(
            "/uapi/domestic-stock/v1/quotations/volume-rank",
            tr_id="FHPST01710000",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_COND_SCR_DIV_CODE": "20171",
                "FID_INPUT_ISCD": "0000",       # 0000=전체
                "FID_DIV_CLS_CODE": "0",         # 0=전체
                "FID_BLNG_CLS_CODE": "0",        # 0=평균거래량
                "FID_TRGT_CLS_CODE": "111111111",
                "FID_TRGT_EXLS_CLS_CODE": "000000",
                "FID_INPUT_PRICE_1": "",
                "FID_INPUT_PRICE_2": "",
                "FID_VOL_CNT": "",
                "FID_INPUT_DATE_1": "",
            },
        )
        rows = [
            {
                "symbol": r["mksc_shrn_iscd"],
                "name": r["hts_kor_isnm"],
                "price": int(r["stck_prpr"]),
                "change_pct": float(r["prdy_ctrt"]),
                "volume": int(r["acml_vol"]),
                "value": int(r.get("acml_tr_pbmn", 0)),
            }
            for r in data.get("output", [])
        ]
        key = "value" if by_value else "volume"
        rows.sort(key=lambda x: x[key], reverse=True)
        for i, r in enumerate(rows[:top], 1):
            r["rank"] = i
        return rows[:top]

    # --- 투자자별 매매동향 (외국인/기관/개인) -------------------------------
    def investor_flow(self, symbol: str) -> list[dict[str, Any]]:
        """최근 일자별 순매수 수량. 반환: [{date, foreign, institution, individual}]."""
        data = self.c.get(
            "/uapi/domestic-stock/v1/quotations/inquire-investor",
            tr_id="FHKST01010900",
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
        )
        rows = [
            {
                "date": r["stck_bsop_date"],
                "foreign": int(r["frgn_ntby_qty"]),       # 외국인 순매수
                "institution": int(r["orgn_ntby_qty"]),   # 기관 순매수
                "individual": int(r["prsn_ntby_qty"]),    # 개인 순매수
            }
            for r in data.get("output", [])
            if r.get("stck_bsop_date")
        ]
        rows.sort(key=lambda x: x["date"])
        return rows

    # --- 재무비율 -----------------------------------------------------------
    def financial_ratio(self, symbol: str) -> dict[str, Any] | None:
        """최근 분기 재무비율(ROE/EPS/BPS 등). 가장 최근 분기 1건 반환."""
        data = self.c.get(
            "/uapi/domestic-stock/v1/finance/financial-ratio",
            tr_id="FHKST66430300",
            params={
                "FID_DIV_CLS_CODE": "0",
                "fid_cond_mrkt_div_code": "J",
                "fid_input_iscd": symbol,
            },
        )
        out = data.get("output", [])
        if not out:
            return None
        r = out[0]  # 가장 최근 분기

        def _f(v: Any) -> float | None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        return {
            "period": r.get("stac_yymm"),
            "roe": _f(r.get("roe_val")),
            "eps": _f(r.get("eps")),
            "bps": _f(r.get("bps")),
            "net_income_growth": _f(r.get("ntin_inrt")),  # 순이익증가율
            "sales_growth": _f(r.get("grs")),             # 매출액증가율
        }
