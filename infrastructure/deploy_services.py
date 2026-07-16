#!/usr/bin/env python3
"""
ClauseGuard — deploy_services.py

Run AFTER infrastructure/provision.py and AFTER Docker images have been
built and pushed to ECR (see infrastructure/deploy.sh, which calls this
script automatically as its final step).

This script:
  1. Reads infrastructure/deployment_state.json for all resource IDs.
  2. Registers a new task definition revision for web and worker pointing
     at the freshly pushed ':latest' image digest.
  3. Creates the ECS services if they don't exist yet, or updates them to
     the new task definition revision (triggering a rolling deployment) if
     they already exist.
"""

import json
import os
import sys

import boto3

REGION = "ap-south-1"
PROJECT = "clauseguard"
STATE_FILE = os.path.join(os.path.dirname(__file__), "deployment_state.json")

session = boto3.Session(region_name=REGION)
ecs = session.client("ecs")


def log(msg: str) -> None:
    print(f"[deploy_services] {msg}")


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        print(
            "deployment_state.json not found. Run infrastructure/provision.py first.",
            file=sys.stderr,
        )
        sys.exit(1)
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def register_new_revision(family: str) -> str:
    """Re-registers the task family using its current (already-defined) container
    definitions but pointing at the ':latest' tag, which now resolves to the
    image most recently pushed by deploy.sh."""
    current = ecs.describe_task_definition(taskDefinition=family)["taskDefinition"]

    container_defs = current["containerDefinitions"]
    # Image URI is already suffixed with :latest from provision.py, so ECS
    # will pull the newest pushed layer on the next deployment automatically.

    response = ecs.register_task_definition(
        family=family,
        networkMode=current["networkMode"],
        requiresCompatibilities=current["requiresCompatibilities"],
        cpu=current["cpu"],
        memory=current["memory"],
        executionRoleArn=current["executionRoleArn"],
        taskRoleArn=current["taskRoleArn"],
        containerDefinitions=container_defs,
    )
    arn = response["taskDefinition"]["taskDefinitionArn"]
    log(f"Registered new revision: {arn}")
    return arn


def ensure_service(
    cluster_arn: str,
    service_name: str,
    task_def_arn: str,
    subnet_ids: list,
    security_group_id: str,
    target_group_arn: str = None,
    container_name: str = None,
    container_port: int = None,
) -> None:
    existing = ecs.describe_services(cluster=cluster_arn, services=[service_name])["services"]
    active = [s for s in existing if s["status"] == "ACTIVE"]

    if active:
        ecs.update_service(
            cluster=cluster_arn,
            service=service_name,
            taskDefinition=task_def_arn,
            forceNewDeployment=True,
        )
        log(f"Updated service {service_name} -> new revision, forcing redeploy")
        return

    kwargs = dict(
        cluster=cluster_arn,
        serviceName=service_name,
        taskDefinition=task_def_arn,
        desiredCount=1,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": subnet_ids,
                "securityGroups": [security_group_id],
                "assignPublicIp": "ENABLED",
            }
        },
    )
    if target_group_arn:
        kwargs["loadBalancers"] = [
            {
                "targetGroupArn": target_group_arn,
                "containerName": container_name,
                "containerPort": container_port,
            }
        ]

    ecs.create_service(**kwargs)
    log(f"Created service: {service_name}")


def main() -> None:
    state = load_state()

    web_task_def_arn = register_new_revision(f"{PROJECT}-web")
    worker_task_def_arn = register_new_revision(f"{PROJECT}-worker")

    state["web_task_def_arn"] = web_task_def_arn
    state["worker_task_def_arn"] = worker_task_def_arn

    net = state["network"]

    ensure_service(
        cluster_arn=state["cluster_arn"],
        service_name=f"{PROJECT}-web-service",
        task_def_arn=web_task_def_arn,
        subnet_ids=net["subnet_ids"],
        security_group_id=net["web_sg"],
        target_group_arn=state["target_group_arn"],
        container_name=f"{PROJECT}-web",
        container_port=8080,
    )

    ensure_service(
        cluster_arn=state["cluster_arn"],
        service_name=f"{PROJECT}-worker-service",
        task_def_arn=worker_task_def_arn,
        subnet_ids=net["subnet_ids"],
        security_group_id=net["worker_sg"],
    )

    save_state(state)
    log("Services deployed.")
    log(f"Web app URL: http://{state['alb_dns_name']}")
    log("Note: the ALB can take 1-3 minutes after first deployment before health checks pass.")


if __name__ == "__main__":
    main()
