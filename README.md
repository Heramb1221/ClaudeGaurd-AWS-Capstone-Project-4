# ClauseGuard — Automated Contract Risk Analysis Platform

> A containerized, AWS-native platform that reads a contract PDF, flags risky clauses in plain English, and produces a downloadable risk report — built as a university capstone project demonstrating containerized microservices on Amazon ECS.

---

## Project Description

ClauseGuard is a web application that lets a user upload a contract (PDF, including scanned documents) and receive an automated risk analysis: which clauses are risky, why, how severe each risk is, and an overall risk score for the document. Results are shown on a dashboard and available as a downloadable PDF report.

## Problem Statement

Small businesses, freelancers, and individuals sign contracts constantly — vendor agreements, freelance contracts, NDAs, leases — but rarely have a lawyer review every one. Risky clauses (auto-renewal traps, unlimited liability, one-sided indemnification, unfavorable jurisdiction) get missed until they cause real financial or legal harm, and professional legal review is expensive and slow for routine documents.

## Why I Built This

This project was built as an AWS capstone with a hard requirement to use Amazon ECR and ECS meaningfully rather than as a checkbox. Contract analysis is a natural fit: the API layer and the document-processing workload have genuinely different resource and scaling profiles, which justifies running them as two independently deployable containerized services on the same ECS cluster — a realistic microservices pattern, not a single toy container.

## Objectives

- Demonstrate a production-style containerized microservices architecture on AWS Fargate.
- Build a real, useful tool rather than a CRUD demo.
- Show an asynchronous processing pipeline (SQS-decoupled producer/consumer).
- Stay within AWS Free Tier as much as reasonably possible.
- Produce fully automated, idempotent infrastructure provisioning.

## Features

- Email/password authentication with PBKDF2-hashed passwords and HMAC-signed session tokens (server-side revocable).
- Upload a contract PDF (including scanned/photographed documents via OCR).
- Asynchronous processing pipeline: upload → S3 → SQS → worker → Textract → risk engine → RDS.
- Transparent, explainable rule-based risk engine covering 10 common contract-risk categories (auto-renewal, unlimited liability, one-sided indemnification, unilateral termination, broad non-compete, perpetual confidentiality, assignment without consent, unfavorable jurisdiction, late payment penalties, broad IP assignment).
- Per-clause risk explanations in plain English, not just a score.
- Downloadable PDF risk report (generated server-side, served via a time-limited presigned S3 URL).
- Dashboard with aggregate stats (contracts reviewed, high-risk count, average risk score) and full contract history.
- Contract status tracking (`PENDING` → `PROCESSING` → `PROCESSED`/`FAILED`) visible in real time on the detail page.

## Architecture

```
                        ┌─────────────────────────┐
                        │        Browser           │
                        └────────────┬─────────────┘
                                     │ HTTP (80)
                        ┌────────────▼─────────────┐
                        │  Application Load Balancer│
                        └────────────┬─────────────┘
                                     │
                     ┌───────────────▼────────────────┐
                     │   ECS Fargate — web service      │
                     │   (Flask + Jinja2, container)     │
                     └───────┬───────────────┬─────────┘
                             │               │
                    S3 (raw file)      SQS (job message)
                             │               │
                             │      ┌────────▼─────────┐
                             │      │ ECS Fargate —      │
                             │      │ worker service      │
                             │      │ (polls SQS)          │
                             │      └────┬─────────┬──────┘
                             │           │         │
                             │      Textract   RDS PostgreSQL
                             │     (OCR/text)  (contracts, clauses,
                             │           │         users, scores)
                             │      ┌────▼─────┐
                             └─────►│    S3     │◄── report PDF uploaded here
                                    └───────────┘

  IAM roles scope each service to only the permissions it needs.
  CloudWatch Logs capture stdout from both services.
  Secrets Manager holds the RDS master credentials (never in code or images).
```

**Workflow:**

