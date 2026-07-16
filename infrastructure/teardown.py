#!/usr/bin/env python3
"""
ClauseGuard — teardown script.

Deletes every AWS resource created by provision.py / deploy.sh, in a safe
order (services before cluster, listener before target group before ALB,
etc.), using infrastructure/deployment_state.json to know what was created.

This is destructive. It will delete your DynamoDB tables and all data in them.
Run only when you are finished with the project or want to avoid ongoing
charges.

Usage:
    python3 infrastructure/teardown.py
    python3 infrastructure/teardown.py --keep-images   # leaves ECR repos/images intact
"""

import argparse
import json
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError

REGION = "ap-south-1"
PROJECT = "clauseguard"
STATE_FILE = os.path.join(os.path.dirname(__file__), "deployment_state.json")

session = boto3.Session(region_name=REGION)
ecs = session.client("ecs")
elbv2 = session.client("elbv2")
ec2 = session.client("ec2")
iam = session.client("iam")
s3 = session.client("s3")
sqs = session.client("sqs")
ecr = session.client("ecr")
logs = session.client("logs")
dynamodb = session.client("dynamodb")


def log(msg: str) -> None:
    print(f"[teardown] {msg}")


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        print("deployment_state.json not found — nothing to tear down.", file=sys.stderr)
        sys.exit(0)
    with open(STATE_FILE) as f:
        return json.load(f)


def ignore_not_found(func, *args, **kwargs):
    try:
        func(*args, **kwargs)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if "NotFound" in code or "NoSuchEntity" in code or code in (
            "ResourceNotFoundException", "ResourceInUseException"
        ):
            return
        raise


def teardown_ecs(state: dict) -> None:
    cluster_arn = state.get("cluster_arn")
    if not cluster_arn:
        return

    for service_name in (f"{PROJECT}-web-service", f"{PROJECT}-worker-service"):
        try:
            ecs.update_service(cluster=cluster_arn, service=service_name, desiredCount=0)
            ecs.delete_service(cluster=cluster_arn, service=service_name, force=True)
            log(f"Deleted ECS service: {service_name}")
        except ClientError as exc:
            if "ServiceNotFoundException" not in str(exc):
                log(f"Could not delete service {service_name}: {exc}")

    log("Waiting for tasks to stop...")
    time.sleep(15)

    ignore_not_found(ecs.delete_cluster, cluster=cluster_arn)
    log("Deleted ECS cluster")

    for family in (f"{PROJECT}-web", f"{PROJECT}-worker"):
        try:
            revisions = ecs.list_task_definitions(familyPrefix=family, status="ACTIVE")[
                "taskDefinitionArns"
            ]
            for arn in revisions:
                ecs.deregister_task_definition(taskDefinition=arn)
            log(f"Deregistered task definitions for family: {family}")
        except ClientError as exc:
            log(f"Could not deregister task definitions for {family}: {exc}")


def teardown_alb(state: dict) -> None:
    alb_arn = state.get("alb_arn")
    if alb_arn:
        listeners = elbv2.describe_listeners(LoadBalancerArn=alb_arn)["Listeners"]
        for listener in listeners:
            ignore_not_found(elbv2.delete_listener, ListenerArn=listener["ListenerArn"])
        ignore_not_found(elbv2.delete_load_balancer, LoadBalancerArn=alb_arn)
        log("Deleted ALB (and listeners)")

    tg_arn = state.get("target_group_arn")
    if tg_arn:
        log("Waiting for ALB deletion to finish before removing target group...")
        time.sleep(20)
        ignore_not_found(elbv2.delete_target_group, TargetGroupArn=tg_arn)
        log("Deleted target group")


def teardown_dynamodb() -> None:
    """Delete all three ClauseGuard DynamoDB tables."""
    for table_name in (
        f"{PROJECT}-users",
        f"{PROJECT}-contracts",
        f"{PROJECT}-tokens",
    ):
        try:
            dynamodb.delete_table(TableName=table_name)
            log(f"Deleted DynamoDB table: {table_name}")
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "ResourceNotFoundException":
                log(f"DynamoDB table not found (already deleted?): {table_name}")
            else:
                log(f"Could not delete DynamoDB table {table_name}: {exc}")


