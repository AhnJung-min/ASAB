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
    def current_price(self, symbol: str) -> int:
        """현재가(원) 조회. symbol: 6자리 종목코드."""
        data = self.c.get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            tr_id="FHKST01010100",  # 시세는 모의/실전 동일
            params={"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
        )
        return int(data["output"]["stck_prpr"])

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
        """보유 종목 및 예수금 조회. 반환: {'holdings': [...], 'cash': int}."""
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
        cash = int(summary.get("dnca_tot_amt", 0))  # 예수금총금액
        return {"holdings": holdings, "cash": cash}

    def position_qty(self, symbol: str) -> int:
        for h in self.balance()["holdings"]:
            if h["symbol"] == symbol:
                return h["qty"]
        return 0
