# CFScan-eccho

CloudFormation IaC Generator를 자동화하는 Lambda 기반 파이프라인.
EventBridge Scheduler로 주기 실행되며, 계정 내 CloudFormation으로 관리되지 않는 리소스를 스캔하고
날짜별 CloudFormation 템플릿을 자동 생성한다. 생성된 템플릿으로 새 스택을 만들어 리소스를 복구하는
방식까지 검증했다.

## 구조

```
Scheduler (EventBridge) → Lambda (CFScan-eccho) → CloudFormation IaC Generator API
```

Step Functions 없이 단일 Lambda가 매 호출마다 현재 상태(스캔 중/완료, 템플릿 생성 중/완료)를
CloudFormation에 조회해서 스스로 다음 단계로 한 칸씩 진행하는 self-polling 구조로 구현했다.

### 진행 순서 (하루 기준)

1. 오늘 날짜로 진행 중이거나 완료된 스캔이 없으면 `StartResourceScan`
2. 스캔이 `IN_PROGRESS`면 이번 실행은 로그만 남기고 대기
3. 오늘자 스캔이 `COMPLETE`고 아직 템플릿을 안 만들었으면 `CreateGeneratedTemplate`
4. 템플릿이 `CREATE_IN_PROGRESS`면 로그만 남기고 대기
5. 템플릿이 `COMPLETE`면 완료 처리, 실패면 리소스별 실패 사유를 로그로 남김

템플릿 이름은 `{접두사}-{YYYYMMDD}` 형태로 매일 새로 생성되어 날짜별로 누적된다.
같은 날 여러 번 실행돼도 이미 완료된 템플릿은 재생성하지 않는다.

## 환경 변수

| 이름 | 설명 |
|---|---|
| `TEMPLATE_NAME` | 생성될 템플릿 이름의 접두사 (예: `cfscan-eccho-template`) |
| `RESOURCE_TYPES` | 선택. 콤마로 구분한 리소스 타입 필터 (예: `AWS::Lambda::Function,AWS::S3::Bucket`). 미지정 시 스캔된 전체 리소스 대상 |

## 필요 IAM 권한

```
cloudformation:StartResourceScan
cloudformation:DescribeResourceScan
cloudformation:ListResourceScans
cloudformation:ListResourceScanResources
cloudformation:CreateGeneratedTemplate
cloudformation:DescribeGeneratedTemplate
cloudformation:DeleteGeneratedTemplate
cloudformation:GetResource
cloudformation:GetResourceRequestStatus
```

CloudFormation IaC Generator는 스캔한 리소스의 상세 속성을 읽을 때 내부적으로 Cloud Control API를 통해
각 리소스 서비스(EC2, ELB, S3, Lambda 등)의 Describe 계열 API를 대신 호출한다. 따라서 위 CloudFormation
권한만으로는 부족하고, 스캔 대상이 될 수 있는 서비스 범위에 대한 읽기 권한(`ReadOnlyAccess` 관리형 정책 등)이
추가로 필요하다.

## 검증 결과

- 리소스 스캔 → 템플릿 생성 자동화 정상 동작 확인
- 생성된 템플릿으로 CloudFormation `Create stack` 실행 → 스캔된 리소스가 스택 기반으로 재현되는 것까지 검증 완료
