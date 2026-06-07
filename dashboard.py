"""ASAB 데이터 수집 대시보드 (Streamlit).

수집 현황 모니터링 + 수집 데이터 점검에 집중.
**기본적으로 DB만 읽어 KIS API를 호출하지 않음 → 수집기와의 API 충돌(rate limit) 없음.**
(예외: 🧾 거래저널 탭의 '잔고 새로고침' 버튼만 클릭 시 1회 KIS 잔고 조회)

백테스트/스크리너/계좌는 데이터·전략이 준비되는 매매 단계에서 다시 붙인다.
지금은 CLI로 사용:  python -m src.backtest / src.screener / src.walkforward

실행:  python -m streamlit run dashboard.py
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from datetime import datetime

from src.data.store import DataStore
from src.kis.config import load_config
from src.kis.client import KISClient
from src.kis.domestic import DomesticStock
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
def ml_run(top_n: int, max_pool: int, hold_days: int = 20, embargo: int = 1) -> dict:
    with open_store() as store:
        ds = build_dataset(store, hold_days=hold_days, max_pool=max_pool)
    res = run_ml_wf(ds, top_n=top_n, embargo=embargo)
    # 누수 전·후 비교용: embargo=0(누수 포함) 성과를 함께 계산
    if "error" not in res and embargo > 0:
        leak = run_ml_wf(ds, top_n=top_n, embargo=0)
        if "error" not in leak:
            res["leak"] = {"ml": leak["ml"], "mom": leak["mom"]}
    return res


@st.cache_data(ttl=120)
def latest_universe() -> pd.DataFrame:
    with open_store() as store:
        cur = store.conn.execute(
            "SELECT rank,symbol,name,price,change_pct,volume,value "
            "FROM universe_snapshot WHERE date=(SELECT MAX(date) FROM universe_snapshot) "
            "ORDER BY rank"
        )
        return pd.DataFrame([dict(r) for r in cur.fetchall()])


# --- 거래 저널 데이터 (라이브 매매 기록, DB만 읽음) ------------------------
@st.cache_data(ttl=30)
def journal_data() -> dict:
    with open_store() as store:
        signals = [dict(r) for r in store.recent_live_signals(200)]
        orders = [dict(r) for r in store.recent_live_orders(200)]
        positions = list(store.get_positions().values())
        snaps = [dict(r) for r in store.account_snapshots(1000)]
    return {"signals": signals, "orders": orders,
            "positions": positions, "snaps": snaps}


@st.cache_data(ttl=120)
def trade_stats_df() -> pd.DataFrame:
    with open_store() as store:
        rows = [dict(r) for r in store.get_trade_stats()]
    return pd.DataFrame(rows)


def fetch_balance_snapshot() -> dict:
    """KIS 잔고를 1회 조회해 account_snapshot 으로 저장하고 잔고를 반환한다.
    버튼 클릭 시에만 호출(평소 대시보드는 API 미호출). 휴장일에도 동작(조회).
    """
    dom = DomesticStock(KISClient(get_config()))
    bal = dom.balance()
    hk = sum(h["eval_amt"] for h in bal["holdings"])
    uk = sum(h["pnl"] for h in bal["holdings"])
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open_store() as store:
        store.save_account_snapshot(ts, {
            "cash_krw": bal["cash"], "holdings_krw": hk,
            "total_krw": bal["cash"] + hk, "realized_krw": 0.0,
            "unrealized_krw": uk, "fx_rate": 1.0})
    return bal


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
tab_collect, tab_model, tab_journal, tab_macro, tab_data = st.tabs(
    ["📡 수집 현황", "🤖 모델", "🧾 거래저널", "🌐 거시(수출)", "🗂 데이터"])

with tab_collect:
    collection_progress()

# --- ML 모델 탭 ------------------------------------------------------------
with tab_model:
    st.subheader("🤖 ML 예측 모델 (LightGBM 워크포워드)")
    cc1, cc2, cc3 = st.columns(3)
    m_topn = cc1.slider("상위 N 종목", 5, 30, 10)
    m_pool = cc2.slider("유니버스(유동성 상위)", 100, 500, 300, step=50)
    m_emb = cc3.slider("Embargo(누수방지, 리밸런스)", 0, 3, 1,
                       help="train↔test 사이를 비우는 구간 수. 1이면 라벨 겹침(누수) 제거. "
                            "0은 누수 포함(낙관 편향)")
    if st.button("▶ 학습·검증 실행 (1~2분 소요)"):
        st.session_state["ml_go"] = (m_topn, m_pool, m_emb)

    go = st.session_state.get("ml_go")
    if not go:
        st.caption("버튼을 눌러 학습·검증을 실행하세요. (DB만 읽어 수집과 충돌 없음. "
                   "결과는 캐시되어 재조회는 즉시)")
    else:
        with st.spinner("데이터셋 생성 + LightGBM 워크포워드 검증 중... (1~2분)"):
            res = ml_run(go[0], go[1], embargo=go[2])
        if "error" in res:
            st.warning(res["error"])
        else:
            st.caption(f"OOS {res['n_test']}기간 · {res['span'][0]}~{res['span'][1]} · "
                       f"학습데이터 {res['dataset_rows']:,}행 · top{go[0]} / 유니버스{go[1]} · "
                       f"embargo={res.get('embargo', 0)}구간")
            cols = st.columns(3)
            for col, (k, label) in zip(cols, [("ml", "ML(LGBM)"), ("mom", "모멘텀"), ("mkt", "시장(평균)")]):
                m = res[k]
                col.metric(f"{label} 샤프", f"{m['sharpe']:.2f}",
                           f"누적 {m['total']*100:.0f}% · MDD {m['mdd']*100:.0f}%")

            # 정보 누수 전·후 비교 (embargo>0 일 때만)
            if res.get("leak"):
                lm, cm = res["leak"]["ml"], res["ml"]
                d_sh = lm["sharpe"] - cm["sharpe"]
                d_ret = (lm["total"] - cm["total"]) * 100
                with st.expander(f"🔬 정보 누수 영향: 샤프 +{d_sh:.2f} · 누적 +{d_ret:.0f}%p 부풀려짐", expanded=True):
                    cmp_df = pd.DataFrame(
                        {"샤프": [lm["sharpe"], cm["sharpe"]],
                         "누적수익%": [lm["total"] * 100, cm["total"] * 100]},
                        index=["누수 포함 (embargo=0)", f"보정 (embargo={res['embargo']})"])
                    st.dataframe(cmp_df.style.format("{:.2f}"), width="stretch")
                    st.caption("train↔test 경계에서 라벨이 겹치면 OOS 성과가 실제보다 좋아 보입니다. "
                               "위 차이만큼이 '누수로 부풀려진' 가짜 성과입니다. 보정값이 진짜 실력에 가깝습니다.")
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

# --- 거래 저널 탭 ----------------------------------------------------------
with tab_journal:
    st.subheader("🧾 거래 저널 (라이브 매매 기록)")
    st.caption("run_bot(screener_rotation)이 남긴 신호·주문·포지션·계좌 기록. DB만 읽음.")

    # 잔고 새로고침: 버튼 클릭 시에만 KIS 조회(휴장일에도 동작) → 스냅샷 저장
    bc1, bc2 = st.columns([1, 3])
    if bc1.button("🔄 잔고 새로고침 (KIS 조회)"):
        try:
            with st.spinner("KIS 잔고 조회 중..."):
                bal = fetch_balance_snapshot()
            st.session_state["live_balance"] = bal
            journal_data.clear()  # 캐시 무효화 → 카드/차트 갱신
            bc2.success(f"갱신 완료 · 예수금 ₩{bal['cash']:,} · 보유 {len(bal['holdings'])}종목")
        except Exception as e:  # noqa: BLE001 (API/네트워크 등 모든 실패 표시)
            bc2.error(f"잔고 조회 실패: {e}")

    lb = st.session_state.get("live_balance")
    if lb and lb["holdings"]:
        st.markdown("**실시간 보유(방금 조회)**")
        hdf = pd.DataFrame(lb["holdings"])[["symbol", "name", "qty", "avg_price", "eval_amt", "pnl"]]
        hdf.columns = ["코드", "종목", "수량", "평균매입가", "평가금액", "평가손익"]
        st.dataframe(hdf.style.format(
            {"평균매입가": "{:,.0f}", "평가금액": "{:,.0f}", "평가손익": "{:+,.0f}"}),
            width="stretch", hide_index=True)

    j = journal_data()
    snaps, positions, orders, signals = j["snaps"], j["positions"], j["orders"], j["signals"]

    if not (snaps or orders or signals or positions):
        st.info("아직 라이브 매매 기록이 없습니다.\n\n"
                "• 미리보기(주문 없음): `python -m src.run_bot --plan`\n"
                "• 모의주문 1회(평일 장중): `python -m src.run_bot --once`\n\n"
                "전략을 screener_rotation 으로 설정해야 합니다(config.yaml).")
    else:
        # 1) 계좌 요약 + 자산추이
        if snaps:
            last = snaps[-1]
            c = st.columns(4)
            c[0].metric("총자산", f"₩{last['total_krw']:,.0f}")
            c[1].metric("현금", f"₩{last['cash_krw']:,.0f}")
            c[2].metric("주식평가", f"₩{last['holdings_krw']:,.0f}")
            c[3].metric("평가손익", f"₩{last['unrealized_krw']:,.0f}")
            sdf = pd.DataFrame(snaps)
            sdf["ts"] = pd.to_datetime(sdf["ts"])
            sdf = sdf.set_index("ts")
            st.caption(f"계좌 자산추이 ({len(snaps)} 스냅샷)")
            st.line_chart(sdf[["total_krw", "cash_krw", "holdings_krw"]].rename(
                columns={"total_krw": "총자산", "cash_krw": "현금", "holdings_krw": "주식평가"}))

        # 2) 현재 포지션(트레일링 고점 추적)
        st.subheader("📦 현재 포지션")
        if positions:
            pdf = pd.DataFrame(positions)
            pdf["고점수익%"] = pdf.apply(
                lambda r: (r["peak_price"] / r["entry_price"] - 1) * 100
                if r["entry_price"] else 0.0, axis=1)
            pdf = pdf[["symbol", "name", "qty", "entry_price", "peak_price", "고점수익%", "updated_ts"]]
            pdf.columns = ["코드", "종목", "수량", "평균매입가", "보유중고점", "고점수익%", "갱신"]
            st.dataframe(pdf.style.format(
                {"평균매입가": "{:,.0f}", "보유중고점": "{:,.0f}", "고점수익%": "{:+.1f}"}),
                width="stretch", hide_index=True)
            st.caption("※ 실시간 현재가·손익은 봇(run_bot) 또는 KIS 앱에서 확인(대시보드는 API 미호출).")
        else:
            st.caption("보유 포지션 없음(현금).")

        # 3) 최근 주문
        st.subheader("🧾 최근 주문")
        if orders:
            odf = pd.DataFrame(orders)
            side_icon = {"buy": "🟢 매수", "sell": "🔴 매도"}
            dvsn = {"00": "지정가", "01": "시장가"}
            odf["구분"] = odf["side"].map(side_icon).fillna(odf["side"])
            odf["유형"] = odf["ord_dvsn"].map(dvsn).fillna(odf["ord_dvsn"])
            view = odf[["ts", "구분", "name", "qty", "price", "유형", "mode", "status", "msg"]]
            view.columns = ["시각", "구분", "종목", "수량", "가격", "유형", "모드", "상태", "메시지"]
            st.dataframe(view, width="stretch", hide_index=True, height=280)
        else:
            st.caption("주문 기록 없음.")

        # 4) 최근 신호
        st.subheader("📨 최근 신호")
        if signals:
            gdf = pd.DataFrame(signals)
            view = gdf[["ts", "action", "name", "rank", "score", "reason"]]
            view.columns = ["시각", "액션", "종목", "순위", "점수", "사유"]
            st.dataframe(view, width="stretch", hide_index=True, height=280)
        else:
            st.caption("신호 기록 없음.")


# --- 거시(수출입동향) 탭 ---------------------------------------------------
with tab_macro:
    st.subheader("🌐 수출입동향 (MOTIE 월간 거시지표)")
    st.caption("`python -m src.trade_stats <PDF> --save` 로 매달 적재. 달이 쌓일수록 추이가 채워집니다.")
    tdf = trade_stats_df()
    if tdf.empty:
        st.info("아직 적재된 수출입동향이 없습니다.\n\n"
                "PDF를 받으면: `python -m src.trade_stats \"2026년 4월 수출입동향.pdf\" --save`")
    else:
        months = sorted(tdf["month"].unique())
        latest = months[-1]

        def _v(metric, field, month=latest):
            r = tdf[(tdf.metric == metric) & (tdf.field == field) & (tdf.month == month)]
            return float(r["value"].iloc[0]) if len(r) else None

        st.markdown(f"**기준월: {latest}**  (적재 {len(months)}개월)")
        c = st.columns(3)
        c[0].metric("총수출", f"{_v('export','usd_bil') or 0:,.1f}억$",
                    f"{_v('export','yoy') or 0:+.1f}%")
        c[1].metric("총수입", f"{_v('import','usd_bil') or 0:,.1f}억$",
                    f"{_v('import','yoy') or 0:+.1f}%")
        c[2].metric("무역수지", f"{_v('balance','usd_bil') or 0:+,.1f}억$")

        # 품목별 최신 YoY (수혜/역풍 한눈에)
        skip = {"export", "import", "balance", "전체"}
        item_rows = tdf[(tdf.field == "yoy") & (tdf.month == latest)
                        & (~tdf.metric.isin(skip)) & (~tdf.metric.str.startswith("mem_"))]
        if not item_rows.empty:
            bar = item_rows.set_index("metric")["value"].sort_values(ascending=False)
            st.markdown("**품목별 수출 증감률(YoY %)** — 🟢수혜 / 🔴역풍")
            st.bar_chart(bar, color="#4C9BE8")

        # 품목별 수출액 추이(달이 쌓이면 라인이 길어짐)
        amt = tdf[(tdf.field == "usd_bil") & (~tdf.metric.isin({"export","import","balance"}))
                  & (~tdf.metric.str.startswith("mem_")) & (tdf.metric != "전체")]
        if not amt.empty and len(months) >= 2:
            pivot = amt.pivot_table(index="month", columns="metric", values="value")
            st.markdown("**품목별 수출액 추이(억$)**")
            st.line_chart(pivot)
        elif not amt.empty:
            st.caption("※ 품목별 추이 차트는 2개월 이상 적재되면 표시됩니다(현재 1개월).")

        # 메모리 고정가
        mem = tdf[(tdf.month == latest) & (tdf.metric.str.startswith("mem_"))
                  & (tdf.field == "price_usd")]
        if not mem.empty:
            mc = st.columns(len(mem))
            for col, (_, row) in zip(mc, mem.iterrows()):
                name = row["metric"].replace("mem_", "")
                yoy = _v(row["metric"], "yoy")
                col.metric(f"{name} 고정가", f"{row['value']}$",
                           f"{yoy:+.0f}%" if yoy is not None else None)

        st.caption("⚠️ 수출동향은 1개월 시차의 후행지표 — 매매 직결이 아니라 '거시 확인·섹터 참고'용. "
                   "신호화(C)는 여러 달 누적 후 백테스트 검증 필요.")


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
