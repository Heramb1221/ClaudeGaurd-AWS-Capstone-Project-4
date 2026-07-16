#!/usr/bin/env python3
"""
ClauseGuard — infrastructure provisioning script.

Creates every AWS resource ClauseGuard needs, in order, using boto3. Designed
to be idempotent: safe to re-run if it fails partway through, since every
step first checks whether the resource already exists before creating it.

Resources created (region: ap-south-1):
  - ECR repositories: clauseguard-web, clauseguard-worker
  - S3 bucket for contract files + generated reports
  - SQS queue + dead-letter queue for the processing pipeline
  - DynamoDB tables: clauseguard-users, clauseguard-contracts, clauseguard-tokens
  - Security groups (ALB, ECS web, ECS worker)
  - IAM roles (ECS task execution role, web task role, worker task role)
  - CloudWatch Log groups
  - Application Load Balancer + target group + listener
  - ECS cluster (Fargate)
  - ECS task definitions + services (web behind the ALB, worker standalone)

NOTE: RDS/PostgreSQL has been removed. All persistence uses DynamoDB, which
is entirely AWS Free Tier eligible and requires no DB credentials.

This script does NOT build or push Docker images — run infrastructure/deploy.sh
for that after provisioning completes.

Nothing in this script is executed automatically. You must run it yourself
with valid AWS credentials configured (aws configure / environment
variables / an assumed role) that have sufficient permissions.
"""

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
sts = session.client("sts")
ec2 = session.client("ec2")
ecr = session.client("ecr")
ecs = session.client("ecs")
iam = session.client("iam")
s3 = session.client("s3")
sqs = session.client("sqs")
logs = session.client("logs")
elbv2 = session.client("elbv2")
dynamodb = session.client("dynamodb")


def log(msg: str) -> None:
    print(f"[provision] {msg}")


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def get_account_id() -> str:
    return sts.get_caller_identity()["Account"]


# ---------------------------------------------------------------------------
# ECR
# ---------------------------------------------------------------------------

def ensure_ecr_repository(name: str) -> str:
    try:
        response = ecr.describe_repositories(repositoryNames=[name])
        repo_uri = response["repositories"][0]["repositoryUri"]
        log(f"ECR repository already exists: {name}")
        return repo_uri
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "RepositoryNotFoundException":
            raise

    response = ecr.create_repository(
        repositoryName=name,
        imageScanningConfiguration={"scanOnPush": True},
        encryptionConfiguration={"encryptionType": "AES256"},
    )
    log(f"Created ECR repository: {name}")
    return response["repository"]["repositoryUri"]


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------

def ensure_s3_bucket(account_id: str) -> str:
    bucket_name = f"{PROJECT}-contracts-{account_id}"

    try:
        s3.head_bucket(Bucket=bucket_name)
        log(f"S3 bucket already exists: {bucket_name}")
    except ClientError as exc:
        status = exc.response["ResponseMetadata"]["HTTPStatusCode"]
        if status != 404:
            raise
        s3.create_bucket(
            Bucket=bucket_name,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        log(f"Created S3 bucket: {bucket_name}")

    s3.put_public_access_block(
        Bucket=bucket_name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )

    s3.put_bucket_encryption(
        Bucket=bucket_name,
        ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        },
    )

    # NOTE: the correct boto3 S3 method is put_bucket_cors (NOT put_bucket_cors_configuration).
    s3.put_bucket_cors(
        Bucket=bucket_name,
        CORSConfiguration={
            "CORSRules": [
                {
                    "AllowedOrigins": ["*"],
                    "AllowedMethods": ["GET"],
                    "AllowedHeaders": ["*"],
                    "MaxAgeSeconds": 3000,
                }
            ]
        },
    )

    s3.put_bucket_lifecycle_configuration(
        Bucket=bucket_name,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "abort-incomplete-multipart-uploads",
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
                }
            ]
        },
    )

    log(f"S3 bucket configured (public access blocked, encrypted, CORS set): {bucket_name}")
    return bucket_name


