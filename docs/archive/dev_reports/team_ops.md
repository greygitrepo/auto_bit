# Auto Bit — Agent Team Operations

## Deadline: 2026-03-29 (일요일) 16:00 KST

## Team Structure

### PM (프로젝트매니저)
- 각 팀 요구사항 조율, 일정 관리
- 팀 간 의견 충돌 시 결정

### Strategy Analysis (전략 분석 팀)
- 실시간 페이퍼 트레이딩 성과 모니터링
- 승률, PnL, 셀 완료율, 심볼별 수익성 분석
- 문제 패턴 식별 (특정 시간대, 특정 심볼, 특정 시장 상황)

### Strategy Design (전략 설계 개발팀)
- 분석 결과 기반 파라미터 최적화
- 그리드 spacing, 레벨 수, 바이어스 가중치 조정
- 새로운 전략 요소 설계 (동적 spacing, 변동성 적응형 등)

### Implementation (구현 팀)
- 설계팀이 결정한 전략을 코드로 구현
- 기능 추가/수정

### System Monitoring (시스템 모니터링 및 이슈해결)
- P1/P2/P3/P5 프로세스 상태 감시
- DB 무결성, 마진 회계 검증
- 에러 로그 분석 및 즉시 수정

### Parity (정합 팀)
- Paper 모드 vs Live 모드 간극 분석
- 슬리피지, 수수료, 레이트리밋 시뮬레이션 정확도 검증
- Live 전환 시 필요한 코드 변경 목록 관리

### QA (품질보증 팀)
- 그리드 엔진 단위 테스트
- 바이어스 계산 테스트
- 마진 회계 통합 테스트
- 엣지 케이스 테스트 (리센터, 동시 fill/TP, 잔고 부족 등)

## Current System State
- Strategy: Grid + Directional Bias Hybrid
- Capital: 20 USDT (paper)
- Leverage: 5x
- Max symbols: 5
- Max open levels: 6
- Min spacing: 0.45%
- Slippage: 15 bps
- Grid center: last_close

## Key Files
- Config: config/strategy/grid.yaml, config/strategy/asset.yaml
- Grid Engine: src/strategy/position/grid_engine.py
- Grid Strategy: src/strategy/position/grid_bias.py
- Bias Calculator: src/strategy/position/bias_calculator.py
- Grid Sizing: src/strategy/asset/grid_sizing.py
- Grid Manager (P3): src/order/grid_manager.py
- P2 Process: src/strategy/process.py
- P3 Process: src/order/process.py
- Paper Executor: src/order/paper_executor.py
- DB: src/utils/db.py, data/auto_bit.db
