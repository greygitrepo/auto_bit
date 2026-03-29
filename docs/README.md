# auto_bit Documentation

## Quick Navigation

| 문서 | 내용 | 언제 읽나 |
|------|------|----------|
| [현재 상태 요약](#현재-상태) | 시스템 현황 한눈에 | 항상 먼저 |
| [strategy-guide.md](strategy-guide.md) | 그리드 전략 원리 + 파라미터 | 전략 이해할 때 |
| [config-guide.md](config-guide.md) | 설정 파일 구조 | 파라미터 조정할 때 |
| [architecture.md](architecture.md) | 시스템 아키텍처 (P1~P5) | 코드 수정할 때 |
| [final_report.md](final_report.md) | 최적화 결과 리포트 | 성과 확인할 때 |
| [live_transition_plan.md](live_transition_plan.md) | 라이브 전환 계획 | 실전 전환 준비할 때 |
| [parity_analysis.md](parity_analysis.md) | Paper vs Live 간극 분석 | 실전 전환 준비할 때 |
| [team_status.md](team_status.md) | 최적화 사이클 로그 | 변경 이력 볼 때 |

---

## 현재 상태

**마지막 업데이트:** 2026-03-29

### 시스템
- **전략:** Grid + Directional Bias Hybrid
- **모드:** Paper Trading (Bybit USDT Perpetual Futures)
- **자본:** 20 USDT, 5x Leverage

### 최종 성과 (18시간 Paper Trading)
| 지표 | 값 |
|------|-----|
| 거래 수 | 187 |
| 승률 | 97.3% |
| 순이익 | +1.116 USDT (+5.32%) |
| 일평균 수익 | ~7.5%/day (paper) |

### 현재 최적 파라미터 (`config/strategy/grid.yaml`)
```yaml
range_atr_multiplier: 1.2
min_spacing_pct: 0.55
target_spacing_pct: 0.55
recenter_threshold_pct: 3.0
recenter_interval_minutes: 180
max_symbols: 8
max_open_levels: 8
leverage: 5
qty_per_level_pct: 2.0
slippage_bps: 15  # (asset.yaml)
```

### 다음 단계
**라이브 전환 준비** → [live_transition_plan.md](live_transition_plan.md) 참조
- Phase 1 (CRITICAL): 인터페이스 수정, Net Position Ledger, close_by_key
- Phase 2 (HIGH): 에러 핸들링, 상태 복구, 슬리피지 동적 체크
- Phase 3 (MEDIUM): 배치 주문, 펀딩비, WebSocket
- Phase 4 (LOW): 리밋오더 지원

---

## 디렉토리 구조

```
docs/
├── README.md                    ← 이 파일 (시작점)
├── strategy-guide.md            ← 그리드 전략 원리 + 파라미터 설명
├── config-guide.md              ← 설정 파일 구조
├── architecture.md              ← 시스템 아키텍처 (프로세스, 데이터 흐름)
├── final_report.md              ← 최적화 최종 결과 리포트
├── live_transition_plan.md      ← 라이브 전환 12개 이슈 + 해결 계획
├── parity_analysis.md           ← Paper vs Live 간극 상세 분석
├── team_status.md               ← 78사이클 최적화 변경 로그
├── iterations/                  ← (비어있음 — 아카이브 이동)
└── archive/                     ← 과거 문서 보관
    ├── iterations/              ← 자동 생성된 사이클 리포트 (74개)
    ├── old_strategy/            ← MomentumScalper 시절 문서
    ├── decisions/               ← ADR (Architecture Decision Records)
    └── dev_reports/             ← 개발 중 생성된 분석/설계 문서
```

---

## 핵심 코드 파일

### 전략 (P2)
| 파일 | 역할 |
|------|------|
| `src/strategy/position/grid_engine.py` | 그리드 레벨 상태머신 + 필/TP 감지 |
| `src/strategy/position/grid_bias.py` | 메인 전략 클래스 (그리드 생성/관리) |
| `src/strategy/position/bias_calculator.py` | 방향 바이어스 계산 (EMA + 펀딩비 + BTC/ETH) |
| `src/strategy/asset/grid_sizing.py` | 포지션 사이징 + 리스크 체크 |
| `src/strategy/process.py` | P2 프로세스 (전략 평가 루프) |

### 주문 (P3)
| 파일 | 역할 |
|------|------|
| `src/order/grid_manager.py` | 그리드 마이크로포지션 관리 |
| `src/order/paper_executor.py` | Paper 주문 시뮬레이션 |
| `src/order/live_executor.py` | Live Bybit API 주문 (미완성) |
| `src/order/process.py` | P3 프로세스 (주문 실행 루프) |

### 설정
| 파일 | 역할 |
|------|------|
| `config/strategy/grid.yaml` | 그리드 전략 파라미터 |
| `config/strategy/asset.yaml` | 자본/리스크/수수료 설정 |
| `config/strategy/scanner.yaml` | 심볼 스캐너 설정 |
| `config/app.yaml` | 앱 모드, 로깅, DB |
| `config/symbols.yaml` | 타임프레임, 베이스 심볼 |

### 테스트
| 파일 | 테스트 수 |
|------|----------|
| `tests/test_grid_engine.py` | 11 |
| `tests/test_bias_calculator.py` | 5 |
| `tests/test_grid_margin.py` | 6 |
| 기타 기존 테스트 | 17 |

---

## 운영 명령어

```bash
# 시작/종료
bash start.sh paper          # GUI 포함
bash start.sh paper --headless
bash stop.sh
bash stop.sh --force
bash status.sh

# 모니터링
python3 scripts/iteration_cycle.py --once    # 1회 분석
python3 scripts/iteration_cycle.py --loop 30 # 30분 반복

# 테스트
python3 -m pytest tests/ -v

# DB 리셋
python3 -c "
import sqlite3; conn = sqlite3.connect('data/auto_bit.db')
for t in ['trades','positions','daily_performance','system_state','grid_levels','grid_state']:
    conn.execute(f'DELETE FROM {t}')
conn.commit(); conn.close()
"

# GUI
open http://localhost:8080
```
