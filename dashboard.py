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

st.set_page_config(page_title="ASAB", page_icon="📈", layout="wide")

# --- 토스증권 웹풍 다크 스타일 ---------------------------------------------
BG, CARD, BORDER, INK, GRAY = "#17171C", "#1E1F25", "#2A2C33", "#E5E8EB", "#8B95A1"
TOSS_BLUE, UP_RED, DOWN_BLUE = "#3182F6", "#F04452", "#4D7EFF"

st.markdown(f"""
<style>
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.min.css');
html, body, [class*="css"], button, input {{ font-family: 'Pretendard', -apple-system, sans-serif; }}
#MainMenu, footer, header [data-testid="stToolbar"] {{ visibility: hidden; }}
.block-container {{ padding-top: 2rem; padding-bottom: 3rem; max-width: 1240px; }}
div[data-testid="stMetric"] {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 16px; padding: 14px 18px;
}}
div[data-testid="stMetricLabel"] p {{ color: {GRAY}; font-size: 0.8rem; font-weight: 600; }}
div[data-testid="stMetricValue"] {{ font-size: 1.45rem; font-weight: 700; color: {INK}; }}
.stTabs [data-baseweb="tab-list"] {{ gap: 2px; border-bottom: 1px solid {BORDER}; }}
.stTabs [data-baseweb="tab"] {{ font-weight: 600; color: {GRAY}; padding: 10px 16px; }}
.stTabs [aria-selected="true"] {{ color: {INK}; }}
div[data-testid="stDataFrame"] {{ border-radius: 12px; overflow: hidden; }}
.stButton button {{ border-radius: 12px; font-weight: 600; border: 1px solid {BORDER}; }}
section[data-testid="stSidebar"] {{ background: {CARD}; border-right: 1px solid {BORDER}; }}
hr {{ margin: 0.8rem 0; border-color: {BORDER}; }}
</style>
""", unsafe_allow_html=True)


def kcolor(v: float) -> str:
    """한국식 등락 색: 상승=빨강 / 하락=파랑 / 보합=회색."""
    return UP_RED if v > 0 else (DOWN_BLUE if v < 0 else GRAY)


def hero_card(label: str, value: str, sub_html: str = "") -> str:
    return (f"<div style='background:{CARD};border:1px solid {BORDER};border-radius:20px;"
            f"padding:20px 24px'>"
            f"<div style='color:{GRAY};font-size:.85rem;font-weight:600;margin-bottom:6px'>{label}</div>"
            f"<div style='color:{INK};font-size:2.0rem;font-weight:800;letter-spacing:-.5px'>{value}</div>"
            f"<div style='margin-top:6px;font-size:.95rem;font-weight:600'>{sub_html}</div></div>")


def market_status() -> str:
    """국내 장 상태 배지(KST 평일 09:00~15:30 기준 근사)."""
    from datetime import datetime
    now = datetime.now()
    is_open = now.weekday() < 5 and (9 * 60) <= (now.hour * 60 + now.minute) < (15 * 60 + 30)
    dot, txt = ("#F04452", "국내 장 열림") if is_open else ("#6B7684", "국내 장 닫힘")
    return (f"<span style='display:inline-flex;align-items:center;gap:6px;color:{GRAY};font-size:.85rem'>"
            f"<span style='width:8px;height:8px;border-radius:50%;background:{dot};display:inline-block'></span>"
            f"{txt}</span>")


def rank_row(rank: int, name: str, code: str, score: float, score_max: float) -> str:
    """토스풍 순위 행: 순위 · 종목 · 점수 + 점수 바."""
    w = int(min(max(score / score_max, 0), 1) * 100) if score_max else 0
    return (f"<div style='display:flex;align-items:center;gap:12px;padding:11px 14px;"
            f"border-bottom:1px solid {BORDER}'>"
            f"<div style='width:22px;color:{GRAY};font-weight:700;text-align:center'>{rank}</div>"
            f"<div style='flex:1;min-width:0'>"
            f"<div style='color:{INK};font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis'>{name}</div>"
            f"<div style='color:{GRAY};font-size:.75rem'>{code}</div></div>"
            f"<div style='width:120px'>"
            f"<div style='height:6px;background:{BORDER};border-radius:3px;overflow:hidden'>"
            f"<div style='height:100%;width:{w}%;background:{TOSS_BLUE}'></div></div></div>"
            f"<div style='width:54px;text-align:right;color:{INK};font-weight:700'>{score:.3f}</div>"
            f"</div>")


