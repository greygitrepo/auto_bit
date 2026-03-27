# Auto Bit - Bybit 자동거래 시스템

## 프로젝트 개요

Bybit 거래소 API를 활용한 암호화폐 자동거래 프로그램.
시장 데이터를 분석하고 3단계 전략 파이프라인(종목 선정 → 진입/청산 → 자산관리)에 따라 자동으로 매매를 실행한다.

**실행 모드:**
- `paper` - 메인넷 실시간 데이터 + 가상 주문 (페이퍼 트레이딩)
- `live` - 메인넷 실시간 데이터 + 실제 주문 (실전 거래)

> 테스트넷이 아닌 **메인넷 데이터 기반 페이퍼 트레이딩**으로 실전과 동일한 환경에서 전략을 검증한다.

---

## 시스템 아키텍처

```
[Bybit Mainnet API]
        │
        ▼
┌─────────────────┐
│  Data Collector  │ ── 실시간 시세/오더북/체결 데이터
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Indicator Engine │ ── 기술적 지표 계산
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────────────────────┐
│              Strategy Pipeline                   │
│                                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌────────┐ │
│  │ 1. Scanner   │→ │ 2. Position  │→ │ 3. Asset│ │
│  │  종목 선정   │  │  진입/청산   │  │  관리   │ │
│  └──────────────┘  └──────────────┘  └────────┘ │
└────────┬────────────────────────────────────────┘
         │
         ▼
┌─────────────────┐     ┌──────────────────┐
│  Order Manager   │────▶│  Execution Layer  │
│  (주문 관리)     │     │  paper │ live     │
└────────┬────────┘     └──────────────────┘
         │
         ▼
┌─────────────────┐     ┌──────────────────┐
│ Position Tracker │────▶│   Notification    │
│  (포지션 추적)   │     │   (텔레그램)      │
└─────────────────┘     └──────────────────┘
```

---

## Agent 정의

### 1. Data Collector Agent

**역할:** Bybit 메인넷에서 시장 데이터를 수집하고 저장한다.

**책임:**
- **상시 수집 (WebSocket):** BTC/USDT, ETH/USDT 캔들 데이터 (추세 판단용)
- **동적 수집 (WebSocket):** 선정된 포지션 종목 (최대 3개) 실시간 캔들/체결
- **온디맨드 수집 (REST):** 스캐너 실행 시 전체 심볼 티커, 후보 30개 캔들 데이터
- 심볼 상장 정보 조회 (`instruments-info` API)
- 수집된 데이터를 DB에 저장
- 연결 끊김 시 자동 재연결

**WebSocket 구독:** BTC + ETH + 포지션 종목 (최대 5개)
**입력:** 심볼 목록, 타임프레임 설정 (`config/symbols.yaml`)
**출력:** 정규화된 시장 데이터

---

### 2. Indicator Engine Agent

**역할:** 수집된 데이터를 기반으로 기술적 지표를 계산한다.

**책임:**
- 추세 지표: SMA, EMA, WMA, DEMA
- 모멘텀 지표: RSI, MACD, Stochastic, CCI
- 변동성 지표: Bollinger Bands, ATR, Keltner Channel
- 거래량 지표: OBV, VWAP, Volume Profile
- 사용자 정의 지표 확장 인터페이스

**입력:** OHLCV 데이터
**출력:** 지표 계산 결과 DataFrame

---

### 3. Strategy Pipeline (3단계 전략)

#### 3-1. Scanner Strategy (종목 선정 전략)

**역할:** 신규상장 종목 중 거래량과 변동성이 큰 종목을 선정한다.

**실행 조건:** 포지션 빈 슬롯이 있을 때만 실행 (온디맨드)

**책임:**
- 신규상장 종목 필터링 (상장 N일 이내)
- 거래량 + 변동성 기준 상위 30개 후보 풀 구성
- BTC/ETH 추세를 반영한 스코어링 (거래량 30%, 변동성 25%, 모멘텀 20%, 신규상장 10%, 시장환경 15%)
- 진입 불가 종목 필터 (기보유, 쿨다운, 유동성 이탈)
- 빈 슬롯 수만큼 상위 종목 선정

