CREATE TABLE repositories (
    id INTEGER PRIMARY KEY,
    location TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    default_ref TEXT,
    created_at INTEGER NOT NULL,
    last_scan_at INTEGER,
    last_scan_task_id INTEGER REFERENCES tasks(id),
    version INTEGER NOT NULL CHECK (version >= 1)
);

CREATE TABLE commits (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    sha BLOB NOT NULL CHECK (length(sha) IN (20, 32)),
    status SMALLINT NOT NULL CHECK (status IN (1, 2, 3)),
    is_merge INTEGER NOT NULL CHECK (is_merge IN (0, 1)),
    committed_at INTEGER,
    last_task_id INTEGER REFERENCES tasks(id),
    scan_run_id INTEGER REFERENCES tasks(id),
    processed_at INTEGER,
    error_code SMALLINT,
    UNIQUE(repository_id, sha)
);

CREATE INDEX commits_repository_status_id_idx
    ON commits(repository_id, status, id);

CREATE TABLE commit_parents (
    commit_id INTEGER NOT NULL REFERENCES commits(id),
    position SMALLINT NOT NULL CHECK (position >= 0),
    parent_sha BLOB NOT NULL CHECK (length(parent_sha) IN (20, 32)),
    PRIMARY KEY(commit_id, position)
);

CREATE TABLE tasks (
    id INTEGER PRIMARY KEY,
    kind SMALLINT NOT NULL CHECK (kind IN (1, 2, 3)),
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    status SMALLINT NOT NULL CHECK (status IN (1, 2, 3, 4, 5, 6)),
    payload TEXT NOT NULL,
    idempotency_key BLOB NOT NULL UNIQUE CHECK (length(idempotency_key) = 16),
    root_task_id INTEGER REFERENCES tasks(id),
    parent_task_id INTEGER REFERENCES tasks(id),
    priority SMALLINT NOT NULL,
    attempts SMALLINT NOT NULL CHECK (attempts >= 0),
    max_attempts SMALLINT NOT NULL CHECK (max_attempts > 0),
    not_before INTEGER,
    lease_owner TEXT,
    lease_expires_at INTEGER,
    error_code SMALLINT,
    error_message TEXT,
    error_retryable INTEGER CHECK (error_retryable IN (0, 1)),
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    finished_at INTEGER
);

CREATE INDEX tasks_queued_idx
    ON tasks(priority, not_before, id) WHERE status = 1;
CREATE INDEX tasks_root_task_id_idx ON tasks(root_task_id);
CREATE INDEX tasks_parent_task_id_idx ON tasks(parent_task_id);
CREATE UNIQUE INDEX tasks_active_scan_idx
    ON tasks(repository_id)
    WHERE kind = 1 AND status IN (1, 2, 3);
CREATE INDEX tasks_running_lease_idx
    ON tasks(lease_expires_at) WHERE status = 2;

CREATE TABLE task_results (
    task_id INTEGER PRIMARY KEY REFERENCES tasks(id),
    outcome SMALLINT NOT NULL CHECK (outcome IN (1, 2, 3)),
    output TEXT,
    error_code SMALLINT,
    error_message TEXT,
    error_retryable INTEGER CHECK (error_retryable IN (0, 1)),
    metrics TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE candidates (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    commit_id INTEGER NOT NULL REFERENCES commits(id),
    file_path TEXT NOT NULL,
    line_number INTEGER NOT NULL CHECK (line_number > 0),
    key_name TEXT,
    secret_type SMALLINT NOT NULL,
    value_masked TEXT NOT NULL,
    value_fingerprint BLOB NOT NULL CHECK (length(value_fingerprint) = 32),
    entropy REAL NOT NULL,
    regex_rules TEXT NOT NULL,
    detector_flags SMALLINT NOT NULL,
    source_line_masked TEXT NOT NULL,
    context_masked TEXT NOT NULL,
    is_test_context INTEGER NOT NULL CHECK (is_test_context IN (0, 1)),
    score_regex REAL NOT NULL,
    score_entropy REAL NOT NULL,
    score_context REAL NOT NULL,
    score_token_type REAL NOT NULL,
    score_total REAL NOT NULL CHECK (score_total >= 0.0 AND score_total <= 1.0),
    route SMALLINT NOT NULL CHECK (route IN (1, 2)),
    created_at INTEGER NOT NULL,
    UNIQUE(commit_id, file_path, line_number, value_fingerprint)
);

CREATE INDEX candidates_repository_fingerprint_idx
    ON candidates(repository_id, value_fingerprint);
CREATE INDEX candidates_route_idx ON candidates(route);

CREATE TABLE ai_verdicts (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL UNIQUE REFERENCES tasks(id),
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    is_secret INTEGER NOT NULL CHECK (is_secret IN (0, 1)),
    secret_type SMALLINT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    severity SMALLINT NOT NULL CHECK (severity IN (1, 2, 3, 4)),
    reason TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE INDEX ai_verdicts_candidate_id_idx ON ai_verdicts(candidate_id);

CREATE TABLE incidents (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    fingerprint BLOB NOT NULL CHECK (length(fingerprint) = 32),
    secret_type SMALLINT NOT NULL,
    severity SMALLINT NOT NULL CHECK (severity IN (1, 2, 3, 4)),
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    detector_flags SMALLINT NOT NULL,
    evidence_masked TEXT NOT NULL,
    reason TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    status SMALLINT NOT NULL CHECK (status IN (1, 2, 3, 4, 5)),
    still_in_head INTEGER CHECK (still_in_head IN (0, 1)),
    assignee TEXT,
    first_commit_id INTEGER NOT NULL REFERENCES commits(id),
    last_verdict_id INTEGER REFERENCES ai_verdicts(id),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    resolved_at INTEGER,
    version INTEGER NOT NULL CHECK (version >= 1),
    UNIQUE(repository_id, fingerprint)
);

CREATE INDEX incidents_repository_status_severity_idx
    ON incidents(repository_id, status, severity);

CREATE TABLE occurrences (
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    candidate_id INTEGER NOT NULL REFERENCES candidates(id),
    ai_task_id INTEGER REFERENCES tasks(id),
    seen_at INTEGER NOT NULL,
    PRIMARY KEY(incident_id, candidate_id)
);

CREATE TABLE incident_events (
    id INTEGER PRIMARY KEY,
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    kind SMALLINT NOT NULL CHECK (kind IN (1, 2, 3, 4, 5, 6)),
    note TEXT,
    actor TEXT,
    created_at INTEGER NOT NULL
);

CREATE INDEX incident_events_incident_id_id_idx
    ON incident_events(incident_id, id);

CREATE TABLE outbox_messages (
    id INTEGER PRIMARY KEY,
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    channel SMALLINT NOT NULL CHECK (channel IN (1, 2, 3)),
    payload TEXT NOT NULL,
    status SMALLINT NOT NULL CHECK (status IN (1, 2, 3)),
    attempts SMALLINT NOT NULL CHECK (attempts >= 0),
    not_before INTEGER,
    created_at INTEGER NOT NULL,
    sent_at INTEGER
);

CREATE INDEX outbox_pending_idx
    ON outbox_messages(not_before, id) WHERE status = 1;
