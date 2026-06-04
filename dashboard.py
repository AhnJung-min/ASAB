"""ASAB 데이터 수집 대시보드 (Streamlit).

수집 현황 모니터링 + 수집 데이터 점검에 집중.
**DB만 읽으므로 KIS API를 호출하지 않음 → 수집기와의 API 충돌(rate limit) 없음.**

백테스트/스크리너/계좌는 데이터·전략이 준비되는 매매 단계에서 다시 붙인다.
지금은 CLI로 사용:  python -m src.backtest / src.screener / src.walkforward

실행:  python -m streamlit run dashboard.py
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from src.data.store import DataStore
from src.kis.config import load_config

st.set_page_config(page_title="ASAB 수집 대시보드", page_icon="📡", layout="wide")


@st.cache_resource
def get_config():
    return load_config()


def open_store() -> DataStore:
    return DataStore()


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


# --- 수집 현황 모니터 (실시간) ---------------------------------------------
def _eta(logs):
    """최근 backfill 로그 타임스탬프로 수집 속도 추정 → (종목/초, span)."""
    ts = [r["ts"] for r in logs if r["kind"] == "backfill"][:20]
    if len(ts) < 2:
        return None
    def sec(t):
        h, m, s = map(int, t.split(":"))
        return h * 3600 + m * 60 + s
    span = sec(ts[0]) - sec(ts[-1])
    if span <= 0:
        return None
    return (len(ts) - 1) / span


@st.fragment(run_every="5s")
def collection_progress():
    with open_store() as store:
        c = store.conn
        universe_n = c.execute("SELECT COUNT(*) FROM stock_master").fetchone()[0]
        total_rows = c.execute("SELECT COUNT(*) FROM daily_price").fetchone()[0]
        collected = c.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT d.symbol FROM daily_price d "
            "JOIN stock_master m ON d.symbol=m.symbol)").fetchone()[0]
        full_hist = c.execute("SELECT COUNT(*) FROM (SELECT symbol FROM daily_price "
                              "GROUP BY symbol HAVING COUNT(*)>=1000)").fetchone()[0]
        liq_done = c.execute("SELECT COUNT(*) FROM stock_master WHERE liquidity IS NOT NULL").fetchone()[0]
        errs = c.execute("SELECT COUNT(*) FROM collect_log WHERE kind='error'").fetchone()[0]
        mk = {row[0]: row[1] for row in c.execute(
            "SELECT market, COUNT(*) FROM stock_master GROUP BY market")}
        mk_done = {row[0]: row[1] for row in c.execute(
            "SELECT m.market, COUNT(DISTINCT d.symbol) FROM daily_price d "
            "JOIN stock_master m ON d.symbol=m.symbol GROUP BY m.market")}
        logs = store.recent_collect_log(200)

    cpct = (collected / universe_n) if universe_n else 0.0
    eta_txt = "거의 완료" if collected >= universe_n else "측정 중…"
    rate = _eta(logs)
    if rate and collected < universe_n:
        secs = (universe_n - collected) / rate
        h, m = int(secs // 3600), int((secs % 3600) // 60)
        eta_txt = f"약 {h}시간 {m}분" if h else f"약 {m}분"

    st.subheader("📡 데이터 수집 현황")
    st.progress(min(cpct, 1.0),
                text=f"일봉 백필: {collected:,} / {universe_n:,} 종목  ({cpct*100:.1f}%)")
    a, b, c1, d = st.columns(4)
    a.metric("수집된 종목", f"{collected:,} / {universe_n:,}", f"10년완비 {full_hist:,}")
    b.metric("일봉 총 행수", f"{total_rows:,}")
    c1.metric("예상 완료", eta_txt)
    d.metric("오류", f"{errs:,}건", delta_color="off")

    cols = st.columns(2)
    for col, mkt in zip(cols, ("KOSPI", "KOSDAQ")):
        tot, dn = mk.get(mkt, 0), mk_done.get(mkt, 0)
        col.progress(min(dn / tot, 1.0) if tot else 0.0, text=f"{mkt}: {dn:,} / {tot:,}")
    st.caption(f"5초마다 자동 갱신 · 시총조사 {liq_done:,}/{universe_n:,} · 갱신 {pd.Timestamp.now():%H:%M:%S}")

    st.markdown("**🧾 실시간 수집 로그 (최근 25건)**")
    if logs:
        icon = {"backfill": "📈", "liquidity": "🔎", "error": "⚠️"}
        feed = pd.DataFrame([
            {"시각": r["ts"], "구분": icon.get(r["kind"], r["kind"]),
             "종목": f'{r["name"]}({r["symbol"]})', "내용": r["detail"]}
            for r in logs[:25]
        ])
        st.dataframe(feed, width="stretch", hide_index=True, height=300)
    else:
        st.caption("아직 로그가 없습니다.")


# --- 사이드바 --------------------------------------------------------------
cfg = get_config()
st.sidebar.title("⚙️ ASAB")
st.sidebar.caption(f"모드: {'🧪 모의투자' if cfg.paper_trading else '🔴 실전투자'}")
st.sidebar.caption(f"계좌: {cfg.account_no}-{cfg.account_product_code}")
st.sidebar.caption("수집 현황은 5초마다 자동 갱신됩니다.")
if st.sidebar.button("🔄 캐시 비우기 / 새로고침"):
    st.cache_data.clear()
    st.rerun()
st.sidebar.caption("백테스트·스크리너는 CLI에서:\n`python -m src.walkforward`")

st.title("📡 ASAB 데이터 수집 대시보드")
tab_collect, tab_data = st.tabs(["📡 수집 현황", "🗂 데이터"])

with tab_collect:
    collection_progress()

with tab_data:
    st.subheader("🗂 수집 데이터 상세")
    stats = store_stats()
    labels = {
        "daily_price": "일봉(행)", "investor_flow": "투자자동향",
        "financial_ratio": "재무비율", "universe_snapshot": "유니버스",
        "symbols": "종목 수",
    }
    show = {k: v for k, v in stats.items() if k in labels}
    cols = st.columns(len(show))
    for col, (k, v) in zip(cols, show.items()):
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