**설정:** `config/strategy/scanner.yaml`
**입력:** 전체 심볼 티커 + BTC/ETH 추세 데이터
**출력:** `ScanResult` (심볼, 스코어, 시장방향, 추천방향, 상세점수)

#### 3-2. Position Strategy (포지션 진입/청산 전략)

**역할:** 선정된 종목에 대해 단타(최대 90분) 진입/청산을 결정한다.

**진입 전략 (MomentumScalper):**
- 5분봉 기준 단기 EMA(5/10/20) 추세 정렬 확인
- RSI(14) 모멘텀 방향 확인 (50 기준)
- 거래량 급증 확인 (직전 5캔들 평균 × 1.5)
- VWAP 대비 가격 위치 확인
- 15분봉 상위 TF 방향 일치 필터
- 스캐너 `suggested_side` 방향 강제 (NEUTRAL 시 양방향)

**청산 전략 (우선순위순):**
1. SL/TP 서버사이드 자동 체결 (ATR×1.5 손절, R:R 2.0 익절)
2. 시간 초과 강제 청산 (90분)
3. 트레일링 스탑 (1R 수익 도달 시 활성화)
4. 전략 청산 신호 (EMA 역교차, RSI 꺾임, 거래량 급감)
5. 스캐너 탈락 시 조건부 처리 (ADR-008)

**설정:** `config/strategy/position.yaml`
**입력:** 스캐너 ScanResult + 5분봉 지표 데이터 + 현재 포지션
**출력:** `{ signal: LONG|SHORT|CLOSE|HOLD, entry, sl, tp, reason }`

#### 3-3. Asset Strategy (자산관리 전략)

**역할:** 포지션 크기, 레버리지, 전체 리스크를 관리한다. 거래의 최종 승인/거부 게이트.

**포지션 사이징:**
- 리스크 기반 역산 (거래당 리스크 = 초기 자본의 1%)
- 종목당 투입 상한: 초기 자본의 5%
- 단리 운영 (초기 자본 기준 고정)
- 레버리지: SL 거리에 따라 동적 결정 (1x~5x)

**일일 리스크 관리:**
- 일일 최대 손실 3%, 최대 거래 15회
- 연속 2회 손절 → 30분 쿨다운
- 연속 3회 손절 → 당일 거래 중단

**드로다운 관리:**
- 5% → 경고 / 10% → 포지션 50% 축소 / 15% → 거래 중단 (수동 재개)

**거부 조건:**
- 동시 포지션 3개 초과
- 동일 심볼 기보유
- 일일 손실/거래 한도 초과
- 드로다운 3단계 (거래 중단 상태)
- 연속 손절 쿨다운 중

**설정:** `config/strategy/asset.yaml`
**입력:** 포지션 신호, 초기 자본, 현재 잔고, 현재 포지션, 일일 통계
**출력:** `{ approved: bool, size, leverage, risk_amount, reject_reason }`

---

### 4. Order Manager Agent

**역할:** 주문을 실행하고 관리한다. 실행 모드(paper/live)에 따라 분기.

**책임:**
- 시장가/지정가 주문 실행
- 조건부 주문 (SL/TP) 설정
- 주문 상태 추적 및 관리
- 미체결 주문 타임아웃 취소
- 슬리피지 기록
- **Paper 모드:** 실시간 가격 기반 가상 체결 시뮬레이션
- **Live 모드:** Bybit API를 통한 실제 주문 실행

**입력:** 자산관리 전략을 통과한 주문 정보
**출력:** 체결 결과

---

### 5. Position Tracker Agent

**역할:** 포지션과 수익/손실을 실시간으로 추적한다.

**책임:**
- 열린 포지션 실시간 P&L 모니터링
- 실현/미실현 손익 계산
- 거래 이력 저장 (paper/live 구분)
- 성과 지표 계산 (승률, 손익비, 샤프비율 등)
- 일별/주별/월별 리포트 생성

**입력:** 체결 데이터, 실시간 가격
**출력:** 포지션 현황, 성과 리포트

---

### 6. GUI Server Agent

**역할:** 웹 기반 GUI를 제공하여 거래 제어 및 모니터링.