# ---------------------------------------------------------------------------
# SQS
# ---------------------------------------------------------------------------

def ensure_sqs_queues() -> dict:
    dlq_name = f"{PROJECT}-jobs-dlq"
    main_name = f"{PROJECT}-jobs"

    def get_or_create(name: str, attributes: dict) -> str:
        try:
            return sqs.get_queue_url(QueueName=name)["QueueUrl"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in ("AWS.SimpleQueueService.NonExistentQueue",):
                raise
        response = sqs.create_queue(QueueName=name, Attributes=attributes)
        log(f"Created SQS queue: {name}")
        return response["QueueUrl"]

    dlq_url = get_or_create(dlq_name, {"MessageRetentionPeriod": "1209600"})  # 14 days
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])[
        "Attributes"
    ]["QueueArn"]

    redrive_policy = json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": 5})
    main_url = get_or_create(
        main_name,
        {
            "VisibilityTimeout": "310",  # slightly above the worker's own visibility timeout
            "MessageRetentionPeriod": "345600",  # 4 days
            "RedrivePolicy": redrive_policy,
        },
    )
    # Ensure redrive policy is applied even if the queue already existed
    sqs.set_queue_attributes(QueueUrl=main_url, Attributes={"RedrivePolicy": redrive_policy})

    log(f"SQS queues ready: {main_name} (DLQ: {dlq_name})")
    return {"queue_url": main_url, "dlq_url": dlq_url, "dlq_arn": dlq_arn}


# ---------------------------------------------------------------------------
# DynamoDB
# ---------------------------------------------------------------------------

def ensure_dynamodb_table(name: str, key_schema: list, attribute_definitions: list,
                           gsis: list = None, ttl_attribute: str = None) -> str:
    """Creates a DynamoDB table if it doesn't already exist. Returns the table ARN."""
    try:
        response = dynamodb.describe_table(TableName=name)
        arn = response["Table"]["TableArn"]
        log(f"DynamoDB table already exists: {name}")
        # Ensure TTL is still enabled if requested (idempotent)
        if ttl_attribute:
            try:
                dynamodb.update_time_to_live(
                    TableName=name,
                    TimeToLiveSpecification={"Enabled": True, "AttributeName": ttl_attribute},
                )
            except ClientError:
                pass  # TTL may already be set; ignore
        return arn
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceNotFoundException":
            raise

    kwargs = dict(
        TableName=name,
        KeySchema=key_schema,
        AttributeDefinitions=attribute_definitions,
        BillingMode="PAY_PER_REQUEST",
    )
    if gsis:
        kwargs["GlobalSecondaryIndexes"] = gsis

    dynamodb.create_table(**kwargs)
    log(f"Created DynamoDB table: {name} (waiting for ACTIVE status...)")

    waiter = dynamodb.get_waiter("table_exists")
    waiter.wait(TableName=name, WaiterConfig={"Delay": 5, "MaxAttempts": 24})
    log(f"DynamoDB table active: {name}")

    if ttl_attribute:
        dynamodb.update_time_to_live(
            TableName=name,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": ttl_attribute},
        )
        log(f"TTL enabled on {name}.{ttl_attribute}")

    arn = dynamodb.describe_table(TableName=name)["Table"]["TableArn"]
    return arn