def holding_row(name: str, code: str, top: str, bottom: str, color: str) -> str:
    """토스풍 보유종목 행: 종목 · (우측 평가/손익)."""
    return (f"<div style='display:flex;align-items:center;gap:10px;padding:10px 14px;"
            f"border-bottom:1px solid {BORDER}'>"
            f"<div style='flex:1;min-width:0'>"
            f"<div style='color:{INK};font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis'>{name}</div>"
            f"<div style='color:{GRAY};font-size:.72rem'>{code}</div></div>"
            f"<div style='text-align:right'><div style='color:{INK};font-weight:600'>{top}</div>"
            f"<div style='color:{color};font-size:.8rem;font-weight:600'>{bottom}</div></div></div>")


def card_wrap(inner: str) -> str:
    return (f"<div style='background:{CARD};border:1px solid {BORDER};border-radius:16px;"
            f"overflow:hidden'>{inner}</div>")


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


@st.cache_data(ttl=120)
def export_hist_pivot() -> pd.DataFrame:
    """섹터별 월별 수출액 시계열(억$). index='YYYY-MM', columns=섹터."""
    with open_store() as store:
        rows = [dict(r) for r in store.get_export_history()]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    piv = df.pivot(index="month", columns="sector", values="export_kusd").sort_index()
    return piv / 100_000.0  # 천불 → 억$


@st.cache_data(ttl=60)
def analytics_overview() -> dict:
    """1군 분석 데이터(수급) 적재 현황 + 보유 종목 목록."""
    with open_store() as store:
        c = store.conn
        out: dict = {}
        for t in ("short_sale", "credit_balance", "program_trade", "loan_trans", "investor_flow"):
            r = c.execute(f"SELECT COUNT(*), COUNT(DISTINCT symbol) FROM {t}").fetchone()
            out[t] = (r[0], r[1])
        out["symbols"] = [row[0] for row in c.execute(
            "SELECT DISTINCT symbol FROM short_sale ORDER BY symbol")]
    return out


@st.cache_data(ttl=60)
def analytics_symbol(symbol: str) -> dict:
    """종목별 수급 시계열(투자자/공매도/신용/프로그램) DataFrame 묶음."""
    with open_store() as store:
        def q(sql: str) -> pd.DataFrame:
            df = pd.read_sql_query(sql, store.conn, params=(symbol,))
            if not df.empty:
                df["date"] = pd.to_datetime(df["date"], format="%Y%m%d")
                df = df.set_index("date")
            return df
        return {
            "investor": q("SELECT date, foreign_qty, institution_qty FROM investor_flow "
                          "WHERE symbol=? ORDER BY date"),
            "short": q("SELECT date, short_vol_ratio FROM short_sale WHERE symbol=? ORDER BY date"),
            "credit": q("SELECT date, loan_rmnd_rate FROM credit_balance WHERE symbol=? ORDER BY date"),
            "program": q("SELECT date, prog_net_qty FROM program_trade WHERE symbol=? ORDER BY date"),
        }


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

_mode_badge = ("🧪 모의투자" if cfg.paper_trading else "🔴 실전투자")
st.markdown(
    f"<div style='display:flex;align-items:baseline;gap:12px;margin-bottom:2px'>"
    f"<span style='font-size:1.9rem;font-weight:800;letter-spacing:-.5px;color:{INK}'>ASAB</span>"
    f"<span style='color:{GRAY};font-weight:600'>자동매매 모니터</span>"
    f"<span style='margin-left:auto;color:{GRAY};font-size:.85rem'>{_mode_badge} · {cfg.account_no}-{cfg.account_product_code}</span>"
    f"</div>", unsafe_allow_html=True)
st.markdown(f"<div style='color:{GRAY};font-size:.85rem;margin-bottom:14px'>국내 단일 트랙 · 검증 중심</div>",
            unsafe_allow_html=True)
tab_journal, tab_macro, tab_model, tab_collect, tab_data = st.tabs(
    ["🧾 거래", "🌐 거시", "🤖 모델", "📡 수집", "🗂 데이터"])

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