**책임:**
- 거래 시작/종료/일시정지 제어
- 현재 포지션 및 미실현 P&L 실시간 표시
- 자산 현황 (잔고, 투입 자본, 드로다운)
- 전체/종목별 수익률 차트
- 거래 이력 및 성과 통계
- 시스템 프로세스 상태 모니터링

**기술:** FastAPI + Jinja2 SSR + WebSocket 실시간 푸시
**접근:** `http://localhost:8080`
**특성:** 거래 엔진은 GUI와 독립적으로 백그라운드 실행. 브라우저를 닫아도 거래 계속.

---

## 실행 흐름

```
1. [시작] config 로드 → 장애 복구 확인 → 실행 모드(paper/live) 결정
2. [장애 복구] Bybit/DB 포지션 동기화, 상태 복원 (ADR-019)
3. [GUI Server] 웹 서버 시작 (http://localhost:8080)
4. [GUI] "거래 시작" → 거래 엔진 프로세스 시작 (백그라운드)
5. [Data Collector] BTC/ETH WebSocket 상시 수집 시작
6. [빈 슬롯 확인] 현재 포지션 < 3 → 스캐너 트리거
7. [Scanner Strategy] 신규상장 종목 30개 후보 → 스코어링 → 빈 슬롯 수만큼 선정
8. [Data Collector] 선정 종목 WebSocket 구독 추가
9. [Indicator Engine] 선정 종목 지표 계산
10. [Position Strategy] 진입/청산 신호 생성
11. [Asset Strategy] 포지션 크기/리스크 검증 (종목당 5%, 리스크 1%)
12. [Order Manager] Isolated 마진 설정 → 시장가 진입 → 서버사이드 SL/TP
13. [Position Tracker] 포지션 및 P&L 업데이트 → GUI 실시간 반영
14. 포지션 보유 중 → 10번 반복 (5분봉마다, 최대 90분)
15. 포지션 청산 시 → 6번으로 (빈 슬롯 발생 → 스캐너 재실행)
16. [GUI] "거래 종료" → 신규 진입 중단, 포지션 자연 청산 후 엔진 종료
```

---

## 실행 모드 상세

### Paper Trading (페이퍼 트레이딩)

- **데이터:** 메인넷 실시간 데이터 사용 (테스트넷 X)
- **주문:** 가상 주문 엔진이 시뮬레이션
  - 시장가 → 현재 호가 기준 즉시 체결 가정
  - 지정가 → 가격 도달 시 체결
  - 슬리피지 시뮬레이션 옵션
- **잔고:** 가상 잔고 (초기 자본 설정)
- **기록:** `paper_trades` 테이블에 별도 저장
- **목적:** 전략 검증, 파라미터 튜닝

### Live Trading (실전 거래)

- **데이터:** 메인넷 실시간 데이터
- **주문:** Bybit API를 통한 실제 주문
- **잔고:** 실제 계좌 잔고
- **기록:** `live_trades` 테이블에 저장
- **안전장치:** 반드시 paper 모드에서 충분한 검증 후 전환

---

## Config 구조

```
config/
├── app.yaml              # 앱 전역 설정 (실행모드, 로깅 레벨, GUI 포트)
├── credentials.yaml      # API 키 (.gitignore)
├── symbols.yaml          # 거래 심볼 및 타임프레임
└── strategy/
    ├── scanner.yaml      # 종목 선정 전략 파라미터
    ├── position.yaml     # 진입/청산 전략 파라미터
    └── asset.yaml        # 자산관리 전략 파라미터
```

---

## 프로젝트 구조

