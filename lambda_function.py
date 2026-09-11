"""
CFScan-eccho

EventBridge Scheduler가 주기적으로 이 함수를 트리거한다고 가정.
매 실행마다 "지금 어느 단계인지"를 CloudFormation 상태를 조회해서 스스로 판단하고,
다음 단계로 한 칸씩 진행한다. (Step Functions 없이 스케줄러 하나로 굴러가는 방식)

템플릿 이름은 TEMPLATE_NAME(접두사) + 오늘 날짜(YYYYMMDD)로 매일 새로 생겨서 누적된다.
예: cfscan-eccho-template-20260911, cfscan-eccho-template-20260912, ...
날짜가 바뀌면 리소스 스캔도 그날 걸로 새로 시작한다(과거 스캔 결과 재사용 안 함).

진행 순서(하루 기준):
  1) 오늘 날짜로 진행중/완료된 스캔 없음 -> StartResourceScan
  2) 스캔 IN_PROGRESS -> 이번 실행은 로그만 남기고 대기
  3) 오늘자 스캔 COMPLETE, 아직 오늘자 템플릿 생성 안 함 -> CreateGeneratedTemplate
  4) 템플릿 CREATE_IN_PROGRESS -> 로그만 남기고 대기
  5) 템플릿 COMPLETE -> 완료 로그 (오늘은 여기서 끝, 다음날 새 이름으로 다시 시작)
  6) 오늘자 템플릿이 이미 COMPLETE면 재작업 안 하고 스킵
"""

import json
import os
from datetime import datetime, timezone

import boto3

cfn = boto3.client("cloudformation")

# Lambda 환경변수로 설정: TEMPLATE_NAME(접두사), RESOURCE_TYPES(콤마구분, 선택)
TEMPLATE_BASE_NAME = os.environ.get("TEMPLATE_NAME", "cfscan-eccho-template")
RESOURCE_TYPES = os.environ.get("RESOURCE_TYPES")  # e.g. "AWS::Lambda::Function,AWS::S3::Bucket"
RESOURCE_TYPE_FILTER = [t.strip() for t in RESOURCE_TYPES.split(",")] if RESOURCE_TYPES else None

# 실행 시점의 날짜를 붙여서 템플릿 이름이 매일 새로 생기게 함 (예: cfscan-eccho-template-20260911)
# 같은 날 여러 번 실행되면 같은 이름을 계속 재사용(중복 생성 안 함), 날짜가 바뀌면 새 템플릿으로 누적됨
TODAY = datetime.now(timezone.utc).strftime("%Y%m%d")
TEMPLATE_NAME = f"{TEMPLATE_BASE_NAME}-{TODAY}"


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}")


def get_latest_scan():
    scans = cfn.list_resource_scans().get("ResourceScanSummaries", [])
    return scans[0] if scans else None


def get_existing_template():
    try:
        return cfn.describe_generated_template(GeneratedTemplateName=TEMPLATE_NAME)
    except cfn.exceptions.ClientError:
        return None
    except Exception:
        return None


def lambda_handler(event, context):
    log("=== IaC Scan Lambda Start ===")
    log(f"Event: {json.dumps(event, ensure_ascii=False)}")

    # 5/6) 이미 만들어진 템플릿 상태부터 확인
    existing = get_existing_template()
    if existing:
        status = existing["Status"]
        if status == "COMPLETE":
            log(f"템플릿 '{TEMPLATE_NAME}' 이미 COMPLETE. 더 할 일 없음.")
            return {"statusCode": 200, "body": json.dumps({"phase": "done", "template_status": status})}
        if status in ("CREATE_PENDING", "CREATE_IN_PROGRESS"):
            log(f"템플릿 생성 진행중: {status}. 다음 실행에서 재확인.")
            return {"statusCode": 200, "body": json.dumps({"phase": "creating_template", "template_status": status})}
        if status == "FAILED":
            log(f"템플릿 생성 실패(요약): {existing.get('StatusReason')}")
            # describe_generated_template 응답 안에 리소스별 상태/사유가 이미 들어있음
            failed_resources = []
            for r in existing.get("Resources", []):
                if r.get("ResourceStatus") == "FAILED":
                    detail = {
                        "ResourceType": r.get("ResourceType"),
                        "LogicalResourceId": r.get("LogicalResourceId"),
                        "ResourceStatusReason": r.get("ResourceStatusReason"),
                    }
                    failed_resources.append(detail)
                    log(f"  - 실패 리소스: {detail}")

            return {
                "statusCode": 500,
                "body": json.dumps(
                    {"phase": "template_failed", "reason": existing.get("StatusReason"), "failed_resources": failed_resources},
                    ensure_ascii=False,
                ),
            }

    # 1/2) 스캔 상태 확인 (오늘 날짜로 만든 스캔이 아니면 최신 리소스 반영을 위해 새로 스캔)
    scan = get_latest_scan()
    scan_is_today = bool(scan) and scan.get("StartTime") and scan["StartTime"].strftime("%Y%m%d") == TODAY

    if scan and scan["Status"] == "IN_PROGRESS":
        log(f"리소스 스캔 진행중 (scan_id={scan['ResourceScanId']}). 다음 실행에서 재확인.")
        return {"statusCode": 200, "body": json.dumps({"phase": "scanning", "scan_id": scan["ResourceScanId"]})}

    if not scan or scan["Status"] != "COMPLETE" or not scan_is_today:
        resp = cfn.start_resource_scan()
        log(f"리소스 스캔 시작(오늘자 스캔 없음/오래됨): {resp['ResourceScanId']}")
        return {"statusCode": 200, "body": json.dumps({"phase": "scan_started", "scan_id": resp["ResourceScanId"]})}

    # 3) 스캔 완료 상태 -> 템플릿 생성
    scan_id = scan["ResourceScanId"]
    resources_to_include = []
    paginator = cfn.get_paginator("list_resource_scan_resources")
    for page in paginator.paginate(ResourceScanId=scan_id):
        for r in page.get("Resources", []):
            if RESOURCE_TYPE_FILTER and r["ResourceType"] not in RESOURCE_TYPE_FILTER:
                continue
            resources_to_include.append(
                {"ResourceType": r["ResourceType"], "ResourceIdentifier": r["ResourceIdentifier"]}
            )

    if not resources_to_include:
        log("조건에 맞는 리소스 없음. RESOURCE_TYPES 필터 확인 필요.")
        return {"statusCode": 200, "body": json.dumps({"phase": "no_resources"})}

    cfn.create_generated_template(GeneratedTemplateName=TEMPLATE_NAME, Resources=resources_to_include)
    log(f"템플릿 생성 요청: {TEMPLATE_NAME} (리소스 {len(resources_to_include)}개)")

    return {
        "statusCode": 200,
        "body": json.dumps(
            {"phase": "template_creation_started", "template_name": TEMPLATE_NAME, "resource_count": len(resources_to_include)},
            ensure_ascii=False,
        ),
    }
