# CFScan-eccho

## 개요

CloudFormation으로 관리되지 않는(미관리) 리소스를 주기적으로 스캔하고, CloudFormation 템플릿으로
자동 백업해두는 Lambda 기반 파이프라인이다.

클라우드 운영을 하다 보면 콘솔에서 급하게 만들고 나서 IaC로 옮기지 못한 리소스, 혹은 예전에
수동으로 만들어져 이력이 남지 않은 리소스가 계정 곳곳에 쌓이기 마련이다. 이런 리소스는 장애나
실수로 삭제됐을 때 복구할 방법이 마땅치 않다. 이 프로젝트는 AWS CloudFormation IaC Generator
기능을 자동화해서, 그런 리소스들의 설정값을 정기적으로 템플릿 형태로 스냅샷처럼 남겨두는 것을
목표로 만들었다. 템플릿만 있으면 리소스가 사라지더라도 CloudFormation 스택 생성으로 원래 설정을
그대로 복구할 수 있다.

## 왜 이렇게 만들었나

- **Step Functions 없이 구현**: 스캔과 템플릿 생성 모두 완료까지 수 분씩 걸리는 비동기 작업이라,
  정석대로면 Step Functions로 Wait/Choice를 구성하는 게 맞다. 하지만 이 정도 규모의 자동화에
  별도 오케스트레이션 서비스를 추가하는 게 과한 것 같아서, 대신 Lambda 하나가 "자기가 지금
  어느 단계인지"를 매번 CloudFormation에 물어보고 판단하는 self-polling 방식으로 단순화했다.
  EventBridge Scheduler가 몇 분 간격으로 이 Lambda를 계속 호출해주는 것만으로 전체 흐름이
  완성된다.
- **날짜 기반 템플릿 누적**: 백업이 목적이라면 최신 상태 하나만 덮어쓰는 것보다 시점별 스냅샷이
  쌓이는 게 유용하다고 판단해서, 템플릿 이름에 실행 날짜(YYYYMMDD)를 붙여 매일 새 템플릿이
  누적되도록 했다. 같은 날 여러 번 실행돼도 이미 완료된 템플릿은 재생성하지 않아 불필요한 API
  호출을 줄였다.

## 아키텍처

```
EventBridge Scheduler (주기 실행)
        │
        ▼
  Lambda: CFScan-eccho
        │
        ▼
CloudFormation IaC Generator API
  (StartResourceScan → CreateGeneratedTemplate)
        │
        ▼
날짜별 CloudFormation 템플릿 누적 생성
        │
        ▼ (필요 시)
CloudFormation Create Stack → 리소스 복구
```

## 동작 방식

Lambda는 호출될 때마다 아래 순서를 판단해서 한 단계씩만 진행한다.

1. 오늘 날짜로 생성된 템플릿이 이미 `COMPLETE` 상태면 → 더 할 일 없이 종료 (중복 작업 방지)
2. 오늘 날짜로 진행 중이거나 완료된 스캔이 없으면 → `StartResourceScan`으로 리소스 스캔 시작
3. 스캔이 `IN_PROGRESS`면 → 이번 실행은 로그만 남기고 다음 호출을 기다림
4. 오늘자 스캔이 `COMPLETE`면 → 스캔된 리소스 목록을 조회해 `CreateGeneratedTemplate`으로 템플릿 생성 요청
5. 템플릿이 `CREATE_IN_PROGRESS`면 → 로그만 남기고 대기
6. 템플릿 생성이 `FAILED`면 → 리소스별 실패 사유(`ResourceStatusReason`)까지 로그로 남겨서 원인 추적 가능하게 함

## 환경 변수

| 이름 | 설명 |
|---|---|
| `TEMPLATE_NAME` | 생성될 템플릿 이름의 접두사. 실제 템플릿 이름은 `{접두사}-{YYYYMMDD}`로 자동 조합됨 |
| `RESOURCE_TYPES` | 선택. 콤마로 구분한 리소스 타입 필터 (예: `AWS::Lambda::Function,AWS::S3::Bucket`). 미지정 시 스캔된 전체 리소스가 대상 |

## 검증 결과

- 리소스 스캔 → 템플릿 자동 생성까지 스케줄러 기반으로 정상 동작하는 것을 확인
- CloudFormation IaC Generator가 리소스 속성을 읽을 때 Cloud Control API를 통해 각 서비스(EC2, ELB,
  S3, Lambda 등)를 대신 호출하는 구조라는 것을 파악하고, 그에 맞춰 필요한 접근 권한 범위를 구성
- 생성된 템플릿으로 CloudFormation **Create stack**을 실행해, 스캔 당시 리소스 설정이 실제로 새
  스택으로 재현되는 것까지 확인 — 백업된 템플릿이 복구 용도로 실사용 가능함을 검증

## 배운 점

- CloudFormation IaC Generator는 콘솔에서 클릭 몇 번으로 끝나는 기능처럼 보이지만, API로 자동화하려면
  스캔과 템플릿 생성이 둘 다 비동기라는 점, 그리고 내부적으로 Cloud Control API에 위임 호출한다는 점을
  이해해야 제대로 동작시킬 수 있었다.
- 오케스트레이션을 항상 Step Functions로 가져가기보다, 짧은 주기로 반복 호출되는 스케줄러 환경이라면
  "상태를 스스로 조회해서 다음 단계를 판단하는" 단일 함수 구조로도 충분히 안정적인 자동화를 만들 수
  있다는 걸 확인했다.
