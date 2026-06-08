"""국내 주식 시세 조회 / 주문 / 잔고.

TR_ID 는 모의투자(V...)와 실전(T...)이 다르므로 paper_trading 에 따라 분기한다.
"""
from __future__ import annotations

from typing import Any

from .client import KISClient


class DomesticStock:
    def __init__(self, client: KISClient):
        self.c = client
        self.paper = client.config.paper_trading

    # --- 시세 ---------------------------------------------------------------
    def current_price(self, symbol: str, market: str = "J") -> int:
        """현재가(원) 조회. symbol: 6자리 종목코드.

        market(FID_COND_MRKT_DIV_CODE): J=KRX(거래소) / NX=넥스트레이드(NXT)
        / UN=통합(KRX+NXT). NXT 연장시간(프리·애프터마켓) 변동까지 보려면 UN.
        """
        data = self.c.get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            tr_id="FHKST01010100",  # 시세는 모의/실전 동일
            params={"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": symbol},
        )
        return int(data["output"]["stck_prpr"])

    def order_book(self, symbol: str, market: str = "J") -> dict[str, Any]:
        """호가창(매수/매도 잔량) 조회. 단타 핵심 피처=잔량 임밸런스.

        반환: {bid1, ask1, total_bid, total_ask, imbalance, spread_bps}
          imbalance = (총매수잔량-총매도잔량)/(총매수+총매도) ∈ [-1,1]
                      양수=매수세 우위(상승압력). (호가 TR FHKST01010200)
        """
        d = self.c.get(
            "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            tr_id="FHKST01010200",  # 시세계 → 모의/실전 동일
            params={"FID_COND_MRKT_DIV_CODE": market, "FID_INPUT_ISCD": symbol},
        )
        o = d.get("output1", {}) or {}
        tb = int(o.get("total_bidp_rsqn", 0) or 0)
        ta = int(o.get("total_askp_rsqn", 0) or 0)
        bid1 = int(o.get("bidp1", 0) or 0)
        ask1 = int(o.get("askp1", 0) or 0)
        denom = tb + ta
        imbalance = (tb - ta) / denom if denom > 0 else 0.0
        spread_bps = ((ask1 - bid1) / bid1 * 10000) if bid1 > 0 else 0.0
        return {"bid1": bid1, "ask1": ask1, "total_bid": tb, "total_ask": ta,
                "imbalance": imbalance, "spread_bps": spread_bps}

    def minute_bars(self, symbol: str, to_hour: str = "153000",
                    market: str = "J") -> list[dict[str, Any]]:
        """당일 분봉 조회. to_hour(HHMMSS) 기준 과거로 최대 ~30봉 반환(최신순).

        하루 전체는 to_hour 를 이전 봉 시각으로 당기며 페이징한다(collect_minute).
        반환: [{date(YYYYMMDD), time(HHMMSS), open, high, low, close, volume}, ...]
        (당일분봉 FHKST03010200, 시세계 → 모의/실전 동일)
        """
        d = self.c.get(
            "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            tr_id="FHKST03010200",
            params={"FID_ETC_CLS_CODE": "", "FID_COND_MRKT_DIV_CODE": market,
                    "FID_INPUT_ISCD": symbol, "FID_INPUT_HOUR_1": to_hour,
                    "FID_PW_DATA_INCU_YN": "N"},
        )
        out: list[dict[str, Any]] = []
        for r in d.get("output2", []) or []:
            try:
                out.append({
                    "date": r["stck_bsop_date"], "time": r["stck_cntg_hour"],
                    "open": float(r["stck_oprc"]), "high": float(r["stck_hgpr"]),
                    "low": float(r["stck_lwpr"]), "close": float(r["stck_prpr"]),
                    "volume": int(r["cntg_vol"]),
                })
            except (KeyError, ValueError):
                continue
        return out

    # --- 순위(스캔) ---------------------------------------------------------
    def top_gainers(self, top: int = 30, *, gubn: str = "up",
                    market: str = "0000", min_price: int = 0,
                    min_volume: int = 0) -> list[dict[str, Any]]:
        """장중 등락률 순위. 단타 스캔용(한 호출로 다수 종목 → 호출제한 절약).

          top      : 상위 몇 종목
          gubn     : 'up'=상승률 상위 / 'down'=하락률 상위
          market   : '0000'=전체 / '0001'=코스피 / '1001'=코스닥
          min_price/min_volume : 가격·거래량 하한 필터(0=무시)

        반환: [{symbol, name, price, rate(등락률%), volume, rank}, ...]
        스캔→매수까지 surge_bot 국내 이식의 진입점. (등락률순위 FHPST01700000)
        """
        data = self.c.get(
            "/uapi/domestic-stock/v1/ranking/fluctuation",
            tr_id="FHPST01700000",  # 등락률 순위(시세계 → 모의/실전 동일)
            params={
                "fid_cond_mrkt_div_code": "J",
                "fid_cond_scr_div_code": "20170",
                "fid_input_iscd": market,
                "fid_rank_sort_cls_code": "0" if gubn == "up" else "1",
                "fid_input_cnt_1": "0",       # 누적일수(0=당일)
                "fid_prc_cls_code": "1",      # 1=종가대비(등락률)
                "fid_input_price_1": str(min_price) if min_price else "",
                "fid_input_price_2": "",
                "fid_vol_cnt": str(min_volume) if min_volume else "",
                "fid_trgt_cls_code": "0",
                "fid_trgt_exls_cls_code": "0",
                "fid_div_cls_code": "0",      # 0=전체
                "fid_rsfl_rate1": "",
                "fid_rsfl_rate2": "",
            },
        )
        rows: list[dict[str, Any]] = []
        for r in data.get("output", []):
            try:
                rows.append({
                    "symbol": r["stck_shrn_iscd"],
                    "name": r["hts_kor_isnm"],
                    "price": int(r["stck_prpr"]),
                    "rate": float(r["prdy_ctrt"]),     # 전일대비율(등락률%)
                    "volume": int(r["acml_vol"]),      # 누적거래량
                    "rank": int(r.get("data_rank", 0) or 0),
                })
            except (KeyError, ValueError):
                continue
        return rows[:top]

    def top_value(self, top: int = 30, *, market: str = "0000",
                  min_price: int = 0, min_volume: int = 0) -> list[dict[str, Any]]:
        """거래대금(거래금액) 순위. 유동성 풍부한 단타 유니버스(스파이커 아님).

        반환: top_gainers 와 동일 형태 + value(거래대금). (거래량순위 FHPST01710000,
        FID_BLNG_CLS_CODE=3=거래금액순). 대형 우량주 위주라 단타에 건강한 풀.
        """
        data = self.c.get(
            "/uapi/domestic-stock/v1/quotations/volume-rank",
            tr_id="FHPST01710000",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_COND_SCR_DIV_CODE": "20171",
                "FID_INPUT_ISCD": market,
                "FID_DIV_CLS_CODE": "0",          # 0=전체
                "FID_BLNG_CLS_CODE": "3",         # 3=거래금액(거래대금)순
                "FID_TRGT_CLS_CODE": "111111111",
                "FID_TRGT_EXLS_CLS_CODE": "0000000000",
                "FID_INPUT_PRICE_1": str(min_price) if min_price else "",
                "FID_INPUT_PRICE_2": "",
                "FID_VOL_CNT": str(min_volume) if min_volume else "",
                "FID_INPUT_DATE_1": "",
            },
        )
        rows: list[dict[str, Any]] = []
        for r in data.get("output", []):
            try:
                rows.append({
                    "symbol": r["mksc_shrn_iscd"],
                    "name": r["hts_kor_isnm"],
                    "price": int(r["stck_prpr"]),
                    "rate": float(r["prdy_ctrt"]),         # 등락률%
                    "volume": int(r.get("acml_vol", 0) or 0),
                    "value": int(r.get("acml_tr_pbmn", 0) or 0),  # 거래대금
                    "rank": int(r.get("data_rank", 0) or 0),
                })
            except (KeyError, ValueError):
                continue
        return rows[:top]

    # --- 주문 ---------------------------------------------------------------
    def _order(self, symbol: str, qty: int, side: str, price: int = 0) -> dict[str, Any]:
        """side: 'buy' | 'sell'. price=0 이면 시장가."""
        if side == "buy":
            tr_id = "VTTC0802U" if self.paper else "TTTC0802U"
        elif side == "sell":
            tr_id = "VTTC0801U" if self.paper else "TTTC0801U"
        else:
            raise ValueError(f"알 수 없는 side: {side}")

        # 주문구분 01=시장가, 00=지정가
        ord_dvsn = "01" if price == 0 else "00"
        body = {
            "CANO": self.c.config.account_no,
            "ACNT_PRDT_CD": self.c.config.account_product_code,
            "PDNO": symbol,
            "ORD_DVSN": ord_dvsn,
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
        }
        return self.c.post(
            "/uapi/domestic-stock/v1/trading/order-cash", tr_id=tr_id, body=body
        )

    def buy(self, symbol: str, qty: int, price: int = 0) -> dict[str, Any]:
        return self._order(symbol, qty, "buy", price)

    def sell(self, symbol: str, qty: int, price: int = 0) -> dict[str, Any]:
        return self._order(symbol, qty, "sell", price)

    # --- 잔고 ---------------------------------------------------------------
    def balance(self) -> dict[str, Any]:
        """보유 종목 및 예수금 조회.

        반환: {'holdings':[...], 'cash':int, 'total':int, 'deposit_total':int}
          cash  = prvs_rcdl_excc_amt(D+2 정산 예수금) = 매수로 실제 빠져나간 '진짜 현금'.
                  dnca_tot_amt(예수금총액)는 T+2 미정산이라 매수해도 안 줄어 부정확.
          total = nass_amt(순자산) = cash + 보유평가액. (cash+holdings 와 일치 확인됨)
        """
        tr_id = "VTTC8434R" if self.paper else "TTTC8434R"
        data = self.c.get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id=tr_id,
            params={
                "CANO": self.c.config.account_no,
                "ACNT_PRDT_CD": self.c.config.account_product_code,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        holdings = [
            {
                "symbol": row["pdno"],
                "name": row["prdt_name"],
                "qty": int(row["hldg_qty"]),
                "avg_price": float(row["pchs_avg_pric"]),
                "eval_amt": int(row["evlu_amt"]),
                "pnl": int(row["evlu_pfls_amt"]),
            }
            for row in data.get("output1", [])
            if int(row.get("hldg_qty", 0)) > 0
        ]
        summary = (data.get("output2") or [{}])[0]

        def _amt(k: str) -> int:
            try:
                return int(float(summary.get(k, 0) or 0))
            except (TypeError, ValueError):
                return 0

        # D+2 정산 예수금이 매수/매도를 반영한 진짜 현금. 키 없으면 예수금총액 폴백.
        cash = (_amt("prvs_rcdl_excc_amt") if "prvs_rcdl_excc_amt" in summary
                else _amt("dnca_tot_amt"))
        total = _amt("nass_amt") or (cash + sum(h["eval_amt"] for h in holdings))
        return {"holdings": holdings, "cash": cash, "total": total,
                "deposit_total": _amt("dnca_tot_amt")}

    def position_qty(self, symbol: str) -> int:
        for h in self.balance()["holdings"]:
            if h["symbol"] == symbol:
                return h["qty"]
        return 0