def ensure_dynamodb_tables() -> dict:
    """
    Creates the three DynamoDB tables ClauseGuard needs.

    Table design:
      clauseguard-users
        PK: email (S)
        GSI: user_id-index  PK=user_id(S)   — allows lookup by UUID user_id

      clauseguard-contracts
        PK: user_id (S), SK: contract_id (S)
        GSI: contract_id-index  PK=contract_id(S)  — allows worker to fetch by contract_id alone

      clauseguard-tokens
        PK: token_hash (S)
        TTL: expires_at  — DynamoDB automatically removes expired tokens
    """
    users_arn = ensure_dynamodb_table(
        name=f"{PROJECT}-users",
        key_schema=[{"AttributeName": "email", "KeyType": "HASH"}],
        attribute_definitions=[
            {"AttributeName": "email", "AttributeType": "S"},
            {"AttributeName": "user_id", "AttributeType": "S"},
        ],
        gsis=[
            {
                "IndexName": "user_id-index",
                "KeySchema": [{"AttributeName": "user_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )

    contracts_arn = ensure_dynamodb_table(
        name=f"{PROJECT}-contracts",
        key_schema=[
            {"AttributeName": "user_id", "KeyType": "HASH"},
            {"AttributeName": "contract_id", "KeyType": "RANGE"},
        ],
        attribute_definitions=[
            {"AttributeName": "user_id", "AttributeType": "S"},
            {"AttributeName": "contract_id", "AttributeType": "S"},
        ],
        gsis=[
            {
                "IndexName": "contract_id-index",
                "KeySchema": [{"AttributeName": "contract_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )

    tokens_arn = ensure_dynamodb_table(
        name=f"{PROJECT}-tokens",
        key_schema=[{"AttributeName": "token_hash", "KeyType": "HASH"}],
        attribute_definitions=[
            {"AttributeName": "token_hash", "AttributeType": "S"},
        ],
        ttl_attribute="expires_at",
    )

    log("All DynamoDB tables ready")
    return {
        "dynamodb_users_arn": users_arn,
        "dynamodb_contracts_arn": contracts_arn,
        "dynamodb_tokens_arn": tokens_arn,
    }


# ---------------------------------------------------------------------------
# Networking (uses the default VPC to keep the project simple and free)
# ---------------------------------------------------------------------------

def get_default_vpc_and_subnets() -> dict:
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        raise RuntimeError(
            "No default VPC found in this account/region. Create a VPC manually and "
            "adjust provision.py's networking section with your subnet IDs."
        )
    vpc_id = vpcs[0]["VpcId"]

    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
    subnet_ids = [s["SubnetId"] for s in subnets]

    if len(subnet_ids) < 2:
        raise RuntimeError(
            "Default VPC has fewer than 2 subnets; an ALB requires at least 2 "
            "subnets in different Availability Zones."
        )

    log(f"Using default VPC {vpc_id} with subnets {subnet_ids}")
    return {"vpc_id": vpc_id, "subnet_ids": subnet_ids}


def ensure_security_group(vpc_id: str, name: str, description: str) -> str:
    existing = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": [name]}, {"Name": "vpc-id", "Values": [vpc_id]}]
    )["SecurityGroups"]
    if existing:
        log(f"Security group already exists: {name}")
        return existing[0]["GroupId"]

    response = ec2.create_security_group(GroupName=name, Description=description, VpcId=vpc_id)
    group_id = response["GroupId"]
    log(f"Created security group: {name} ({group_id})")
    return group_id


def authorize_ingress_if_missing(group_id: str, ip_permissions: list) -> None:
    try:
        ec2.authorize_security_group_ingress(GroupId=group_id, IpPermissions=ip_permissions)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "InvalidPermission.Duplicate":
            raise


def setup_networking() -> dict:
    net = get_default_vpc_and_subnets()
    vpc_id = net["vpc_id"]

    alb_sg = ensure_security_group(vpc_id, f"{PROJECT}-alb-sg", "ClauseGuard ALB security group")
    authorize_ingress_if_missing(
        alb_sg,
        [{"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}],
    )

    web_sg = ensure_security_group(vpc_id, f"{PROJECT}-web-sg", "ClauseGuard web ECS tasks")
    authorize_ingress_if_missing(
        web_sg,
        [{"IpProtocol": "tcp", "FromPort": 8080, "ToPort": 8080, "UserIdGroupPairs": [{"GroupId": alb_sg}]}],
    )

    worker_sg = ensure_security_group(vpc_id, f"{PROJECT}-worker-sg", "ClauseGuard worker ECS tasks")
    # Worker has no inbound requirements; it only makes outbound calls.

    net.update({"alb_sg": alb_sg, "web_sg": web_sg, "worker_sg": worker_sg})
    return net


# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------

TRUST_POLICY_ECS_TASKS = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
)


def ensure_role(role_name: str, trust_policy: str) -> str:
    try:
        role = iam.get_role(RoleName=role_name)
        log(f"IAM role already exists: {role_name}")
        return role["Role"]["Arn"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchEntity":
            raise

    role = iam.create_role(RoleName=role_name, AssumeRolePolicyDocument=trust_policy)
    log(f"Created IAM role: {role_name}")
    return role["Role"]["Arn"]


def attach_managed_policy_if_missing(role_name: str, policy_arn: str) -> None:
    attached = iam.list_attached_role_policies(RoleName=role_name)["AttachedPolicies"]
    if any(p["PolicyArn"] == policy_arn for p in attached):
        return
    iam.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)


def put_inline_policy(role_name: str, policy_name: str, policy_document: dict) -> None:
    iam.put_role_policy(
        RoleName=role_name, PolicyName=policy_name, PolicyDocument=json.dumps(policy_document)
    )


def ensure_iam_roles(bucket_name: str, queue_arn: str,
                     dynamodb_table_arns: list, account_id: str) -> dict:
    # --- Task execution role: pulls images from ECR, writes logs ---
    exec_role_name = f"{PROJECT}-ecs-task-execution-role"
    exec_role_arn = ensure_role(exec_role_name, TRUST_POLICY_ECS_TASKS)
    attach_managed_policy_if_missing(
        exec_role_name, "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
    )

    # DynamoDB wildcard ARNs for table + indexes
    dynamodb_resources = []
    for arn in dynamodb_table_arns:
        dynamodb_resources.append(arn)
        dynamodb_resources.append(f"{arn}/index/*")

    # --- Web task role: S3 read/write, SQS send, DynamoDB all tables ---
    web_role_name = f"{PROJECT}-web-task-role"
    web_role_arn = ensure_role(web_role_name, TRUST_POLICY_ECS_TASKS)
    put_inline_policy(
        web_role_name,
        "web-permissions",
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:PutObject", "s3:GetObject"],
                    "Resource": f"arn:aws:s3:::{bucket_name}/*",
                },
                {"Effect": "Allow", "Action": ["sqs:SendMessage"], "Resource": queue_arn},
                {
                    "Effect": "Allow",
                    "Action": [
                        "dynamodb:GetItem",
                        "dynamodb:PutItem",
                        "dynamodb:UpdateItem",
                        "dynamodb:DeleteItem",
                        "dynamodb:Query",
                        "dynamodb:Scan",
                    ],
                    "Resource": dynamodb_resources,
                },
            ],
        },
    )

    # --- Worker task role: S3 read/write, SQS consume, Textract, DynamoDB ---
    worker_role_name = f"{PROJECT}-worker-task-role"
    worker_role_arn = ensure_role(worker_role_name, TRUST_POLICY_ECS_TASKS)
    put_inline_policy(
        worker_role_name,
        "worker-permissions",
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:PutObject"],
                    "Resource": f"arn:aws:s3:::{bucket_name}/*",
                },
                {
                    "Effect": "Allow",
                    "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
                    "Resource": queue_arn,
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "textract:StartDocumentTextDetection",
                        "textract:GetDocumentTextDetection",
                    ],
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "dynamodb:GetItem",
                        "dynamodb:PutItem",
                        "dynamodb:UpdateItem",
                        "dynamodb:DeleteItem",
                        "dynamodb:Query",
                    ],
                    "Resource": dynamodb_resources,
                },
            ],
        },
    )

    # IAM role propagation can lag briefly after creation
    time.sleep(5)

    return {
        "exec_role_arn": exec_role_arn,
        "web_role_arn": web_role_arn,
        "worker_role_arn": worker_role_arn,
    }


# ---------------------------------------------------------------------------
# CloudWatch Logs
# ---------------------------------------------------------------------------

def ensure_log_group(name: str) -> None:
    try:
        logs.create_log_group(logGroupName=name)
        logs.put_retention_policy(logGroupName=name, retentionInDays=14)
        log(f"Created CloudWatch log group: {name}")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
        log(f"CloudWatch log group already exists: {name}")


# ---------------------------------------------------------------------------
# ALB
# ---------------------------------------------------------------------------

def ensure_alb(net: dict) -> dict:
    alb_name = f"{PROJECT}-alb"
    tg_name = f"{PROJECT}-web-tg"

    try:
        albs = elbv2.describe_load_balancers(Names=[alb_name])["LoadBalancers"]
        alb = albs[0]
        log(f"ALB already exists: {alb_name}")
    except ClientError as exc:
        if "LoadBalancerNotFound" not in str(exc):
            raise
        alb = elbv2.create_load_balancer(
            Name=alb_name,
            Subnets=net["subnet_ids"],
            SecurityGroups=[net["alb_sg"]],
            Scheme="internet-facing",
            Type="application",
            IpAddressType="ipv4",
        )["LoadBalancers"][0]
        log(f"Created ALB: {alb_name}")

    try:
        tgs = elbv2.describe_target_groups(Names=[tg_name])["TargetGroups"]
        target_group = tgs[0]
        log(f"Target group already exists: {tg_name}")
    except ClientError as exc:
        if "TargetGroupNotFound" not in str(exc):
            raise
        target_group = elbv2.create_target_group(
            Name=tg_name,
            Protocol="HTTP",
            Port=8080,
            VpcId=net["vpc_id"],
            TargetType="ip",
            HealthCheckPath="/healthz",
            HealthCheckIntervalSeconds=30,
            HealthyThresholdCount=2,
            UnhealthyThresholdCount=3,
        )["TargetGroups"][0]
        log(f"Created target group: {tg_name}")

    listeners = elbv2.describe_listeners(LoadBalancerArn=alb["LoadBalancerArn"])["Listeners"]
    if not listeners:
        elbv2.create_listener(
            LoadBalancerArn=alb["LoadBalancerArn"],
            Protocol="HTTP",
            Port=80,
            DefaultActions=[{"Type": "forward", "TargetGroupArn": target_group["TargetGroupArn"]}],
        )
        log("Created ALB listener on port 80")

    return {
        "alb_arn": alb["LoadBalancerArn"],
        "alb_dns_name": alb["DNSName"],
        "target_group_arn": target_group["TargetGroupArn"],
    }


# ---------------------------------------------------------------------------
# ECS
# ---------------------------------------------------------------------------

def ensure_ecs_cluster() -> str:
    cluster_name = f"{PROJECT}-cluster"
    existing = ecs.describe_clusters(clusters=[cluster_name])["clusters"]
    if existing and existing[0]["status"] == "ACTIVE":
        log(f"ECS cluster already exists: {cluster_name}")
        return existing[0]["clusterArn"]

    cluster = ecs.create_cluster(
        clusterName=cluster_name,
        capacityProviders=["FARGATE"],
        defaultCapacityProviderStrategy=[{"capacityProvider": "FARGATE", "weight": 1}],
    )
    log(f"Created ECS cluster: {cluster_name}")
    return cluster["cluster"]["clusterArn"]


def register_task_definition(
    family: str,
    image_placeholder: str,
    container_port: int,
    exec_role_arn: str,
    task_role_arn: str,
    log_group: str,
    environment: list,
) -> str:
    """
    Registers a task definition using a placeholder image tag. deploy.sh
    re-registers a new revision with the real image URI after each build.
    """
    container_def = {
        "name": family,
        "image": image_placeholder,
        "essential": True,
        "environment": environment,
        "secrets": [],
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": log_group,
                "awslogs-region": REGION,
                "awslogs-stream-prefix": family,
            },
        },
    }
    if container_port:
        container_def["portMappings"] = [{"containerPort": container_port, "protocol": "tcp"}]

    response = ecs.register_task_definition(
        family=family,
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu="512",
        memory="1024",
        executionRoleArn=exec_role_arn,
        taskRoleArn=task_role_arn,
        containerDefinitions=[container_def],
    )
    arn = response["taskDefinition"]["taskDefinitionArn"]
    log(f"Registered task definition: {arn}")
    return arn


