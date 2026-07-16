-- ClauseGuard database schema
-- Target: Amazon RDS for PostgreSQL
-- Run this once against the newly created RDS instance before starting the app.

CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    email           VARCHAR(255) NOT NULL UNIQUE,
    password_hash   VARCHAR(255) NOT NULL,
    password_salt   VARCHAR(64)  NOT NULL,
    full_name       VARCHAR(255) NOT NULL,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS contracts (
    id                  SERIAL PRIMARY KEY,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    original_filename   VARCHAR(512) NOT NULL,
    s3_key              VARCHAR(1024) NOT NULL,
    report_s3_key       VARCHAR(1024),
    status              VARCHAR(32) NOT NULL DEFAULT 'PENDING',
        -- PENDING -> PROCESSING -> PROCESSED | FAILED
    overall_risk_score  NUMERIC(5,2),
    risk_level          VARCHAR(16),
        -- LOW | MEDIUM | HIGH
    error_message       TEXT,
    created_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    processed_at        TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_contracts_user_id ON contracts(user_id);
CREATE INDEX IF NOT EXISTS idx_contracts_status ON contracts(status);

CREATE TABLE IF NOT EXISTS clauses (
    id              SERIAL PRIMARY KEY,
    contract_id     INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    clause_index    INTEGER NOT NULL,
    clause_text     TEXT NOT NULL,
    category        VARCHAR(64) NOT NULL,
        -- e.g. AUTO_RENEWAL, UNLIMITED_LIABILITY, INDEMNIFICATION, ...
    severity        VARCHAR(16) NOT NULL,
        -- LOW | MEDIUM | HIGH
    score           NUMERIC(5,2) NOT NULL,
    explanation     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_clauses_contract_id ON clauses(contract_id);

CREATE TABLE IF NOT EXISTS auth_tokens (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash      VARCHAR(255) NOT NULL UNIQUE,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMP NOT NULL,
    revoked         BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_auth_tokens_user_id ON auth_tokens(user_id);
