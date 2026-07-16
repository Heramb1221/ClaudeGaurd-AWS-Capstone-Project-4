# ClauseGuard — Step-by-Step Setup & Deployment Guide

This guide assumes you are deploying ClauseGuard for the first time and walks through every step in order. Follow it top to bottom.

---

## 1. Prerequisites

Install the following on your machine before starting:

| Tool | Minimum version | Check with |
|---|---|---|
| Python | 3.11+ | `python3 --version` |
| Docker | 24+ | `docker --version` |
| AWS CLI v2 | 2.13+ | `aws --version` |
| Git | any recent | `git --version` |

You also need:
- An AWS account with billing enabled (Free Tier eligible resources are used, but some AWS services outside Free Tier limits can incur small charges — see the cost notes in Step 8).
- An IAM user or role with sufficient permissions (Step 2).
- Docker Desktop (or the Docker daemon) running locally, since `deploy.sh` builds images on your machine.

---

## 2. AWS Account & Credentials Setup

1. Log in to the [AWS Console](https://console.aws.amazon.com/).
2. Go to **IAM → Users → Create user**. Name it something like `clauseguard-deployer`.
3. Attach these AWS-managed policies for this capstone project (broad policies are used here to keep setup simple for a course project; a real production deployment should scope these down further):
   - `AmazonEC2ContainerRegistryFullAccess`
   - `AmazonECS_FullAccess`
   - `AmazonRDSFullAccess`
   - `AmazonS3FullAccess`
   - `AmazonSQSFullAccess`
   - `AmazonTextractFullAccess`
   - `SecretsManagerReadWrite`
   - `CloudWatchLogsFullAccess`
   - `ElasticLoadBalancingFullAccess`
   - `IAMFullAccess` (needed because `provision.py` creates IAM roles)
   - `AmazonVPCFullAccess`
4. Create an access key for this user (**Security credentials → Create access key → Command Line Interface (CLI)**).
5. On your machine, configure the AWS CLI:
   ```bash
   aws configure
   # AWS Access Key ID: <paste>
   # AWS Secret Access Key: <paste>
   # Default region name: ap-south-1
   # Default output format: json
   ```
6. Verify:
   ```bash
   aws sts get-caller-identity
   ```
   This should print your AWS account ID and confirm the CLI is authenticated. **Do not share this output publicly** — it contains your account ID.

> **Manual step required:** You must create these credentials yourself; this project cannot generate or access AWS credentials on your behalf.

---

## 3. Clone and Inspect the Project

```bash
git clone <this-repository-url>
cd clauseguard
```

Review `web/.env.example` and `worker/.env.example` — these list every configuration value the app needs.

---

## 4. Running Locally (Before Deploying to AWS)

Running locally still uses **real AWS services** for S3, SQS, and Textract (there is no free local emulator for Textract), but uses a **local Docker Postgres** for the database so you don't need RDS running yet.

### 4.1 Start local PostgreSQL

```bash
docker compose up -d
```

This starts PostgreSQL on `localhost:5432` and automatically loads `infrastructure/sql/schema.sql`.

### 4.2 Create the S3 bucket and SQS queue for local testing

You can either:
- Run `python3 infrastructure/provision.py` now (Step 5) to create real S3/SQS resources, or
- Manually create an S3 bucket and SQS queue in the console for local testing, then update your `.env` files with those names.

The simplest path is to just run `infrastructure/provision.py` first (Step 5) — it's idempotent, so running it again later before the full AWS deploy is harmless.

### 4.3 Configure and run the web service

```bash
cd web
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `web/.env`:
- `FLASK_SECRET_KEY`: generate with `python3 -c "import secrets; print(secrets.token_hex(32))"`
- `S3_BUCKET_NAME`, `SQS_QUEUE_URL`: from `infrastructure/deployment_state.json` after running `provision.py`
- `DB_HOST=localhost`, `DB_PORT=5432`, `DB_NAME=clauseguard`, `DB_USER=clauseguard_app`, `DB_PASSWORD=localdevpassword` (matches `docker-compose.yml`)

Run it:
```bash
python wsgi.py
```
Visit `http://localhost:5000`.

### 4.4 Configure and run the worker service

In a separate terminal:
```bash
cd worker
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `worker/.env` with the same `S3_BUCKET_NAME`, `SQS_QUEUE_URL`, and local DB values as above.

Run it:
```bash
python -m worker.main
```

You should see `ClauseGuard worker starting...` in the logs. Upload a contract through the local web UI and watch the worker pick up the job.

---

## 5. AWS Infrastructure Provisioning

This creates every AWS resource ClauseGuard needs (ECR, S3, SQS, RDS, IAM, ALB, ECS cluster, CloudWatch log groups, and registers task definitions with placeholder images).

```bash
cd clauseguard
pip install -r infrastructure/requirements.txt
export FLASK_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")

python3 infrastructure/provision.py
```

This will take **5-10 minutes**, mostly waiting for the RDS instance to become available. It prints progress for every resource and is safe to re-run if it fails partway (it checks for existing resources before creating new ones).

At the end, `infrastructure/deployment_state.json` is created — this file is your source of truth for every resource ID created. **Do not commit this file to git** (it's already in `.gitignore`).

> **Manual/automatic note:** The RDS master password is auto-generated randomly and stored directly in AWS Secrets Manager — you never need to type or copy it manually, and it is never printed to your terminal or written to any file in the repo.

---

## 6. Building and Deploying the Application

```bash
./infrastructure/deploy.sh
```

This will:
1. Log Docker in to your ECR registry.
2. Build the `web` and `worker` Docker images.
3. Push both images to their ECR repositories.
4. Register new ECS task definition revisions pointing at the images you just pushed.
5. Create (first run) or update (subsequent runs) the `clauseguard-web-service` and `clauseguard-worker-service` ECS services.

At the end it prints your application URL, e.g.:
```
Web app URL: http://clauseguard-alb-123456789.ap-south-1.elb.amazonaws.com
```

Give the ALB **1-3 minutes** after the first deploy for health checks to pass before the site becomes reachable.

---

## 7. Verifying the Deployment

1. Open the printed ALB URL in your browser.
2. Register a new account, log in.
3. Upload a small PDF contract (a sample employment or vendor agreement works well for testing the risk categories).
4. Watch the contract detail page — status should move from `PENDING` → `PROCESSING` → `PROCESSED` within roughly 15-60 seconds depending on document length.
5. Confirm flagged clauses appear with explanations, and that **Download PDF report** works.

### Checking logs

```bash
aws logs tail /ecs/clauseguard-web --follow --region ap-south-1
aws logs tail /ecs/clauseguard-worker --follow --region ap-south-1
```

### Checking ECS service health

```bash
aws ecs describe-services \
  --cluster clauseguard-cluster \
  --services clauseguard-web-service clauseguard-worker-service \
  --region ap-south-1
```

---

## 8. Cost Notes / Free Tier

- **RDS db.t3.micro**: Free Tier includes 750 hours/month for 12 months — keep only one instance running.
- **ECS Fargate**: NOT covered by the AWS Free Tier. Each task (512 CPU / 1024 MB, ×2 services) costs a small hourly amount (a few cents/hour total for both services combined at these sizes in `ap-south-1`). Run `teardown.py` when you're done testing to avoid ongoing charges.
- **ALB**: Also not Free Tier eligible; a small hourly charge applies while it exists.
- **S3, SQS, Textract, Secrets Manager, CloudWatch Logs**: All have generous free tiers that a course project will not exceed under normal testing.

**If you only need this running for grading/demo purposes**, deploy it, demo it, then run `teardown.py` the same day.

---

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `provision.py` fails with `AccessDenied` | Your IAM user is missing a permission | Re-check the policy list in Step 2 |
| RDS creation times out | `db_instance_available` waiter exceeded 20 min | Check RDS console for the actual status; re-run `provision.py`, it will detect the existing instance |
| ALB health checks never pass | Web task crashing on startup | `aws logs tail /ecs/clauseguard-web --follow` — usually a missing/incorrect env var |
| Worker never processes a job | SQS queue URL mismatch between web and worker task defs | Confirm both point at the same `SQS_QUEUE_URL` in `deployment_state.json` |
| Contract stuck in `PROCESSING` forever | Worker crashed mid-job before writing failure state | Check worker logs; the SQS message will become visible again after the visibility timeout and retry automatically |
| `put_bucket_cors` error about unknown method | Using an old boto3 version | `pip install --upgrade boto3` — the correct method name is `put_bucket_cors`, not `put_bucket_cors_configuration` |
| `docker push` fails with 401 | ECR login token expired (they expire after 12 hours) | Re-run `deploy.sh`, which re-authenticates every time |
| Local web app can't reach Postgres | `docker compose up -d` not running, or wrong `DB_HOST` | `docker ps` to confirm the container is up; use `DB_HOST=localhost` locally |

---

## 10. Cleanup

When you're done with the project (after grading/demo), delete all AWS resources to stop any charges:

```bash
python3 infrastructure/teardown.py
```

Type `DELETE` when prompted to confirm. This deletes, in order: ECS services and cluster, the ALB and target group, the RDS instance (including all data — no snapshot is kept), security groups, IAM roles, the S3 bucket (and all objects/reports in it), SQS queues, the Secrets Manager secret, CloudWatch log groups, and (optionally) the ECR repositories.

To keep your ECR images (e.g., to redeploy later without rebuilding) but delete everything else:
```bash
python3 infrastructure/teardown.py --keep-images
```

Verify nothing billable is left:
```bash
aws rds describe-db-instances --region ap-south-1
aws ecs list-clusters --region ap-south-1
aws elbv2 describe-load-balancers --region ap-south-1
```
All three should return empty lists after teardown.