def teardown_networking(state: dict) -> None:
    net = state.get("network", {})
    # Security group deletion can fail if still referenced; retry briefly.
    for attempt in range(6):
        remaining = []
        for key in ("web_sg", "worker_sg", "alb_sg"):
            sg_id = net.get(key)
            if not sg_id:
                continue
            try:
                ec2.delete_security_group(GroupId=sg_id)
                log(f"Deleted security group: {key} ({sg_id})")
            except ClientError as exc:
                if "InvalidGroup.NotFound" in str(exc):
                    continue
                remaining.append(key)
        if not remaining:
            break
        log(f"Retrying security group deletion for: {remaining}")
        time.sleep(15)


def teardown_iam(state: dict) -> None:
    role_policy_names = {
        f"{PROJECT}-ecs-task-execution-role": [],
        f"{PROJECT}-web-task-role": ["web-permissions"],
        f"{PROJECT}-worker-task-role": ["worker-permissions"],
    }
    for role_name, inline_policies in role_policy_names.items():
        for policy_name in inline_policies:
            ignore_not_found(iam.delete_role_policy, RoleName=role_name, PolicyName=policy_name)

        try:
            attached = iam.list_attached_role_policies(RoleName=role_name)["AttachedPolicies"]
            for policy in attached:
                iam.detach_role_policy(RoleName=role_name, PolicyArn=policy["PolicyArn"])
        except ClientError:
            pass

        ignore_not_found(iam.delete_role, RoleName=role_name)
        log(f"Deleted IAM role: {role_name}")


def teardown_s3(state: dict) -> None:
    bucket_name = state.get("s3_bucket")
    if not bucket_name:
        return
    try:
        paginator = s3.get_paginator("list_object_versions")
        delete_keys = []
        for page in paginator.paginate(Bucket=bucket_name):
            for version in page.get("Versions", []):
                delete_keys.append({"Key": version["Key"], "VersionId": version["VersionId"]})
            for marker in page.get("DeleteMarkers", []):
                delete_keys.append({"Key": marker["Key"], "VersionId": marker["VersionId"]})

        for i in range(0, len(delete_keys), 1000):
            batch = delete_keys[i : i + 1000]
            if batch:
                s3.delete_objects(Bucket=bucket_name, Delete={"Objects": batch})

        s3.delete_bucket(Bucket=bucket_name)
        log(f"Deleted S3 bucket and all objects: {bucket_name}")
    except ClientError as exc:
        if "NoSuchBucket" not in str(exc):
            log(f"Could not delete S3 bucket: {exc}")


def teardown_sqs(state: dict) -> None:
    for key in ("queue_url", "dlq_url"):
        url = state.get(key)
        if url:
            ignore_not_found(sqs.delete_queue, QueueUrl=url)
    log("Deleted SQS queues")


def teardown_logs() -> None:
    for name in (f"/ecs/{PROJECT}-web", f"/ecs/{PROJECT}-worker"):
        ignore_not_found(logs.delete_log_group, logGroupName=name)
    log("Deleted CloudWatch log groups")


def teardown_ecr(keep_images: bool) -> None:
    if keep_images:
        log("Skipping ECR repository deletion (--keep-images was passed)")
        return
    for name in (f"{PROJECT}-web", f"{PROJECT}-worker"):
        ignore_not_found(ecr.delete_repository, repositoryName=name, force=True)
    log("Deleted ECR repositories")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-images", action="store_true")
    args = parser.parse_args()

    state = load_state()

    confirmation = input(
        "This will permanently delete all ClauseGuard AWS resources, including the "
        "DynamoDB tables and all uploaded contracts. Type 'DELETE' to continue: "
    )
    if confirmation != "DELETE":
        log("Aborted.")
        return

    teardown_ecs(state)
    teardown_alb(state)
    teardown_dynamodb()
    teardown_networking(state)
    teardown_iam(state)
    teardown_s3(state)
    teardown_sqs(state)
    teardown_logs()
    teardown_ecr(args.keep_images)

    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)

    log("Teardown complete.")


if __name__ == "__main__":
    main()
