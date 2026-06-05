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
from src.features import build_dataset
from src.ml import run_ml_wf

st.set_page_config(page_title="ASAB 대시보드", page_icon="📡", layout="wide")


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


@st.cache_data(ttl=3600, show_spinner=False)
def ml_run(top_n: int, max_pool: int, hold_days: int = 20) -> dict:
    with open_store() as store:
        ds = build_dataset(store, hold_days=hold_days, max_pool=max_pool)
    return run_ml_wf(ds, top_n=top_n)


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

st.title("📡 ASAB 대시보드")
tab_collect, tab_model, tab_data = st.tabs(["📡 수집 현황", "🤖 모델", "🗂 데이터"])

with tab_collect:
    collection_progress()

# --- ML 모델 탭 ------------------------------------------------------------
with tab_model:
    st.subheader("🤖 ML 예측 모델 (LightGBM 워크포워드)")
    cc1, cc2 = st.columns(2)
    m_topn = cc1.slider("상위 N 종목", 5, 30, 10)
    m_pool = cc2.slider("유니버스(유동성 상위)", 100, 500, 300, step=50)
    if st.button("▶ 학습·검증 실행 (1~2분 소요)"):
        st.session_state["ml_go"] = (m_topn, m_pool)

    go = st.session_state.get("ml_go")
    if not go:
        st.caption("버튼을 눌러 학습·검증을 실행하세요. (DB만 읽어 수집과 충돌 없음. "
                   "결과는 캐시되어 재조회는 즉시)")
    else:
        with st.spinner("데이터셋 생성 + LightGBM 워크포워드 검증 중... (1~2분)"):
            res = ml_run(go[0], go[1])
        if "error" in res:
            st.warning(res["error"])
        else:
            st.caption(f"OOS {res['n_test']}기간 · {res['span'][0]}~{res['span'][1]} · "
                       f"학습데이터 {res['dataset_rows']:,}행 · top{go[0]} / 유니버스{go[1]}")
            cols = st.columns(3)
            for col, (k, label) in zip(cols, [("ml", "ML(LGBM)"), ("mom", "모멘텀"), ("mkt", "시장(평균)")]):
                m = res[k]
                col.metric(f"{label} 샤프", f"{m['sharpe']:.2f}",
                           f"누적 {m['total']*100:.0f}% · MDD {m['mdd']*100:.0f}%")
            cv = res["curves"]
            curve_df = pd.DataFrame(
                {"ML": cv["ml"], "모멘텀": cv["mom"], "시장": cv["mkt"]},
                index=pd.to_datetime(cv["dates"], format="%Y%m%d"))
            st.subheader("OOS 누적수익 곡선 (1.0 = 시작)")
            st.line_chart(curve_df)
            st.subheader("피처 중요도")
            imp_df = pd.DataFrame(res["importance"], columns=["피처", "중요도"]).set_index("피처")
            st.bar_chart(imp_df)
            ml_sh, mom_sh = res["ml"]["sharpe"], res["mom"]["sharpe"]
            verdict = ("✅ ML이 모멘텀보다 위험효율(샤프) 우위" if ml_sh > mom_sh
                       else "⚠️ 이번 설정에선 ML이 모멘텀을 못 이김")
            st.info(f"{verdict}. (절대수익은 강세장 영향 → 상대비교가 핵심)")

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
