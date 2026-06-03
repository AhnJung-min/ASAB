"""국내주식 자동매매 대시보드 (Streamlit).

실행:  python -m streamlit run dashboard.py

탭: 📊 백테스트 / 🔍 스크리너 / 💰 계좌 / 🗂 수집 데이터
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from src.data.store import DataStore
from src.kis.client import KISApiError, KISClient
from src.kis.config import load_config
from src.kis.domestic import DomesticStock
from src.screener import screen
from src.backtest import run_backtest

st.set_page_config(page_title="국내주식 자동매매 대시보드", page_icon="📈", layout="wide")


# --- 공용 리소스 -----------------------------------------------------------
@st.cache_resource
def get_config():
    return load_config()


@st.cache_resource
def get_client() -> KISClient:
    return KISClient(get_config())


def open_store() -> DataStore:
    return DataStore()


# --- 캐시되는 조회 ---------------------------------------------------------
@st.cache_data(ttl=20)
def fetch_prices(symbols: tuple[str, ...]) -> dict[str, int]:
    dom = DomesticStock(get_client())
    out = {}
    for s in symbols:
        try:
            out[s] = dom.current_price(s)
        except KISApiError:
            out[s] = None
    return out


@st.cache_data(ttl=30)
def fetch_balance() -> dict:
    return DomesticStock(get_client()).balance()


@st.cache_data(ttl=60)
def get_screener(include_etf: bool) -> list[dict]:
    with open_store() as store:
        return screen(store, require_financials=not include_etf)


@st.cache_data(ttl=300)
def get_backtest(top_n: int, hold_days: int, cost_bps: float, include_etf: bool,
                 regime_filter: bool) -> dict:
    with open_store() as store:
        return run_backtest(
            store, top_n=top_n, hold_days=hold_days,
            cost_bps=cost_bps, require_financials=not include_etf,
            regime_filter=regime_filter,
        )


@st.cache_data(ttl=120)
def store_stats() -> dict:
    with open_store() as store:
        return store.stats()


@st.cache_data(ttl=120)
def store_symbols() -> list[tuple[str, str]]:
    with open_store() as store:
        return [(s, store.name_of(s)) for s in store.symbols()]


@st.cache_data(ttl=120)
def symbol_daily(symbol: str) -> pd.DataFrame:
    with open_store() as store:
        rows = store.get_daily(symbol)
    df = pd.DataFrame([dict(r) for r in rows])
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
        df = df.set_index("date")
    return df


@st.cache_data(ttl=120)
def latest_universe() -> pd.DataFrame:
    with open_store() as store:
        cur = store.conn.execute(
            "SELECT rank,symbol,name,price,change_pct,volume,value "
            "FROM universe_snapshot WHERE date=(SELECT MAX(date) FROM universe_snapshot) "
            "ORDER BY rank"
        )
        return pd.DataFrame([dict(r) for r in cur.fetchall()])


# --- 수집 진행률 (실시간) --------------------------------------------------
@st.fragment(run_every="5s")
def collection_progress():
    with open_store() as store:
        universe_n = store.conn.execute(
            "SELECT COUNT(*) c FROM stock_master").fetchone()["c"]
        total_rows = store.conn.execute(
            "SELECT COUNT(*) c FROM daily_price").fetchone()["c"]
        collected = store.conn.execute(
            "SELECT COUNT(*) FROM (SELECT symbol FROM daily_price "
            "GROUP BY symbol HAVING COUNT(*)>=1000)").fetchone()[0]
        liq_done = store.conn.execute(
            "SELECT COUNT(*) c FROM stock_master WHERE liquidity IS NOT NULL").fetchone()["c"]

    # 1) 시가총액 조사 진행률 (수집 우선순위 결정)
    lpct = (liq_done / universe_n) if universe_n else 0.0
    st.progress(min(lpct, 1.0),
                text=f"① 시가총액 조사: {liq_done:,} / {universe_n:,} 종목 ({lpct*100:.1f}%)")
    # 2) 10년 일봉 백필 진행률
    cpct = (collected / universe_n) if universe_n else 0.0
    st.progress(min(cpct, 1.0),
                text=f"② 10년 일봉 백필: {collected:,} / {universe_n:,} 종목 ({cpct*100:.1f}%)")

    a, b, c, d = st.columns(4)
    a.metric("시총 조사", f"{liq_done:,}")
    b.metric("백필 완료 종목", f"{collected:,}")
    c.metric("일봉 총 행수", f"{total_rows:,}")
    d.metric("갱신 시각", f"{pd.Timestamp.now():%H:%M:%S}")
    st.caption("5초마다 자동 갱신. 숫자가 오르면 수집이 진행 중입니다. "
               "①시가총액 조사 후 ②대형주부터 일봉을 백필합니다.")


# --- 사이드바 --------------------------------------------------------------
cfg = get_config()
st.sidebar.title("⚙️ 설정")
st.sidebar.caption(f"모드: {'🧪 모의투자' if cfg.paper_trading else '🔴 실전투자'}")
st.sidebar.caption(f"계좌: {cfg.account_no}-{cfg.account_product_code}")

st.sidebar.subheader("백테스트 파라미터")
top_n = st.sidebar.slider("보유 종목 수 (top N)", 1, 10, 3)
hold_days = st.sidebar.slider("리밸런스 주기 (영업일)", 5, 60, 20, step=5)
cost_bps = st.sidebar.slider("거래비용 (bps)", 0, 100, 25, step=5)
include_etf = st.sidebar.checkbox("ETF/ETN 포함", value=False)
regime_filter = st.sidebar.checkbox("시장국면 필터 (지수 200일선)", value=False,
                                    help="지수가 200일선 아래면 현금 보유 → 하락장 회피")

st.sidebar.subheader("실시간")
auto = st.sidebar.checkbox("자동 새로고침", value=False)
refresh_sec = st.sidebar.slider("주기(초)", 5, 60, 15, disabled=not auto)
if st.sidebar.button("🔄 지금 새로고침"):
    st.cache_data.clear()
    st.rerun()

st.title("📈 국내주식 자동매매 대시보드")

tab_bt, tab_screen, tab_acct, tab_data = st.tabs(
    ["📊 백테스트", "🔍 스크리너", "💰 계좌", "🗂 수집 데이터"]
)

# --- 백테스트 탭 -----------------------------------------------------------
with tab_bt:
    res = get_backtest(top_n, hold_days, float(cost_bps), include_etf, regime_filter)
    if "error" in res:
        st.warning(res["error"])
    else:
        m = res["metrics"]
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        c1.metric("누적수익률", f"{m['total_return']*100:+.1f}%",
                  f"vs 벤치 {(m['total_return']-m['bench_return'])*100:+.1f}%p")
        c2.metric("CAGR", f"{m['cagr']*100:+.1f}%")
        c3.metric("샤프지수", f"{m['sharpe']:.2f}")
        c4.metric("최대낙폭", f"{m['mdd']*100:.1f}%")
        c5.metric("연변동성", f"{m['volatility']*100:.1f}%")
        c6.metric("투자기간", f"{m.get('time_in_market', 1)*100:.0f}%",
                  "국면필터 ON" if res["params"].get("regime_filter") else "필터 OFF")

        df = pd.DataFrame(res["curve"])
        df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
        df = df.set_index("date").rename(
            columns={"strategy": "전략", "benchmark": "벤치마크(KODEX200)"}
        )
        st.subheader("수익곡선 (1.0 = 원금)")
        st.line_chart(df)

        dd = df[["전략"]].copy()
        dd["전략"] = dd["전략"] / dd["전략"].cummax() - 1
        st.subheader("낙폭 (Drawdown)")
        st.area_chart(dd.rename(columns={"전략": "낙폭"}))

        st.subheader("리밸런스 보유 종목 이력")
        log_df = pd.DataFrame(
            [{"날짜": h["date"], "보유종목": ", ".join(h["names"])}
             for h in res["holdings_log"]]
        )
        st.dataframe(log_df, width="stretch", hide_index=True)

        st.info(
            f"대상 종목 풀: {res['pool_size']}개 · "
            f"top{res['params']['top_n']} · {res['params']['hold_days']}일 리밸런스 · "
            f"비용 {res['params']['cost_bps']}bps\n\n"
            "⚠️ 최근 국내 데이터는 강세장 단일 국면이라 결과가 과적합될 수 있습니다. "
            "다국면(10년) 데이터 + 워크포워드 검증 필요."
        )


# --- 스크리너 탭 -----------------------------------------------------------
with tab_screen:
    st.subheader("🔍 종목 스크리너 (팩터 순위)")
    ranked = get_screener(include_etf)
    if not ranked:
        st.warning("후보가 없습니다. `python -m src.collect` 로 데이터를 먼저 수집하세요.")
    else:
        sdf = pd.DataFrame([
            {
                "순위": i, "종목명": c["name"], "코드": c["symbol"],
                "점수": round(c["score"], 3),
                "모멘텀%": round(c["momentum"] * 100, 1),
                "추세": "↑" if c["trend"] else "↓",
                "거래대금(억)": round(c["liquidity"] / 1e8),
            }
            for i, c in enumerate(ranked, 1)
        ])
        st.dataframe(sdf, width="stretch", hide_index=True)
        st.bar_chart(sdf.set_index("종목명")["점수"])


# --- 계좌 탭 (실시간) ------------------------------------------------------
def render_account():
    try:
        bal = fetch_balance()
    except KISApiError as e:
        st.error(f"잔고 조회 실패: {e}")
        return

    holdings = bal["holdings"]
    total_eval = sum(h.get("eval_amt", 0) for h in holdings)
    total_pnl = sum(h.get("pnl", 0) for h in holdings)
    c1, c2, c3 = st.columns(3)
    c1.metric("예수금", f"₩{bal['cash']:,}")
    c2.metric("주식 평가금액", f"₩{total_eval:,}")
    c3.metric("평가손익", f"₩{total_pnl:,}")

    st.subheader("📌 보유 종목")
    if holdings:
        hdf = pd.DataFrame(holdings).rename(columns={
            "symbol": "코드", "name": "종목명", "qty": "수량",
            "avg_price": "평균단가", "eval_amt": "평가금액", "pnl": "평가손익",
        })
        st.dataframe(hdf, width="stretch", hide_index=True)
    else:
        st.info("보유 종목이 없습니다 (모의계좌).")

    ranked = get_screener(include_etf)
    if ranked:
        syms = tuple(c["symbol"] for c in ranked[:5])
        prices = fetch_prices(syms)
        st.subheader("스크리너 상위 5종목 현재가")
        pdf = pd.DataFrame([
            {"종목명": next(c["name"] for c in ranked if c["symbol"] == s),
             "코드": s, "현재가": prices.get(s)}
            for s in syms
        ])
        st.dataframe(pdf, width="stretch", hide_index=True)
    st.caption(f"갱신: {pd.Timestamp.now():%H:%M:%S}")


with tab_acct:
    st.subheader("💰 모의계좌 (국내)")
    if auto:
        st.fragment(render_account, run_every=f"{refresh_sec}s")()
    else:
        render_account()


# --- 수집 데이터 탭 --------------------------------------------------------
with tab_data:
    st.subheader("⏳ 데이터 수집 진행 상황 (실시간)")
    collection_progress()
    st.divider()
    st.subheader("🗂 수집 데이터 현황")
    stats = store_stats()
    labels = {
        "daily_price": "일봉(행)", "investor_flow": "투자자동향",
        "financial_ratio": "재무비율", "universe_snapshot": "유니버스",
        "symbols": "종목 수",
    }
    show_stats = {k: v for k, v in stats.items() if k in labels}
    cols = st.columns(len(show_stats))
    for col, (k, v) in zip(cols, show_stats.items()):
        col.metric(labels[k], f"{v:,}")

    st.subheader("최근 유니버스 (거래대금 상위)")
    st.dataframe(latest_universe(), width="stretch", hide_index=True)

    st.subheader("개별 종목 차트")
    syms = store_symbols()
    if syms:
        labelmap = {f"{n} ({s})": s for s, n in syms}
        choice = st.selectbox("종목 선택", list(labelmap.keys()))
        df = symbol_daily(labelmap[choice])
        if not df.empty:
            st.line_chart(df[["close"]].rename(columns={"close": "종가"}))
            st.bar_chart(df[["volume"]].rename(columns={"volume": "거래량"}))
    else:
        st.info("수집된 종목이 없습니다.")