1. User registers/logs in and uploads a contract PDF via the web service.
2. Web service stores the raw file in S3, creates a `PENDING` row in RDS, and publishes a job message to SQS.
3. The worker service (a separate ECS service, scaled independently) long-polls SQS, marks the contract `PROCESSING`, and calls Textract to extract text directly from the S3 object (async job, supports multi-page and scanned documents).
4. The worker runs the extracted text through the rule-based risk engine, which segments the text into clauses and matches each against 10 risk categories with severity weights.
5. The worker generates a PDF risk report, uploads it to S3, and writes all clause results plus the aggregate score to RDS in a single transaction, marking the contract `PROCESSED` (or `FAILED` with a message on error).
6. The user's dashboard and contract detail page reflect the final status, with the flagged clauses, explanations, and a report download link (presigned S3 URL).

## AWS Services Used

| Service | Purpose |
|---|---|
| **Amazon ECR** | Stores the `clauseguard-web` and `clauseguard-worker` container images (mandatory service). |
| **Amazon ECS (Fargate)** | Runs both services as independently scalable, serverless containers — no EC2 patching (mandatory service). |
| **Amazon RDS (PostgreSQL)** | Stores users, contracts, clauses, and auth tokens (mandatory relational database). |
| **Amazon S3** | Stores raw contract uploads and generated PDF reports. |
| **Amazon SQS** | Decouples the web service from the worker; includes a dead-letter queue for failed jobs. |
| **Amazon Textract** | Extracts text from PDFs, including scanned/photographed documents. |
| **AWS IAM** | Least-privilege roles scoped separately for the execution role, the web task role, and the worker task role. |
| **AWS Secrets Manager** | Stores the RDS master password; never hardcoded or baked into images. |
| **Amazon CloudWatch Logs** | Captures logs from both ECS services via the `awslogs` driver. |
| **Elastic Load Balancing (ALB)** | Publicly exposes the web service and performs health checks against `/healthz`. |

## Folder Structure

```
clauseguard/
├── web/                        # Flask web service (API + server-rendered frontend)
│   ├── app/
│   │   ├── routes/             # auth, contracts, dashboard blueprints
│   │   ├── services/           # db_service, s3_service, sqs_service
│   │   ├── templates/          # Jinja2 templates
│   │   ├── static/             # CSS, JS
│   │   ├── utils/              # auth_utils, validators, decorators, logger
│   │   ├── config.py
│   │   └── __init__.py         # application factory
│   ├── wsgi.py
│   ├── Dockerfile
│   ├── requirements.txt
│   └── .env.example
├── worker/                     # Background document-processing service
│   ├── worker/
│   │   ├── main.py             # SQS polling loop
│   │   ├── textract_service.py
│   │   ├── risk_engine.py      # rule-based clause risk scoring
│   │   ├── report_service.py   # PDF report generation
│   │   ├── db.py
│   │   ├── s3_service.py
│   │   ├── sqs_service.py
│   │   └── config.py
│   ├── Dockerfile
│   ├── requirements.txt
│   └── .env.example
├── infrastructure/
│   ├── provision.py            # idempotent AWS resource creation
│   ├── deploy.sh                # build, push, deploy images
│   ├── deploy_services.py       # registers task defs + creates/updates ECS services
│   ├── teardown.py              # deletes all provisioned resources
│   ├── sql/schema.sql           # RDS schema
│   └── requirements.txt
├── docker-compose.yml           # local PostgreSQL for development
├── .gitignore
├── README.md
└── instructions.md
```

## Technology Stack

- **Backend:** Python 3.12, Flask 3, gunicorn
- **Frontend:** Jinja2 templates, vanilla JavaScript, hand-written CSS (no framework)
- **Database:** Amazon RDS for PostgreSQL 16
- **Infrastructure automation:** Python + boto3
- **Containers:** Docker, Amazon ECS (Fargate), Amazon ECR
- **AWS SDK:** boto3

## Installation