# --- 거래 탭 (토스풍 2분할: 좌=타깃순위 / 우=내 투자) -----------------------
with tab_journal:
    head = st.columns([3, 1])
    head[1].markdown(f"<div style='text-align:right;padding-top:6px'>{market_status()}</div>",
                     unsafe_allow_html=True)
    if head[0].button("🔄 잔고 새로고침 (KIS 조회)"):
        try:
            with st.spinner("KIS 잔고 조회 중..."):
                bal = fetch_balance_snapshot()
            st.session_state["live_balance"] = bal
            journal_data.clear()
            st.toast(f"갱신 완료 · 예수금 {bal['cash']:,}원 · 보유 {len(bal['holdings'])}종목")
        except Exception as e:  # noqa: BLE001
            st.error(f"잔고 조회 실패: {e}")

    j = journal_data()
    snaps, positions, orders, signals = j["snaps"], j["positions"], j["orders"], j["signals"]
    lb = st.session_state.get("live_balance")

    left, right = st.columns([1.4, 1], gap="large")

    # ── 좌: 봇 타깃 순위 (저널 최신 target 신호) ──
    with left:
        st.markdown("<div style='font-weight:700;font-size:1.05rem;margin-bottom:8px'>🎯 봇 타깃 순위</div>",
                    unsafe_allow_html=True)
        targets = [s for s in signals if s.get("action") == "target"]
        if targets:
            latest_ts = targets[0]["ts"]            # signals는 최신순(id DESC)
            rows = sorted([s for s in targets if s["ts"] == latest_ts],
                          key=lambda r: (r.get("rank") or 999))
            smax = max((r.get("score") or 0) for r in rows) or 1.0
            body = "".join(rank_row(r.get("rank") or i + 1, r.get("name") or r["symbol"],
                                    r["symbol"], r.get("score") or 0.0, smax)
                           for i, r in enumerate(rows))
            st.markdown(card_wrap(body), unsafe_allow_html=True)
            st.caption(f"기준일 {rows[0].get('asof')} · 산출 {latest_ts}")
        else:
            st.markdown(card_wrap(f"<div style='padding:24px;color:{GRAY}'>타깃 기록이 없습니다. "
                                  f"<br>`python -m src.run_bot --plan` 실행 후 표시됩니다.</div>"),
                        unsafe_allow_html=True)

    # ── 우: 내 투자 (총자산 + 보유) ──
    with right:
        st.markdown("<div style='font-weight:700;font-size:1.05rem;margin-bottom:8px'>내 투자</div>",
                    unsafe_allow_html=True)
        if snaps:
            last = snaps[-1]
            pnl = last["unrealized_krw"]
            base = last["total_krw"] - pnl
            pct = (pnl / base * 100) if base else 0.0
            sg = "+" if pnl >= 0 else ""
            sub = f"<span style='color:{kcolor(pnl)}'>{sg}{pnl:,.0f}원 ({sg}{pct:.2f}%)</span>"
            st.markdown(hero_card("총자산", f"{last['total_krw']:,.0f}원", sub), unsafe_allow_html=True)
            mc = st.columns(2)
            mc[0].metric("현금", f"{last['cash_krw']:,.0f}원")
            mc[1].metric("주식평가", f"{last['holdings_krw']:,.0f}원")
        else:
            st.markdown(hero_card("총자산", "—", "🔄 잔고 새로고침을 눌러주세요"), unsafe_allow_html=True)

        st.markdown(f"<div style='color:{GRAY};font-size:.85rem;font-weight:600;margin:12px 0 4px'>보유 종목</div>",
                    unsafe_allow_html=True)
        if lb and lb["holdings"]:
            body = "".join(holding_row(h["name"], h["symbol"], f"{h['eval_amt']:,}원",
                                       ("+" if h["pnl"] >= 0 else "") + f"{h['pnl']:,}원", kcolor(h["pnl"]))
                           for h in lb["holdings"])
            st.markdown(card_wrap(body), unsafe_allow_html=True)
        elif positions:
            body = "".join(holding_row(p["name"], p["symbol"], f"{p['qty']}주", "고점 추적중", GRAY)
                           for p in positions)
            st.markdown(card_wrap(body), unsafe_allow_html=True)
        else:
            st.caption("보유 없음 (현금)")

    # ── 하단: 자산추이 · 최근 활동(접이식) ──
    st.write("")
    if snaps:
        with st.expander("📈 계좌 자산추이", expanded=False):
            sdf = pd.DataFrame(snaps)
            sdf["ts"] = pd.to_datetime(sdf["ts"])
            sdf = sdf.set_index("ts")
            st.line_chart(sdf[["total_krw", "cash_krw", "holdings_krw"]].rename(
                columns={"total_krw": "총자산", "cash_krw": "현금", "holdings_krw": "주식평가"}))

    with st.expander("🧾 최근 주문", expanded=bool(orders)):
        if orders:
            odf = pd.DataFrame(orders)
            odf["구분"] = odf["side"].map({"buy": "🔴 매수", "sell": "🔵 매도"}).fillna(odf["side"])
            odf["유형"] = odf["ord_dvsn"].map({"00": "지정가", "01": "시장가"}).fillna(odf["ord_dvsn"])
            view = odf[["ts", "구분", "name", "qty", "price", "유형", "mode", "status", "msg"]]
            view.columns = ["시각", "구분", "종목", "수량", "가격", "유형", "모드", "상태", "메시지"]
            st.dataframe(view, width="stretch", hide_index=True, height=240)
        else:
            st.caption("주문 기록 없음.")

    with st.expander("📨 최근 신호"):
        if signals:
            gdf = pd.DataFrame(signals)
            view = gdf[["ts", "action", "name", "rank", "score", "reason"]]
            view.columns = ["시각", "액션", "종목", "순위", "점수", "사유"]
            st.dataframe(view, width="stretch", hide_index=True, height=240)
        else:
            st.caption("신호 기록 없음.")


