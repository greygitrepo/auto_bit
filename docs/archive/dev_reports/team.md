# Auto Bit 프로젝트 팀 구성

## 프로젝트 매니저 (PM)

**역할:** 전체 프로젝트의 진행 관리, 팀 간 조율, 품질 관리

**책임:**
- 각 팀의 작업 할당 및 우선순위 조정
- 팀 간 의존성 파악 및 작업 순서 조율
- 진행률 추적 및 보고
- 코드 리뷰 기준 및 문서화 기준 관리
- 통합 테스트 계획 수립
- 기술적 의사결정이 필요할 때 ADR 작성 주도

**산출물:**
- `docs/progress.md` — 전체 진행률 추적
- `docs/integration-plan.md` — 팀 간 통합 계획
- 각 Phase 완료 시 리뷰 리포트

---

## 팀 구성

### Team 1: Core Infrastructure (핵심 인프라)

**담당 영역:** 프로젝트 골격, 설정 관리, DB, 로깅, 멀티프로세스 기반

**업무 범위:**

| ID | 작업 | 의존성 | Phase |
|----|------|--------|-------|
| C-01 | 프로젝트 디렉토리 및 패키지 구조 생성 | 없음 | 1 |
| C-02 | Config 로더 구현 (YAML 로드, 검증, 기본값) | C-01 | 1 |
| C-03 | DB 스키마 생성 (candles, trades, positions, system_state, daily_performance) | C-01 | 1 |
| C-04 | DB 유틸리티 (WAL 모드, 연결 관리, CRUD 헬퍼) | C-03 | 1 |
| C-05 | 로거 설정 (loguru, 프로세스별 로그 분리) | C-01 | 1 |
| C-06 | IPC 메시지 정의 (dataclass: MarketData, Signal, PositionUpdate 등) | C-01 | 1 |
| C-07 | Rate Limiter 구현 | C-01 | 1 |
| C-08 | Orchestrator (Main Process) — 프로세스 생성, Queue 연결, Watchdog | C-06 | 3 |
| C-09 | Graceful Shutdown 핸들러 (SIGTERM/SIGINT) | C-08 | 3 |
| C-10 | 장애 복구 모듈 (recovery.py) — Bybit/DB 동기화, 타이머 복원 | C-08, D-03 | 3 |

**산출물 문서:**
- `docs/dev/core-infrastructure.md` — 모듈별 API 명세
- 각 작업 완료 시 해당 모듈의 docstring 및 사용 예시

---

### Team 2: Data & Indicators (데이터 수집 및 지표)

**담당 영역:** Bybit API 연동, 시장 데이터 수집, 기술적 지표 계산

**업무 범위:**

| ID | 작업 | 의존성 | Phase |
|----|------|--------|-------|
| D-01 | Bybit REST API 클라이언트 (pybit 래핑, 인증, 에러 핸들링) | C-02 | 1 |
| D-02 | 심볼 정보 조회 (instruments-info, 상장일 파싱) | D-01 | 1 |
| D-03 | WebSocket 매니저 (연결, 구독 관리, 재연결, 동적 구독 추가/제거) | D-01 | 1 |
| D-04 | 캔들 데이터 수집 및 DB 저장 (REST 히스토리 + WebSocket 실시간) | D-03, C-04 | 1 |
| D-05 | BTC/ETH 상시 수집 프로세스 로직 | D-04 | 1 |
| D-06 | Indicator Engine — EMA(5,10,20), RSI(14), VWAP, ATR(14) 계산 | D-04 | 1 |
| D-07 | Indicator Engine — 볼린저 밴드, Volume MA 계산 | D-06 | 1 |
| D-08 | Data Collector 프로세스 (P1) 통합 — WebSocket + Queue 출력 | D-05, C-06 | 3 |

**산출물 문서:**
- `docs/dev/data-indicators.md` — API 래핑 인터페이스, 지표 계산 공식, 데이터 포맷

---

### Team 3: Strategy (전략 파이프라인)

**담당 영역:** Scanner, Position, Asset 전략 구현

**업무 범위:**