See [instructions.md](./instructions.md) for the complete first-time setup walkthrough (AWS account setup, IAM, running locally, deploying). Quick summary:

```bash
git clone <this-repo>
cd clauseguard

# Local Postgres for development
docker compose up -d

# Web service
cd web
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in values
python wsgi.py

# Worker service (separate terminal)
cd worker
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in values
python -m worker.main
```

## Configuration

All configuration is via environment variables — see `web/.env.example` and `worker/.env.example`. No secrets are hardcoded anywhere in the codebase. In production, `DB_USER`/`DB_PASSWORD` are injected into the ECS task via Secrets Manager (see `infrastructure/provision.py`), not plain environment variables.

## Deployment

```bash
pip install -r infrastructure/requirements.txt
export FLASK_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")

python3 infrastructure/provision.py     # creates all AWS resources (idempotent)
./infrastructure/deploy.sh              # builds, pushes images, deploys ECS services
```

Full step-by-step deployment guide: [instructions.md](./instructions.md).

To tear everything down and stop incurring charges:

```bash
python3 infrastructure/teardown.py
```

## API Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/` | — | Redirects to `/dashboard` |
| GET / POST | `/register` | — | Create an account |
| GET / POST | `/login` | — | Log in |
| POST | `/logout` | ✔ | Revoke session and log out |
| GET | `/dashboard` | ✔ | Dashboard with stats + contract list |
| GET / POST | `/contracts/upload` | ✔ | Upload a contract PDF |
| GET | `/contracts/<id>` | ✔ | Contract detail, status, flagged clauses |
| GET | `/contracts/<id>/report` | ✔ | Redirects to a presigned S3 URL for the PDF report |
| GET | `/healthz` | — | ALB health check |

## Challenges Faced

- **Multi-page and scanned PDFs**: Textract's synchronous API only supports single-page documents, so the worker uses the asynchronous `start_document_text_detection` / `get_document_text_detection` job API with pagination, which required a polling loop with a bounded retry ceiling.
- **Async job visibility vs. correctness**: the SQS visibility timeout had to be set comfortably above the worst-case Textract processing time so a slow job isn't picked up twice by accident, while the dead-letter queue catches genuinely broken jobs after repeated failures.
- **S3 CORS configuration**: the boto3 method for setting bucket CORS rules is `put_bucket_cors`, not `put_bucket_cors_configuration` — a naming trap that's easy to get wrong and silently call an API that doesn't exist.
- **Two-service ECS design**: giving the worker its own task role, security group, and scaling story (rather than bundling it into the web container) took more upfront IAM/networking work but reflects how this would actually be built in production.

## Future Improvements

- Auto-scaling policies on the worker ECS service based on `ApproximateNumberOfMessagesVisible` in SQS.
- HTTPS via ACM + a custom domain on the ALB.
- Support for `.docx` contracts in addition to PDF.
- Per-organization accounts and team sharing of contract history.
- Replace polling-based Textract calls with an SNS completion notification to reduce worker idle polling.

## What I Learned

Building ClauseGuard reinforced how to design a genuine two-service containerized architecture on ECS Fargate — including the IAM, networking, and load-balancing plumbing that a single-container demo skips — and how to build a reliable async processing pipeline with SQS, including dead-letter handling and idempotent infrastructure-as-code with boto3.

## Contact

**Heramb Chaudhari**

[![GitHub](https://img.shields.io/badge/GitHub-Heramb1221-black?style=for-the-badge&logo=github)](https://github.com/Heramb1221)

[![LinkedIn](https://img.shields.io/badge/LinkedIn-Heramb%20Chaudhari-blue?style=for-the-badge&logo=linkedin)](https://www.linkedin.com/in/heramb-chaudhari)

[![Email](https://img.shields.io/badge/Email-hchaudhari1221%40gmail.com-red?style=for-the-badge&logo=gmail)](mailto:hchaudhari1221@gmail.com)
