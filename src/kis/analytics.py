"""1군 분석 데이터 조회 (실전 도메인 전용).

공매도·신용잔고·프로그램매매·투자자매매동향·대차거래 — 모두 모의투자 미지원이라
실전 도메인(openapi.koreainvestment.com)으로 호출해야 한다.
같은 appkey/secret 으로 실전 도메인 시세 조회가 가능함(probe 로 확인).

사용:
    real_cfg = dataclasses.replace(cfg, paper_trading=False)
    real_client = KISClient(real_cfg)
    ana = MarketAnalytics(real_client)
    ana.short_sale("005930", "20260101", "20260605")
"""
from __future__ import annotations

from typing import Any

from .client import KISClient


def _i(v: Any) -> int | None:
    """문자열 → int. 부호/콤마/공백 처리, 빈값은 None."""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s == "" or s == "-":
        return None
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return None


def _f(v: Any) -> float | None:
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if s == "" or s == "-":
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


class MarketAnalytics:
    def __init__(self, client: KISClient):
        self.c = client

    # --- 종목별 투자자매매동향(일별): 외국인/기관/개인 순매수 ----------------
    def investor_daily(self, symbol: str, date: str) -> list[dict[str, Any]]:
        """date 까지의 최근 ~30영업일 일별 순매수. date: 'YYYYMMDD'."""
        data = self.c.get(
            "/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily",
            tr_id="FHPTJ04160001",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": date,
                "FID_ORG_ADJ_PRC": "",
                "FID_ETC_CLS_CODE": "1",
            },
        )
        rows = []
        for r in data.get("output2", []):
            if not r.get("stck_bsop_date"):
                continue
            rows.append({
                "date": r["stck_bsop_date"],
                "foreign": _i(r.get("frgn_ntby_qty")),
                "institution": _i(r.get("orgn_ntby_qty")),
                "individual": _i(r.get("prsn_ntby_qty")),
            })
        rows.sort(key=lambda x: x["date"])
        return rows

    # --- 공매도 일별추이 ----------------------------------------------------
    def short_sale(self, symbol: str, start: str, end: str) -> list[dict[str, Any]]:
        data = self.c.get(
            "/uapi/domestic-stock/v1/quotations/daily-short-sale",
            tr_id="FHPST04830000",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": start,
                "FID_INPUT_DATE_2": end,
            },
        )
        rows = []
        for r in data.get("output2", []):
            if not r.get("stck_bsop_date"):
                continue
            rows.append({
                "date": r["stck_bsop_date"],
                "short_qty": _i(r.get("ssts_cntg_qty")),
                "short_vol_ratio": _f(r.get("ssts_vol_rlim")),
                "short_value": _i(r.get("ssts_tr_pbmn")),
                "short_value_ratio": _f(r.get("ssts_tr_pbmn_rlim")),
            })
        rows.sort(key=lambda x: x["date"])
        return rows

    # --- 신용잔고 일별추이 --------------------------------------------------
    def credit_balance(self, symbol: str, date: str) -> list[dict[str, Any]]:
        data = self.c.get(
            "/uapi/domestic-stock/v1/quotations/daily-credit-balance",
            tr_id="FHPST04760000",
            params={
                "fid_cond_mrkt_div_code": "J",
                "fid_cond_scr_div_code": "20476",
                "fid_input_iscd": symbol,
                "fid_input_date_1": date,
            },
        )
        rows = []
        for r in data.get("output", []):
            d = r.get("deal_date") or r.get("stlm_date")
            if not d:
                continue
            rows.append({
                "date": d,
                "loan_rmnd_qty": _i(r.get("whol_loan_rmnd_stcn")),
                "loan_rmnd_rate": _f(r.get("whol_loan_rmnd_rate")),
                "loan_gvrt": _f(r.get("whol_loan_gvrt")),
                "stln_rmnd_qty": _i(r.get("whol_stln_rmnd_stcn")),
            })
        rows.sort(key=lambda x: x["date"])
        return rows

    # --- 종목별 프로그램매매추이(일별) --------------------------------------
    def program_trade(self, symbol: str, date: str) -> list[dict[str, Any]]:
        data = self.c.get(
            "/uapi/domestic-stock/v1/quotations/program-trade-by-stock-daily",
            tr_id="FHPPG04650201",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": date,
            },
        )
        rows = []
        for r in data.get("output", []):
            if not r.get("stck_bsop_date"):
                continue
            rows.append({
                "date": r["stck_bsop_date"],
                "prog_net_qty": _i(r.get("whol_smtn_ntby_qty")),
                "prog_net_value": _i(r.get("whol_smtn_ntby_tr_pbmn")),
            })
        rows.sort(key=lambda x: x["date"])
        return rows

    # --- 종목별 일별 대차거래추이 -------------------------------------------
    def loan_trans(self, symbol: str, start: str, end: str) -> list[dict[str, Any]]:
        data = self.c.get(
            "/uapi/domestic-stock/v1/quotations/daily-loan-trans",
            tr_id="HHPST074500C0",
            params={
                "MRKT_DIV_CLS_CODE": "3",   # 3=종목
                "MKSC_SHRN_ISCD": symbol,
                "START_DATE": start,
                "END_DATE": end,
                "CTS": "",
            },
        )
        # 문서상 응답 키가 output1/output2 혼재 → 둘 다 시도
        out = data.get("output2") or data.get("output1") or data.get("output") or []
        rows = []
        for r in out:
            d = r.get("bsop_date")
            if not d:
                continue
            rows.append({
                "date": d,
                "loan_new_qty": _i(r.get("new_stcn")),
                "loan_redeem_qty": _i(r.get("rdmp_stcn")),
                "loan_rmnd_qty": _i(r.get("rmnd_stcn")),
                "loan_rmnd_amt": _i(r.get("rmnd_amt")),
            })
        rows.sort(key=lambda x: x["date"])
        return rows