| ID | 작업 | 의존성 | Phase |
|----|------|--------|-------|
| S-01 | Scanner 기본 클래스 (BaseScannerStrategy) | C-01 | 2 |
| S-02 | NewListingScanner — 1차 필터 (상장일, 거래대금) | S-01, D-02 | 2 |
| S-03 | NewListingScanner — 스코어링 (거래량, 변동성, 모멘텀, 신규상장, 시장환경) | S-02, D-06 | 2 |
| S-04 | NewListingScanner — 진입 필터 (기보유, 쿨다운, 유동성 이탈) | S-03 | 2 |
| S-05 | BTC/ETH 추세 판단 모듈 (EMA 정렬 기반 bull/bear/mixed) | D-06 | 2 |
| S-06 | Position 기본 클래스 (BasePositionStrategy) | C-01 | 2 |
| S-07 | MomentumScalper — 진입 로직 (EMA 정렬, RSI, VWAP, 거래량, 15분봉 필터) | S-06, D-06 | 2 |
| S-08 | MomentumScalper — 청산 로직 (EMA 역교차, RSI 꺾임, 거래량 급감) | S-07 | 2 |
| S-09 | SL/TP 계산 모듈 (ATR 기반 SL, R:R TP, min/max 보정) | D-06 | 2 |
| S-10 | 트레일링 스탑 로직 (1R 활성화, ATR×0.8 콜백) | S-09 | 2 |
| S-11 | 90분 시간 제한 관리 (타이머, 75분 경고, 강제 청산 신호) | S-06 | 2 |
| S-12 | Asset 기본 클래스 (BaseAssetStrategy) | C-01 | 2 |
| S-13 | FixedRatioStrategy — 포지션 사이징 (리스크 1%, 5% 캡, 동적 레버리지) | S-12 | 2 |
| S-14 | FixedRatioStrategy — 거부 조건 (슬롯, 중복, 일일 한도, 쿨다운) | S-13 | 2 |
| S-15 | FixedRatioStrategy — 드로다운 관리 (3단계 대응) | S-14 | 2 |
| S-16 | 연속 손절 카운터 및 쿨다운 로직 | S-14 | 2 |
| S-17 | Strategy Engine 프로세스 (P2) 통합 — Queue 입출력, 스캐너→포지션→자산 파이프라인 | S-04, S-08, S-15, C-06 | 3 |

**산출물 문서:**
- `docs/dev/strategy-scanner.md` — 스캐너 로직 상세, 스코어링 공식
- `docs/dev/strategy-position.md` — 진입/청산 조건 상세, SL/TP 계산
- `docs/dev/strategy-asset.md` — 사이징 공식, 거부 조건, 드로다운 로직

---

### Team 4: Execution (주문 실행)

**담당 영역:** 주문 실행, Paper/Live 엔진, 포지션 추적

**업무 범위:**

| ID | 작업 | 의존성 | Phase |
|----|------|--------|-------|
| E-01 | Order Manager 기본 구조 (주문 큐, 상태 머신) | C-06 | 3 |
| E-02 | Live Executor — Bybit API 주문 (시장가, Isolated 마진 설정, SL/TP 조건부 주문) | E-01, D-01 | 3 |
| E-03 | Paper Executor — 가상 체결 엔진 (시장가 시뮬, 슬리피지, 수수료, 펀딩비) | E-01 | 3 |
| E-04 | Paper Executor — 가상 SL/TP 모니터링 (캔들 기반 체결 판정) | E-03 | 3 |
| E-05 | Paper 가상 계좌 (잔고, 포지션, Isolated 마진 시뮬레이션) | E-03 | 3 |
| E-06 | Position Tracker — 포지션 P&L 계산, DB 업데이트 | E-01, C-04 | 3 |
| E-07 | Position Tracker — 성과 지표 (승률, 손익비, 청산사유별 통계) | E-06 | 3 |
| E-08 | Position Tracker — 일별 성과 기록 (daily_performance 테이블) | E-07 | 3 |
| E-09 | Order Manager 프로세스 (P3) 통합 — Asset 전략 실행, Queue 입출력 | E-02, E-03, S-15, C-06 | 3 |

**산출물 문서:**
- `docs/dev/execution.md` — 주문 흐름, Paper/Live 분기, 가상 체결 로직
- `docs/dev/position-tracking.md` — P&L 계산, 성과 지표 공식

---

### Team 5: GUI (웹 인터페이스)

**담당 영역:** FastAPI 웹 서버, 대시보드, 차트, 거래 제어

**업무 범위:**

