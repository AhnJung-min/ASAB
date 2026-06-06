# KIS 자동매매 봇 (모의투자)

한국투자증권(KIS) Open API 를 이용한 **국내주식 자동매매** 프로그램입니다.
**국내장 단일 트랙**에 집중합니다(해외/미국 관련 코드는 제거됨).
현재 **모의투자** 모드로 동작합니다.

## 구조

```
ASAB/
├─ config.yaml.example   # 설정 템플릿 (복사해서 config.yaml 작성)
├─ requirements.txt
├─ test_connection.py    # 인증/시세/잔고 점검 (주문 안 함)
├─ data/market.db        # 수집 데이터 (SQLite, 자동 생성)
└─ src/
   ├─ run_bot.py         # 자동매매 진입점
   ├─ collect.py         # 데이터 수집기 (과거 일봉/투자자/재무 백필)
   ├─ screener.py        # 종목 스크리너 (팩터 기반 자동 선정)
   ├─ kis/
   │  ├─ config.py       # 설정 로딩, 도메인 분기(모의/실전)
   │  ├─ auth.py         # OAuth 토큰 발급·캐싱
   │  ├─ client.py       # REST 공통 클라이언트(스로틀·재시도·hashkey)
   │  ├─ domestic.py     # 국내 시세/주문/잔고
   │  └─ marketdata.py   # 일봉/순위/투자자/재무 조회
   ├─ data/
   │  └─ store.py        # SQLite 저장소
   └─ strategy/
      ├─ base.py         # 전략 인터페이스
      └─ sma_cross.py    # 이동평균 교차 전략
```

## 데이터 수집 & 종목 선정 파이프라인

```powershell
# 0) 종목 유니버스 구축: KIS 종목 마스터에서 ETF/ETN 제외 개별주 전체(~2,700개)
python -m src.universe

# 1) 다국면 데이터 수집: 개별주 10년 일봉 백필 (강세장 단일국면 탈출)
python -m src.collect --source master --months 120 --skip-existing
python -m src.collect --source master --limit 300 --months 120   # 일부만
python -m src.collect --stats                                    # 저장 현황

# 2) 스크리너: 저장된 데이터로 매매 후보 종목 자동 선정
python -m src.screener --top 10
```

- **유니버스**: `src/universe.py` 가 KOSPI/KOSDAQ 마스터를 받아 증권그룹구분코드
  `ST`(주권)만 추려 `stock_master` 테이블에 저장 (ETF/ETN/리츠 제외).
- **다국면 데이터**: `--months 120` 이면 약 10년(2016~) 일봉을 백필하여
  2018 조정·2020 코로나·2022 약세장을 포함 → 강세장 과적합 방지.
- 종목당 ~30초 소요. `--skip-existing` 으로 중단 후 재개 가능.
- 수집 데이터는 `data/market.db`(SQLite)에 누적 저장됩니다(중복 안전).
- 스크리너 팩터: 모멘텀(40%) + 추세(20%) + 유동성(20%) + 저변동성(20%).

## 백테스트

```powershell
python -m src.backtest --top-n 3 --hold-days 20 --cost-bps 25
```
- 횡단면 팩터 로테이션: 매 리밸런스마다 그 시점까지의 데이터로만 상위 N종목 선정(미래정보 누출 방지), 동일비중 보유 후 교체.
- 지표: 누적수익률, CAGR, 샤프지수, 최대낙폭(MDD), 연변동성. 벤치마크는 KODEX 200.

## 실시간 대시보드 🖥️

```powershell
python -m streamlit run dashboard.py
```
브라우저(기본 http://localhost:8501)에서 열립니다. 4개 탭:
- **📊 백테스트**: 수익곡선·낙폭·지표·보유이력 (사이드바에서 파라미터 조절)
- **🔍 스크리너**: 팩터 순위 표/차트
- **💰 계좌**: 모의계좌 잔고·포지션 + 상위종목 실시간 현재가 (자동 새로고침 가능)
- **🗂 수집 데이터**: 저장 현황·유니버스·개별 종목 차트

> ⚠️ 최근 국내 데이터는 강세장 단일 국면이라 백테스트가 과적합될 수 있습니다.
> 다국면(10년) 데이터 + 워크포워드 검증으로 보완 예정.

## 프로젝트 방향 (국내장 단일 트랙)

목표는 **검증된 전략으로 실제 수익**. 국내장 단일 트랙에 집중(해외 코드 제거).
검증 중심 로드맵: 다국면 데이터 → 시장국면 필터 → 상대강도 → 워크포워드 → 모의 포워드 → 실전.

## 시작하기

### 1. API 키 발급
1. https://apiportal.koreainvestment.com 접속 → 로그인
2. **모의투자** 신청 (KIS HTS/앱에서 모의투자 계좌 개설 필요)
3. "Open API 신청" → 앱 등록 → **APP KEY / APP SECRET** 발급

### 2. 설정
```powershell
copy config.yaml.example config.yaml
# config.yaml 을 열어 app_key / app_secret / account_no 입력
```

### 3. 패키지 설치
```powershell
pip install -r requirements.txt
```

### 4. 연결 점검 (주문 없음 — 먼저 이걸로 확인)
```powershell
python test_connection.py
```

### 5. 자동매매 실행
```powershell
python -m src.run_bot --once   # 1회만 점검
python -m src.run_bot          # 주기적 자동매매 (Ctrl+C 로 중지)
```

## 동작 방식
- `config.yaml` 의 `domestic_symbols` 를 주기적으로 조회
- `sma_cross` 전략: 단기(5)·장기(20) 이동평균 골든크로스 시 매수, 데드크로스 시 매도
- 종목당 `max_position_per_symbol` 수량까지만 보유

## ⚠️ 주의사항
- **반드시 모의투자(`paper_trading: true`)로 충분히 검증한 뒤** 실전 전환을 고려하세요.
- `config.yaml` 과 토큰 캐시는 `.gitignore` 에 포함되어 있습니다. **절대 외부에 노출하지 마세요.**
- 이동평균 전략은 워밍업에 `long_window` 개의 데이터(기본 20틱)가 필요합니다.
  실전에서는 일/분봉 과거 데이터로 초기화하는 것을 권장합니다(향후 개선 항목).
- 투자 판단과 그 결과의 책임은 전적으로 사용자 본인에게 있습니다.
```
