# auto_bit

Bybit USDT 무기한 선물 자동매매 시스템. 신규 상장 종목을 스캔하고, 모멘텀 스캘핑 전략으로 진입/청산하며, 리스크 관리까지 자동으로 수행합니다.

## 주요 기능

- **종목 스캔** — 거래량, 변동성, 모멘텀 등 5가지 지표로 후보 선정 (최대 50종목 모니터링)
- **자동 매매** — EMA 정렬 + RSI + 거래량 확인 기반 진입, ATR 기반 SL/TP 자동 설정
- **리스크 관리** — 포지션 사이징, 드로다운 관리, 연속 손절 쿨다운
- **전략 자동 튜닝** — 시그널 비율 모니터링 후 파라미터 자동 조절 (6단계)
- **웹 대시보드** — 실시간 포지션, P&L, 거래 이력, 튜너 상태 확인
- **페이퍼/라이브 전환** — 동일 코드로 모의거래와 실거래 지원

## 시스템 요구사항

- Python 3.10+
- Bybit 메인넷 API 키 (페이퍼 모드: 읽기 전용, 라이브 모드: 거래 권한)

## 설치

```bash
git clone <repo-url> auto_bit
cd auto_bit
pip install -r requirements.txt
```

## 설정

### 1. API 키 설정

```bash
cp config/credentials.yaml.example config/credentials.yaml
# credentials.yaml 편집:
```

```yaml
bybit:
  api_key: "YOUR_API_KEY"
  api_secret: "YOUR_API_SECRET"
```

### 2. 주요 설정 파일

| 파일 | 설명 |
|------|------|
| `config/app.yaml` | 실행 모드(paper/live), 로깅, DB, GUI 포트 |
| `config/symbols.yaml` | 기준 심볼(BTC/ETH), 타임프레임, 블랙리스트 |
| `config/strategy/scanner.yaml` | 종목 선정 기준 (상장일, 거래대금, 스코어링) |
| `config/strategy/position.yaml` | 진입/청산 전략 (EMA, RSI, 거래량, SL/TP) |
| `config/strategy/asset.yaml` | 자산 관리 (포지션 크기, 레버리지, 드로다운) |

### 3. 모드 변경

`config/app.yaml`에서 직접 변경하거나 시작 시 인자로 지정:

```yaml
mode: paper   # paper | live
```

## 실행

### 시작

```bash
# 페이퍼 트레이딩 (기본)
./start.sh paper

# 라이브 트레이딩 (확인 프롬프트 표시)
./start.sh live

# GUI 없이 실행
./start.sh paper --headless
```

### 상태 확인

```bash
./status.sh
```

출력 예시:

```
============================================
  auto_bit System Status
============================================
  Main Process: RUNNING (PID=12345, uptime=01:30)

  GUI: http://localhost:8080 (OK)

  Mode: PAPER
  Trading: ACTIVE
  Balance: 19.47 USDT
  P&L: -0.5307 USDT (-2.65%)
  Today: 2 trades, P&L=-0.5307
  Positions: 3 open
    VIRTUALUSDT LONG pnl=0.0042 (0.1%)
    ASTERUSDT SHORT pnl=-0.0057 (-0.1%)
    ALCHUSDT LONG pnl=0.0240 (0.8%)
  Tuner: L0 rate=26.7% streak=5
============================================
```

### 정지

```bash
# 정상 종료 (열린 포지션 정리 후 종료)
./stop.sh

# 강제 종료
./stop.sh --force
```

### Python 직접 실행

```bash
python3 -m src.main                    # 기본 (config 설정대로)
python3 -m src.main --mode paper       # 모드 지정
python3 -m src.main --headless         # GUI 없이
```

## 웹 대시보드

시작 후 `http://localhost:8080` 접속 (포트는 `config/app.yaml`에서 변경 가능)

### 페이지 구성

| 탭 | 기능 |
|----|------|
| **Dashboard** | 잔고, P&L, 에쿼티 차트, 오픈 포지션, 오늘의 통계 |
| **Positions** | 오픈 포지션 상세 (진입가, 현재가, SL/TP, 미실현 P&L, 남은 시간) |
| **History** | 청산된 거래 이력 (필터, 페이지네이션) |
| **Settings** | 거래 제어(Start/Stop/Pause), 프로세스 상태, 튜너, 설정 확인 |

### Settings 주요 기능

- **Start / Stop / Pause** — 거래 시작, 정지, 일시 중지
- **Reset Paper Trading** — 모든 거래 기록 초기화, 잔고 리셋 후 재시작
- **Strategy Tuner** — 현재 튜닝 레벨, 시그널 비율, 파라미터 확인
  - *Apply to YAML* — 안정적인 파라미터를 config에 저장
  - *Reset Tuner* — 튜닝 레벨을 0으로 초기화

## 시스템 아키텍처

```
Main Process (Orchestrator)
├── P1: DataCollector      — WebSocket 실시간 캔들 수집 + REST 히스토리
├── P2: StrategyEngine     — 지표 계산 → 스캐너 → 포지션 전략 → 시그널
├── P3: OrderManager       — 주문 실행, SL/TP 모니터링, P&L 추적
└── P5: GUIServer          — FastAPI 대시보드 (WebSocket 실시간 업데이트)
```

### 전략 파이프라인

```
1. Scanner (종목 선정)
   └─ NewListingScanner: 거래량 + 변동성 + 모멘텀 + 시장환경 스코어링
   └─ 상위 종목 모니터링 리스트에 추가

2. Position (진입/청산)
   └─ MomentumScalper: EMA 정렬 + RSI + 거래량 급등 + VWAP
   └─ 청산: SL/TP, 트레일링 스톱, 시간 제한, 전략 시그널

3. Asset (자산 관리)
   └─ FixedRatio: 포지션 크기, 레버리지, 동시 포지션 수 관리
   └─ 드로다운 관리: 경고 → 축소 → 중단
```