```
auto_bit/
├── agents.md
├── docs/
│   ├── architecture.md
│   ├── strategy-guide.md
│   ├── config-guide.md
│   ├── paper-trading.md
│   └── decisions/
│       ├── _template.md
│       └── 001~022 ADR 문서들
├── config/
│   ├── app.yaml
│   ├── credentials.yaml.example
│   ├── symbols.yaml
│   └── strategy/
│       ├── scanner.yaml
│       ├── position.yaml
│       └── asset.yaml
├── src/
│   ├── __init__.py
│   ├── main.py              # 진입점 (Orchestrator)
│   ├── recovery.py          # 장애 복구 로직
│   ├── collector/
│   │   ├── __init__.py
│   │   ├── data_collector.py
│   │   └── rate_limiter.py
│   ├── indicators/
│   │   ├── __init__.py
│   │   └── technical.py
│   ├── strategy/
│   │   ├── __init__.py
│   │   ├── scanner/
│   │   │   ├── __init__.py
│   │   │   ├── base.py
│   │   │   └── new_listing.py
│   │   ├── position/
│   │   │   ├── __init__.py
│   │   │   ├── base.py
│   │   │   └── momentum_scalper.py
│   │   └── asset/
│   │       ├── __init__.py
│   │       ├── base.py
│   │       └── fixed_ratio.py
│   ├── order/
│   │   ├── __init__.py
│   │   ├── order_manager.py
│   │   ├── paper_executor.py
│   │   └── live_executor.py
│   ├── tracker/
│   │   ├── __init__.py
│   │   └── position_tracker.py
│   ├── gui/
│   │   ├── __init__.py
│   │   ├── app.py            # FastAPI 앱
│   │   ├── api.py            # REST API 엔드포인트
│   │   ├── websocket.py      # 실시간 WebSocket
│   │   ├── templates/        # Jinja2 HTML
│   │   │   ├── base.html
│   │   │   ├── dashboard.html
│   │   │   ├── positions.html
│   │   │   ├── history.html
│   │   │   └── settings.html
│   │   └── static/           # CSS, JS
│   │       ├── css/
│   │       ├── js/
│   │       └── lib/          # lightweight-charts, chart.js
│   └── utils/
│       ├── __init__.py
│       ├── config.py
│       ├── logger.py
│       └── db.py
├── tests/
├── requirements.txt
├── .env.example
└── .gitignore
```

---

## 기술 스택

| 구분 | 기술 |
|------|------|
| 언어 | Python 3.11+ |
| 거래소 API | pybit (Bybit V5 API) |
| 기술 분석 | pandas-ta |
| 데이터 처리 | pandas, numpy |
| 데이터 저장 | SQLite (WAL 모드) |
| GUI | FastAPI, uvicorn, jinja2 |
| 차트 | Lightweight Charts (TV), Chart.js |
| 설정 관리 | PyYAML, python-dotenv |
| 로깅 | loguru |
| 테스트 | pytest |
| 동시성 | multiprocessing + asyncio (프로세스 내부) |
| 거래 유형 | USDT 무기한 선물 (Linear) |
| 주문 방식 | 시장가 + 서버사이드 SL/TP |
| 마진 모드 | Isolated (격리 마진) |

---

## 개발 단계

### Phase 1 - 기반 구축
- [ ] 프로젝트 구조 및 config 체계 생성
- [ ] Bybit 메인넷 API 연결 및 데이터 수집 (BTC/ETH 상시, 동적 구독)
- [ ] 기본 지표 계산 (EMA, RSI, VWAP, ATR)
- [ ] DB 스키마 설계 (candles, trades, positions, system_state)
- [ ] Rate Limiter 구현

### Phase 2 - 전략 파이프라인
- [ ] Scanner 전략 (NewListingScanner)
- [ ] Position 전략 (MomentumScalper, 5분봉, 90분 제한)
- [ ] Asset 전략 (FixedRatio, 리스크 1%, 5% 캡, 드로다운 3단계)

### Phase 3 - 실행 엔진
- [ ] 멀티프로세스 Orchestrator (P1~P3 관리, Watchdog)
- [ ] Paper 트레이딩 엔진 (가상 체결, Isolated 마진 시뮬)
- [ ] 장애 복구 (Bybit/DB 동기화, 타이머 복원)
- [ ] 포지션 트래커 (90분 타이머, 트레일링 스탑)

### Phase 4 - GUI
- [ ] FastAPI + Jinja2 GUI 서버 (P5)
- [ ] 대시보드 (자산 현황, 수익률 차트)
- [ ] 포지션 탭 (실시간 P&L, 종목별 수익률 차트)
- [ ] 거래 이력 탭 (성과 통계)
- [ ] 설정 탭 (거래 시작/종료 제어, 프로세스 상태)

### Phase 5 - 검증 및 실전
- [ ] Paper 트레이딩으로 전략 검증
- [ ] 파라미터 튜닝
- [ ] Live 전환 (소액)
