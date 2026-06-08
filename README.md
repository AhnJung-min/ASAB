# ASAB — KIS 국내주식 자동매매 (모의투자)

한국투자증권(KIS) Open API 기반 **국내주식 자동매매** 프로젝트입니다.
**국내장 단일 트랙**에 집중하며(해외/미국 코드는 제거됨), 현재 **모의투자** 모드로 동작합니다.

이 프로젝트의 핵심은 화려한 전략이 아니라 **검증의 정직성**입니다.
"솔깃한 백테스트 수익"이 실전에서 녹는 함정(생존편향·거래비용·과적합·시장국면)을
구조적으로 걸러내는 데 집중합니다.

---

## 두 개의 트랙

| | **Track 1 · 일봉 로테이션** | **Track 2 · 장중 단타** |
|---|---|---|
| 전략 | 검증된 추세추종(blend top10 + 시장국면 필터) | 급등주 + 거래대금 유동주 단타 |
| 주기 | 하루 1회 리밸런스 | 30초 폴링 |
| 상태 | ✅ 10년 워크포워드 검증 완료 | 🧪 데이터 수집·검증 단계(엣지 미입증) |

> ⚠️ 두 봇은 **같은 모의계좌를 공유**하므로 **동시에 실행하지 마세요**.

---

## 빠른 시작

```powershell
# 1) 패키지 설치 + 설정
copy config.yaml.example config.yaml   # 열어서 app_key / app_secret / account_no 입력
pip install -r requirements.txt

# 2) 통합 메뉴 실행 (이거 하나만 더블클릭하면 됩니다)
ASAB.bat
```

`ASAB.bat` 하나가 **모든 작업의 진입점**입니다. 번호로 골라 실행하세요:

```
[장중 단타]      1.봇 시작⚠️  2.점검(주문없음)  3.1회테스트⚠️
[단타 데이터]    4.분봉 수집   5.학습           6.청산 분석
[일봉 로테이션]  7.봇 시작⚠️  8.사전점검
[데이터 관리]    9.일봉 업데이트  10.수집 이어하기  11.수급 백필
[도구]          12.대시보드   13.백업          14.설치
```
⚠️ = 실제(모의) 주문을 내는 항목(실행 전 확인 프롬프트). API 키 발급은
[apiportal.koreainvestment.com](https://apiportal.koreainvestment.com) 에서 **모의투자**로.

---

## Track 1 · 일봉 로테이션 (검증 완료)

매일 1회, 그 시점까지의 일봉으로 상위 N종목을 선정해 동일비중 리밸런싱합니다.

- **유니버스**: 종목 마스터에서 개별주(ST)만, 유동성(시총) 상위. ETF/ETN 제외.
- **점수**: screener(4팩터) / ml(LightGBM) / blend — 워크포워드 맞대결 결과 **blend top10 권장**.
- **시장국면 필터**: 지수가 200일선 아래면 현금 보유 → **낙폭 통제 1순위 수단**(MDD −70%→−42%).
- **검증틀**: `backtest.py`(슬리피지·ADV 반영), `walkforward.py`(Purge&Embargo 누수방지),
  `tune_exits.py`(청산 파라미터 스윕), `strategy_compare.py`(전략 OOS 맞대결).
- 핵심 교훈: 낙폭은 손절이 아니라 **국면필터**가 잡는다. 타이트 손절·트레일링은 휩쏘로 수익 악화.

```powershell
python -m src.run_bot --plan          # 타깃만 계산(API/주문 없음)
python -m src.run_bot --once --dry-run # 잔고 반영, 주문은 안 함
python -m src.backtest --top-n 10      # 백테스트
```

## Track 2 · 장중 단타 (실험·데이터 수집 단계)

급등주와 거래대금 상위(유동성) 종목을 30초마다 스캔해 단타하고, 모든 것을 학습 데이터로 쌓습니다.

**파이프라인:**
```
[수집]  스캔(등락률∪거래대금, ETF·레버리지 제외) → surge_scan
        상위 후보 호가 잔량 임밸런스          → surge_orderbook
        장 마감 후 당일 분봉(편향 제거)        → minute_bar
[매매]  필터(상승중+유동성+지수국면) → 지정가/시장가 매수 → 익절/손절/트레일링
[학습]  분봉 forward-return 라벨(비용+슬리피지 차감) → Ridge/LightGBM
[분석]  분봉으로 청산 규칙 시뮬레이션(어떤 익절/손절이 최선인가)
```

- **데이터 수집이 forward-only**(과거 백필 불가)인 호가·분봉은 **매일 장중 봇 + 마감 후 분봉수집**을
  빠짐없이 돌려야 쌓입니다.
- **지수 국면 필터**: 지수 급락 중이면 신규 매수 자동 중단(Track 1 방어를 이식).
- **모델 사다리**: 표본 < 3,000 → Ridge(선형, 과적합 위험 0) / ≥ 3,000 → 얕은 LightGBM(depth 3).
  **데이터 개수가 아니라 OOS 엣지 입증으로 단계 승급**.

```powershell
python -m src.surge_bot --dry-run   # 지금 무엇을 살지 확인(주문 없음)
python -m src.collect_minute        # 장 마감 후, 당일 분봉 수집
python -m src.surge_ml              # 학습 데이터 현황 / --train 으로 학습
python -m src.surge_exits           # 청산 규칙 분석
```

### 검증의 정직성 (이 프로젝트의 핵심)

- **생존편향 제거**: 스캔만 쓰면 "끝까지 잘나간 종목"만 라벨됨 → 분봉으로 이탈 종목의 실제 결과도 측정.
- **거래비용·슬리피지 차감**: forward-return 라벨에서 비용+슬리피지를 빼 **진짜 순수익**으로 학습.
- **표본 게이트 + OOS IC**: 데이터 부족하면 학습 거부. 시간순 홀드아웃으로 예측력 정직 검증.
- 실제 사례: 편향 제거 + 슬리피지 적용 시 **OOS IC가 +0.184 → −0.172로 뒤집힘** =
  솔깃한 양수는 함정이었고, 현재 엣지는 미입증. **여러 날(다국면) 데이터가 쌓여야 진짜 판정**.

---

## 대시보드

```powershell
python -m streamlit run dashboard.py   # 또는 ASAB.bat → 12
```
백테스트 / 스크리너 / 계좌(원화 잔고·포지션·자산추이) / 수집현황 탭. http://localhost:8501

---

## 구조

```
ASAB/
├─ ASAB.bat              # ★ 통합 실행 메뉴 (유일한 진입점)
├─ config.yaml.example   # 설정 템플릿
├─ dashboard.py          # Streamlit 대시보드
├─ test_connection.py    # 인증/시세/잔고 점검(주문 없음)
├─ data/market.db        # 수집 데이터(SQLite, 정션링크 → D:)
└─ src/
   ├─ menu.py            # ASAB.bat 이 띄우는 메뉴
   ├─ kis/               # API 계층
   │  ├─ auth.py         #   OAuth 토큰(만료 자동 재발급·재시도)
   │  ├─ client.py       #   REST 공통(스로틀 0.35s·재시도·토큰갱신)
   │  └─ domestic.py     #   시세/순위/호가/분봉/주문/잔고
   ├─ data/store.py      # SQLite 저장소(전 테이블·마이그레이션)
   │
   ├─ run_bot.py         # [T1] 일봉 로테이션 자동매매
   ├─ portfolio.py       #      타깃 선정(스크리너+국면필터)
   ├─ screener.py / ml_serve.py / features.py    # 점수·ML 서빙
   ├─ backtest.py / walkforward.py / tune_exits.py / strategy_compare.py  # 검증틀
   │
   ├─ surge_bot.py       # [T2] 장중 단타 봇
   ├─ surge_ml.py        #      단타 학습(Ridge/LightGBM, 비용·편향 반영)
   ├─ surge_exits.py     #      청산 규칙 분석
   ├─ collect_minute.py  #      EOD 분봉 수집
   │
   ├─ collect.py universe.py collect_analytics.py  # 데이터 수집
   └─ backup.py          # 구글드라이브 백업
```

주요 테이블: `daily_price`(10년 일봉) · `stock_master`(유니버스) · `surge_scan`/
`surge_orderbook`/`minute_bar`/`surge_trade`(단타 학습) · `live_*`(거래 저널) ·
`account_snapshot`(자산 추이).

---

## ⚠️ 주의사항

- **반드시 모의투자(`paper_trading: true`)로 충분히 검증한 뒤** 실전 전환을 고려하세요.
- 단타(Track 2)는 **아직 엣지가 입증되지 않은 실험 단계**입니다. 데이터를 쌓아 정직하게
  판정하는 중이며, OOS 엣지가 확인되기 전엔 실전 투입 대상이 아닙니다.
- 두 봇을 같은 계좌에서 **동시 실행 금지**(포지션 충돌).
- `config.yaml`·토큰 캐시·`data/*.db` 는 `.gitignore` 처리됨. **절대 외부 노출 금지.**
- 투자 판단과 그 결과의 책임은 전적으로 사용자 본인에게 있습니다.