### 전략 자동 튜너

시그널 비율(5분마다 평가)을 모니터링하여 파라미터를 자동 조절:

| 레벨 | 변경 내용 | 시그널 비율 조건 |
|------|-----------|-----------------|
| L0 | 원래 설정 | 5~30% (적정) |
| L1 | RSI 확대, 거래량 완화 | < 5% |
| L2 | VWAP 필터 비활성화 | < 5% |
| L3 | 15분 필터 비활성화 | < 5% |
| L4 | RSI 대폭 확대 | < 5% |
| L5 | EMA 2개 정렬로 완화 | < 5% |
| L6 | 최대 완화 | < 5% |

- 6회 연속 안정 유지 시 YAML 저장 제안 (Settings에서 사용자가 직접 적용)
- 시그널 비율이 30% 초과 시 자동으로 레벨 하향 (강화)
- 재시작 시 이전 튜닝 상태 DB에서 자동 복원

## 청산 전략 (우선순위)

1. **SL/TP** — ATR 기반 손절/익절 (5초마다 ticker 체크)
2. **트레일링 스톱** — 이익 0.6R 도달 시 활성화, ATR×0.7 추적
3. **시간 제한** — 90분 초과 시 시장가 강제 청산
4. **전략 청산** — EMA 크로스, RSI 반전, 거래량 고갈
5. **스캐너 탈락** — 후보 목록에서 제외 시 조건부 청산

## API 엔드포인트

### 조회

| 엔드포인트 | 설명 |
|-----------|------|
| `GET /api/status` | 시스템 상태, 프로세스 상태 |
| `GET /api/summary` | 잔고, P&L, 오늘 통계 |
| `GET /api/positions` | 오픈 포지션 (실시간 현재가, P&L%) |
| `GET /api/trades?days=30&page=1` | 거래 이력 (페이지네이션) |
| `GET /api/stats` | 전체 성과 통계 |
| `GET /api/stats/today` | 오늘 통계 |
| `GET /api/stats/{symbol}` | 종목별 통계 + 거래 내역 |
| `GET /api/equity-curve` | 에쿼티 차트 데이터 |
| `GET /api/symbols` | 거래된 종목 목록 |
| `GET /api/tuner` | 전략 튜너 상태 |
| `GET /api/config` | 현재 설정값 |
| `GET /api/system-info` | DB 크기, 업타임 |

### 제어

| 엔드포인트 | 설명 |
|-----------|------|
| `POST /api/trading/start` | 거래 시작 |
| `POST /api/trading/stop` | 거래 정지 (`{force_close: true}` 옵션) |
| `POST /api/trading/pause` | 신규 진입 일시 중지 |
| `POST /api/trading/reset` | 페이퍼 트레이딩 초기화 |
| `POST /api/tuner/apply` | 튜닝 파라미터 YAML 저장 |
| `POST /api/tuner/reset` | 튜너 레벨 0 초기화 |
| `POST /api/drawdown/resume` | 드로다운 중단 해제 |

## 디렉토리 구조

```
auto_bit/
├── start.sh / stop.sh / status.sh   # 운영 스크립트
├── config/
│   ├── app.yaml                     # 앱 설정
│   ├── credentials.yaml             # API 키 (gitignored)
│   ├── symbols.yaml                 # 심볼/타임프레임
│   └── strategy/
│       ├── scanner.yaml             # 종목 선정 파라미터
│       ├── position.yaml            # 진입/청산 파라미터
│       └── asset.yaml               # 자산 관리 파라미터
├── src/
│   ├── main.py                      # 오케스트레이터
│   ├── collector/                   # P1: 데이터 수집
│   ├── strategy/                    # P2: 전략 엔진 + 튜너
│   ├── order/                       # P3: 주문 실행
│   ├── tracker/                     # 포지션/P&L 추적
│   ├── gui/                         # P5: 웹 대시보드
│   ├── indicators/                  # 기술적 지표
│   └── utils/                       # 설정, DB, 로깅
├── data/                            # SQLite DB, PID 파일
├── logs/                            # 로그 파일
└── tests/                           # 테스트
```

## 페이퍼 → 라이브 전환

1. `config/credentials.yaml`에 **거래 권한** API 키 등록
2. `config/strategy/asset.yaml`에서 `initial_balance`를 실제 잔고에 맞게 조정
3. `config/app.yaml`에서 `mode: live` 또는 `./start.sh live`로 실행
4. 라이브 모드 시 확인 프롬프트가 표시됨

> **주의**: 라이브 모드는 실제 자금으로 거래합니다. 충분한 페이퍼 테스트 후 전환하세요.

## 트러블슈팅

### 시스템이 시작되지 않을 때

```bash
# 로그 확인
tail -50 logs/auto_bit.log

# 고아 프로세스 정리
./stop.sh --force

# DB 문제 시 초기화
rm data/auto_bit.db
./start.sh paper
```

### 시그널이 발생하지 않을 때

- `./status.sh`로 튜너 레벨 확인
- Settings 페이지에서 튜너 상태 확인 (L0에서 시그널 비율 < 5%이면 자동 완화)
- `config/strategy/position.yaml`에서 RSI 범위, 거래량 기준 직접 조정 가능

### 포트 충돌 시

```yaml
# config/app.yaml
gui:
  port: 9090   # 다른 포트로 변경
```