# --- 거시(수출 월별 추이/비교) 탭 ------------------------------------------
with tab_macro:
    st.subheader("🌐 수출 월별 추이 · 비교")
    piv = export_hist_pivot()
    if piv.empty:
        st.info("수출 월별 시계열이 없습니다.\n\n"
                "`python -m src.trade_history \"수출입 실적(품목별).csv\" --save` 로 적재하세요.")
    else:
        months = list(piv.index)              # 'YYYY-MM' 오름차순
        years = sorted({m[:4] for m in months}, reverse=True)

        def _prev_year(m: str) -> str:
            return f"{int(m[:4]) - 1}{m[4:]}"

        mode = st.radio("보기 모드", ["📅 단월 보기", "🔀 2개월 비교"], horizontal=True)

        if mode == "📅 단월 보기":
            cc = st.columns([1, 1, 3])
            y = cc[0].selectbox("년도", years)
            mons = [m for m in months if m.startswith(y)]
            m = cc[1].selectbox("월", [f"{int(x[5:7])}월" for x in mons],
                                index=len(mons) - 1)
            sel = f"{y}-{int(m[:-1]):02d}"
            row = piv.loc[sel].sort_values(ascending=False)
            st.metric(f"{sel} 선택 섹터 수출 합계", f"{row.sum():,.0f}억$")
            st.markdown("**섹터별 수출액 (억$)**")
            st.bar_chart(row, color=UP_RED)
            prev = _prev_year(sel)
            if prev in piv.index:
                yoy = ((piv.loc[sel] / piv.loc[prev] - 1) * 100).sort_values(ascending=False)
                st.markdown(f"**전년동월({prev}) 대비 증감률 (%)**")
                st.bar_chart(yoy, color=TOSS_BLUE)
            else:
                st.caption(f"전년동월({prev}) 데이터가 없어 YoY 생략.")

        else:  # 2개월 비교
            cc = st.columns(2)
            mA = cc[0].selectbox("월 A (기준)", months,
                                 index=max(0, len(months) - 13))
            mB = cc[1].selectbox("월 B (비교)", months, index=len(months) - 1)
            cmp = pd.DataFrame({mA: piv.loc[mA], mB: piv.loc[mB]})
            st.markdown("**섹터별 수출액 비교 (억$)**")
            st.bar_chart(cmp, color=[TOSS_BLUE, UP_RED])
            diff = ((piv.loc[mB] / piv.loc[mA] - 1) * 100)
            dd = diff.reset_index()
            dd.columns = ["섹터", "증감%"]
            dd["A(억$)"] = piv.loc[mA].values
            dd["B(억$)"] = piv.loc[mB].values
            dd = dd[["섹터", "A(억$)", "B(억$)", "증감%"]].sort_values("증감%", ascending=False)
            st.markdown(f"**{mA} → {mB} 섹터별 증감**")
            st.dataframe(dd.style.format({"A(억$)": "{:,.0f}", "B(억$)": "{:,.0f}",
                                          "증감%": "{:+.1f}"}),
                         width="stretch", hide_index=True)

        # 최신 MOTIE 요약(있으면) — 총수출/수입/수지
        tdf = trade_stats_df()
        if not tdf.empty:
            lm = sorted(tdf["month"].unique())[-1]
            def _v(metric, field):
                r = tdf[(tdf.metric == metric) & (tdf.field == field) & (tdf.month == lm)]
                return float(r["value"].iloc[0]) if len(r) else None
            mcount = tdf["month"].nunique()
            with st.expander(f"📄 MOTIE 월간 (총수출·수입·수지 + 메모리 고정가) · {mcount}개월"):
                c = st.columns(3)
                c[0].metric("총수출", f"{_v('export','usd_bil') or 0:,.1f}억$",
                            f"{_v('export','yoy') or 0:+.1f}%")
                c[1].metric("총수입", f"{_v('import','usd_bil') or 0:,.1f}억$",
                            f"{_v('import','yoy') or 0:+.1f}%")
                c[2].metric("무역수지", f"{_v('balance','usd_bil') or 0:+,.1f}억$")

                # 2개월 이상 쌓이면 추이 차트(MOTIE PDF 매달 적재 시 자동으로 길어짐)
                if mcount >= 2:
                    tot = tdf[(tdf.metric.isin(["export", "import", "balance"]))
                              & (tdf.field == "usd_bil")]
                    tp = tot.pivot_table(index="month", columns="metric", values="value").rename(
                        columns={"export": "총수출", "import": "총수입", "balance": "무역수지"})
                    st.markdown("**총수출·수입·수지 추이(억$)**")
                    st.line_chart(tp)
                    memt = tdf[(tdf.metric.str.startswith("mem_")) & (tdf.field == "price_usd")]
                    if not memt.empty:
                        mp = memt.pivot_table(index="month", columns="metric", values="value")
                        mp.columns = [c.replace("mem_", "") for c in mp.columns]
                        st.markdown("**메모리 고정가 추이($)**")
                        st.line_chart(mp)
                else:
                    st.caption("※ PDF가 2개월 이상 쌓이면 총수출/수입/수지·메모리가 추이 차트가 그려집니다.")

        st.caption("※ 섹터 수출은 관세청 HS 기준 합산(억$). 거시 참고용 — 매매 신호 아님(C 검증 탈락).")


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

    # --- 1군 분석 데이터(수급) ---
    st.divider()
    st.subheader("🔬 1군 분석 데이터 (수급)")
    ov = analytics_overview()
    albl = {"investor_flow": "투자자", "short_sale": "공매도", "credit_balance": "신용잔고",
            "program_trade": "프로그램", "loan_trans": "대차"}
    acols = st.columns(len(albl))
    for col, (k, lab) in zip(acols, albl.items()):
        n, sy = ov[k]
        col.metric(lab, f"{n:,}행", f"{sy}종목", delta_color="off")

    asyms = ov["symbols"]
    if asyms:
        names = dict(syms)
        amap = {f"{names.get(s, s)} ({s})": s for s in asyms}
        ch = st.selectbox("종목 선택 (수급)", list(amap.keys()), key="analytics_sym")
        d = analytics_symbol(amap[ch])
        if not d["investor"].empty:
            st.markdown("**투자자 순매수 (주) — 외국인·기관**")
            st.line_chart(d["investor"].rename(
                columns={"foreign_qty": "외국인", "institution_qty": "기관"}))
        cc = st.columns(2)
        if not d["short"].empty:
            cc[0].markdown("**공매도 거래량 비중(%)**")
            cc[0].line_chart(d["short"].rename(columns={"short_vol_ratio": "공매도비중%"}))
        if not d["credit"].empty:
            cc[1].markdown("**신용 융자잔고 비율(%)**")
            cc[1].line_chart(d["credit"].rename(columns={"loan_rmnd_rate": "융자잔고율%"}))
        if not d["program"].empty:
            st.markdown("**프로그램 순매수 수량**")
            st.line_chart(d["program"].rename(columns={"prog_net_qty": "프로그램순매수"}))
        st.caption("백필 진행 중이면 종목·기간이 계속 늘어납니다(5초 캐시 후 갱신).")
    else:
        st.caption("아직 1군 데이터가 없습니다. `python -m src.collect_analytics` 로 수집하세요.")
