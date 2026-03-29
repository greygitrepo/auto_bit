# ADR-001: 비동기 프레임워크 선택

## 상태

`결정됨`

## 날짜

2026-03-25

## 맥락

자동거래 시스템은 WebSocket 데이터 수신, 지표 계산, 주문 실행 등 여러 작업을 동시에 처리해야 한다.
Python에서 동시성을 처리하는 방식을 결정해야 한다.

## 선택지

### 선택지 A: asyncio (순수 비동기)

**설명:**
Python 표준 라이브러리 asyncio를 사용하여 단일 스레드 이벤트 루프 기반으로 동시성을 처리한다.

**장점:**
- Python 표준 라이브러리, 추가 의존성 없음
- WebSocket, HTTP 요청 등 I/O 작업에 최적
- 단일 스레드로 race condition 걱정 없음
- pybit이 asyncio를 지원함

**단점:**
- CPU 집약적 작업(지표 계산)이 이벤트 루프를 블로킹할 수 있음
- 모든 라이브러리가 async를 지원하지는 않음
- 디버깅이 다소 복잡

### 선택지 B: threading + asyncio 혼합

**설명:**
WebSocket 등 I/O는 asyncio, 지표 계산 등 CPU 작업은 별도 스레드로 처리한다.

**장점:**
- I/O와 CPU 작업을 분리하여 블로킹 방지
- 기존 동기 라이브러리를 스레드에서 그대로 사용 가능

**단점:**
- 스레드 간 동기화 필요
- 코드 복잡도 증가
- 디버깅 난이도 상승

### 선택지 C: 멀티프로세스 (multiprocessing)

**설명:**
데이터 수집, 전략 실행, 주문 관리를 별도 프로세스로 분리한다.

**장점:**
- 완전한 병렬 처리
- 프로세스 간 격리 (장애 전파 방지)

**단점:**
- 프로세스 간 통신(IPC) 복잡
- 메모리 사용량 증가
- 초기 단계에서 과한 복잡도

## 결정

**선택지 C: 멀티프로세스 (multiprocessing)** 채택.

각 Agent를 독립 프로세스로 실행하여 장애 격리와 완전한 병렬 처리를 확보한다.
프로세스 간 통신은 `multiprocessing.Queue` 기반으로 구현하며, 각 프로세스 내부에서는 필요 시 asyncio를 사용할 수 있다 (예: WebSocket 수신).

### 프로세스 구조

```
Main Process (Orchestrator)
  │
  ├── [P1] Data Collector    ── WebSocket 수신, 캔들/오더북 수집
  │         └── 내부: asyncio event loop
  │
  ├── [P2] Strategy Engine   ── 지표 계산 + 스캐너 + 포지션 전략
  │         └── 내부: 동기 루프 (CPU 집약)
  │
  ├── [P3] Order Manager     ── 주문 실행 + 자산관리 + 포지션 추적
  │         └── 내부: asyncio (API 호출)
  │
  └── [P4] Notification      ── 텔레그램 알림
            └── 내부: asyncio (봇 API)
```

### 프로세스 간 통신 (IPC)

```
[P1] ──Queue──▶ [P2]    # 시장 데이터 (캔들, 틱)
[P2] ──Queue──▶ [P3]    # 매매 신호
[P3] ──Queue──▶ [P4]    # 알림 이벤트
[P3] ──Queue──▶ [P2]    # 포지션 상태 (피드백)
```

### 장애 처리

- Main Process가 각 자식 프로세스를 감시 (watchdog)
- 프로세스 비정상 종료 시 자동 재시작
- P3(Order Manager) 장애 시 열린 포지션의 SL/TP는 서버사이드 주문으로 보호

## 영향

- 전체 코드 구조: Agent별 독립 프로세스, Queue 기반 메시지 패싱
- IPC 메시지 포맷 표준화 필요 (dataclass 직렬화)
- 각 프로세스 내부는 자유롭게 asyncio 또는 동기 방식 선택 가능
- 에러 핸들링: 프로세스 레벨 watchdog + 프로세스 내부 예외 처리 이중 구조
- 메모리 사용량 증가 허용 (격리 이점이 더 큼)