def ensure_ecs_service(
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
            desiredCount=1,
        )
        log(f"Updated existing ECS service: {service_name}")
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
    log(f"Created ECS service: {service_name}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log("Starting ClauseGuard infrastructure provisioning...")
    state = load_state()

    account_id = get_account_id()
    log(f"Using AWS account: {account_id}, region: {REGION}")

    state["account_id"] = account_id
    state["region"] = REGION

    state["ecr_web_uri"] = ensure_ecr_repository(f"{PROJECT}-web")
    state["ecr_worker_uri"] = ensure_ecr_repository(f"{PROJECT}-worker")

    bucket_name = ensure_s3_bucket(account_id)
    state["s3_bucket"] = bucket_name

    sqs_info = ensure_sqs_queues()
    state.update(sqs_info)
    queue_arn = sqs.get_queue_attributes(QueueUrl=sqs_info["queue_url"], AttributeNames=["QueueArn"])[
        "Attributes"
    ]["QueueArn"]
    state["queue_arn"] = queue_arn

    # DynamoDB tables (replaces RDS)
    dynamodb_info = ensure_dynamodb_tables()
    state.update(dynamodb_info)
    dynamodb_table_arns = [
        dynamodb_info["dynamodb_users_arn"],
        dynamodb_info["dynamodb_contracts_arn"],
        dynamodb_info["dynamodb_tokens_arn"],
    ]

    net = setup_networking()
    state["network"] = net

    roles = ensure_iam_roles(bucket_name, queue_arn, dynamodb_table_arns, account_id)
    state.update(roles)

    ensure_log_group(f"/ecs/{PROJECT}-web")
    ensure_log_group(f"/ecs/{PROJECT}-worker")

    alb_info = ensure_alb(net)
    state.update(alb_info)

    cluster_arn = ensure_ecs_cluster()
    state["cluster_arn"] = cluster_arn

    # Shared environment — no DB credentials needed; DynamoDB uses IAM roles
    shared_env = [
        {"name": "AWS_REGION", "value": REGION},
        {"name": "S3_BUCKET_NAME", "value": bucket_name},
        {"name": "SQS_QUEUE_URL", "value": sqs_info["queue_url"]},
    ]

    web_env = shared_env + [
        {"name": "FLASK_SECRET_KEY", "value": os.environ.get("FLASK_SECRET_KEY", "")},
        {"name": "FLASK_ENV", "value": "production"},
    ]
    if not os.environ.get("FLASK_SECRET_KEY"):
        log(
            "WARNING: FLASK_SECRET_KEY was not set in your shell environment before running "
            "this script. A placeholder empty value was written — set a real random secret "
            "in the web task definition before going to production (see instructions.md)."
        )

    web_task_def_arn = register_task_definition(
        family=f"{PROJECT}-web",
        image_placeholder=f"{state['ecr_web_uri']}:latest",
        container_port=8080,
        exec_role_arn=roles["exec_role_arn"],
        task_role_arn=roles["web_role_arn"],
        log_group=f"/ecs/{PROJECT}-web",
        environment=web_env,
    )
    state["web_task_def_arn"] = web_task_def_arn

    worker_task_def_arn = register_task_definition(
        family=f"{PROJECT}-worker",
        image_placeholder=f"{state['ecr_worker_uri']}:latest",
        container_port=0,
        exec_role_arn=roles["exec_role_arn"],
        task_role_arn=roles["worker_role_arn"],
        log_group=f"/ecs/{PROJECT}-worker",
        environment=shared_env,
    )
    state["worker_task_def_arn"] = worker_task_def_arn

    save_state(state)
    log(
        "Task definitions registered with placeholder ':latest' images. "
        "Run infrastructure/deploy.sh to build, push, and deploy the real images "
        "before creating the ECS services."
    )
    log(f"State saved to {STATE_FILE}")
    log("Provisioning (pre-deploy) complete.")


if __name__ == "__main__":
    try:
        main()
    except ClientError as e:
        print(f"[provision] AWS API error: {e}", file=sys.stderr)
        sys.exit(1)