| ID | 작업 | 의존성 | Phase |
|----|------|--------|-------|
| G-01 | FastAPI 앱 구조, Jinja2 템플릿 기반 | C-01 | 4 |
| G-02 | REST API — 포지션 조회, 거래이력 조회, 성과 통계 조회 | G-01, C-04 | 4 |
| G-03 | REST API — 거래 시작/종료/일시정지 제어 | G-02, C-08 | 4 |
| G-04 | WebSocket — 실시간 포지션 P&L, 이벤트 푸시 | G-01 | 4 |
| G-05 | 대시보드 탭 — 자산 현황, 전체 수익률 차트, 오늘 통계 | G-02, G-04 | 4 |
| G-06 | 포지션 탭 — 현재 포지션 상세, 종목별 수익률 차트 | G-02, G-04 | 4 |
| G-07 | 거래이력 탭 — 거래 테이블, 필터, 성과 요약 | G-02 | 4 |
| G-08 | 설정 탭 — 거래 제어 버튼, 현재 설정 표시, 프로세스 상태 | G-03, G-04 | 4 |
| G-09 | GUI 프로세스 (P5) 통합 — Orchestrator와 제어 Queue 연결 | G-03, C-08 | 4 |

**산출물 문서:**
- `docs/dev/gui-api.md` — REST 엔드포인트 명세, WebSocket 메시지 포맷
- `docs/dev/gui-screens.md` — 화면별 데이터 바인딩, 차트 설정

---

## 팀 간 의존성 맵

```
Phase 1                Phase 2              Phase 3              Phase 4
─────────             ─────────            ─────────            ─────────

Team 1 (Core)  ────→  Team 3 (Strategy) ──→ Team 1 (Orchestrator)
  C-01~C-07             S-01~S-16             C-08~C-10
                                                  │
Team 2 (Data)  ────→  Team 3 (Strategy) ──→ Team 4 (Execution) ──→ Team 5 (GUI)
  D-01~D-07             (지표 사용)            E-01~E-09              G-01~G-09
                                                  │
                                              Team 2 (P1 통합)
                                                D-08
```

### 병렬 작업 가능 구간

**Phase 1:** Team 1과 Team 2는 완전 병렬 (C-01 완료 후)
**Phase 2:** Team 3은 Team 1, 2 결과물에 의존하지만, 기본 클래스(S-01, S-06, S-12)는 인터페이스만 정의하므로 선행 가능
**Phase 3:** Team 1(Orchestrator), Team 2(P1 통합), Team 4(Execution)는 부분 병렬
**Phase 4:** Team 5는 Phase 3 거래 엔진 완성 후 시작, 단 G-01~G-02는 DB만 있으면 선행 가능

---

## 업무 진행 규칙

### 1. 문서화 의무

모든 작업은 다음 문서를 산출한다:

```
작업 시작 전:
  - 작업 항목의 목적, 입출력, 의존성 확인

작업 완료 시:
  - 코드 내 docstring (클래스, 함수)
  - 팀별 개발 문서 (docs/dev/) 업데이트
  - 변경된 인터페이스가 있으면 관련 팀에 공유

통합 시:
  - 통합 테스트 결과 기록
  - docs/progress.md 업데이트
```

### 2. 작업 상태 정의

| 상태 | 의미 |
|------|------|
| `대기` | 의존성 미충족, 아직 시작 불가 |
| `준비` | 의존성 충족, 착수 가능 |
| `진행중` | 개발 중 |
| `리뷰` | 코드 완성, PM 리뷰 대기 |
| `완료` | 리뷰 통과, 통합 가능 |
| `블로커` | 문제 발생, 해결 필요 |

### 3. 커밋 컨벤션

```
[팀ID-작업ID] 작업 내용 요약

예:
[C-01] 프로젝트 디렉토리 및 패키지 구조 생성
[D-03] WebSocket 매니저 구현 (연결, 구독, 재연결)
[S-07] MomentumScalper 진입 로직 구현
```

### 4. Phase 완료 기준

| Phase | 완료 기준 |
|-------|----------|
| 1 | Config 로드 → Bybit 연결 → 캔들 수집 → 지표 계산 → DB 저장 E2E 동작 |
| 2 | 30개 후보 스캔 → 종목 선정 → 진입/청산 신호 → 자산관리 승인/거부 단위 테스트 통과 |
| 3 | Paper 모드 E2E: 스캐너 → 진입 → 보유(90분 제한) → 청산 → 성과 기록 |
| 4 | GUI에서 거래 시작 → 대시보드에서 실시간 포지션/수익률 확인 → 거래 종료 |
| 5 | Paper 2주+ 운영, Live 소액 전환 |
