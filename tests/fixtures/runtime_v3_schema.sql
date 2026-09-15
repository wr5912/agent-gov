-- 从精确历史提交的 Base.metadata 在真实 SQLite 上建表后导出，禁止用当前 ORM 重写。
-- source_commit: 71a4c7452cbf45d46ed94d824d53ad3dff24d79b
-- physical_schema_sha256: 6839624b48d7fccedfd8551295b3857df88e23bebc66aa4f72a1eaec3cf969ff
-- 来源仅有 schema，不包含运行数据、模型响应或私有配置。

CREATE TABLE agent_admission_states (
	agent_id VARCHAR(128) NOT NULL,
	generation INTEGER NOT NULL,
	maintenance_token VARCHAR(128),
	maintenance_generation INTEGER NOT NULL,
	maintenance_kind VARCHAR(64),
	maintenance_owner_id VARCHAR(256),
	maintenance_expires_at VARCHAR(64),
	created_at VARCHAR(64) NOT NULL,
	updated_at VARCHAR(64) NOT NULL,
	PRIMARY KEY (agent_id)
);

CREATE TABLE agent_change_set_events (
	event_id VARCHAR(128) NOT NULL,
	change_set_id VARCHAR(128) NOT NULL,
	action VARCHAR(64) NOT NULL,
	operator VARCHAR(128) NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	before_json JSON NOT NULL,
	after_json JSON NOT NULL,
	PRIMARY KEY (event_id),
	FOREIGN KEY(change_set_id) REFERENCES agent_change_sets (change_set_id) ON DELETE CASCADE
);

CREATE TABLE agent_change_sets (
	change_set_id VARCHAR(128) NOT NULL,
	agent_id VARCHAR(128) NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	updated_at VARCHAR(64) NOT NULL,
	status VARCHAR(64) NOT NULL,
	execution_job_id VARCHAR(128),
	base_commit_sha VARCHAR(64) NOT NULL,
	candidate_commit_sha VARCHAR(64),
	branch_name VARCHAR(256) NOT NULL,
	worktree_path VARCHAR(2048) NOT NULL,
	payload_json JSON NOT NULL,
	PRIMARY KEY (change_set_id)
);

CREATE TABLE agent_jobs (
	job_id VARCHAR(128) NOT NULL,
	job_type VARCHAR(64) NOT NULL,
	scope_kind VARCHAR(64) NOT NULL,
	scope_id VARCHAR(256) NOT NULL,
	status VARCHAR(64) NOT NULL,
	profile_name VARCHAR(128) NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	started_at VARCHAR(64),
	completed_at VARCHAR(64),
	input_path VARCHAR(2048) NOT NULL,
	raw_output_path VARCHAR(2048) NOT NULL,
	validated_output_path VARCHAR(2048) NOT NULL,
	error_path VARCHAR(2048) NOT NULL,
	runtime_version VARCHAR(64) NOT NULL,
	schema_version VARCHAR(64) NOT NULL,
	timeout_seconds INTEGER NOT NULL,
	retry_count INTEGER NOT NULL,
	profile_version_json JSON,
	input_json JSON,
	raw_output_json JSON,
	validated_output_json JSON,
	error_json JSON,
	PRIMARY KEY (job_id)
);

CREATE TABLE agent_registry (
	agent_id VARCHAR(128) NOT NULL,
	name VARCHAR(256) NOT NULL,
	category VARCHAR(32) NOT NULL,
	workspace_dir VARCHAR(2048) NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	status VARCHAR(32) NOT NULL,
	deleted_at VARCHAR(64),
	provision_state VARCHAR(32) NOT NULL,
	provision_token VARCHAR(64),
	provision_started_at VARCHAR(64),
	provision_previous_json JSON,
	PRIMARY KEY (agent_id)
);

CREATE TABLE agent_release_source_claims (
	agent_id VARCHAR(128) NOT NULL,
	source_improvement_id VARCHAR(128) NOT NULL,
	change_set_id VARCHAR(128) NOT NULL,
	release_id VARCHAR(128) NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	PRIMARY KEY (agent_id, source_improvement_id)
);

CREATE TABLE agent_release_tag_claims (
	agent_id VARCHAR(128) NOT NULL,
	tag_name VARCHAR(256) NOT NULL,
	change_set_id VARCHAR(128) NOT NULL,
	release_id VARCHAR(128) NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	PRIMARY KEY (agent_id, tag_name)
);

CREATE TABLE agent_releases (
	release_id VARCHAR(128) NOT NULL,
	agent_id VARCHAR(128) NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	updated_at VARCHAR(64) NOT NULL,
	status VARCHAR(64) NOT NULL,
	tag_name VARCHAR(256) NOT NULL,
	commit_sha VARCHAR(64) NOT NULL,
	change_set_id VARCHAR(128),
	rollback_of_release_id VARCHAR(128),
	archive_path VARCHAR(2048),
	payload_json JSON NOT NULL,
	PRIMARY KEY (release_id)
);

CREATE TABLE agent_runs (
	run_id VARCHAR(128) NOT NULL,
	session_id VARCHAR(128) NOT NULL,
	agent_id VARCHAR(128) NOT NULL,
	agent_version_id VARCHAR(256) NOT NULL,
	runtime_agent_id VARCHAR(128) NOT NULL,
	harness_digest VARCHAR(64) NOT NULL,
	client_operation_id VARCHAR(128),
	input_fingerprint VARCHAR(64),
	status VARCHAR(32) NOT NULL,
	reply_ids_json JSON NOT NULL,
	persisted_reply_ids_json JSON NOT NULL,
	persistence_batch_reply_ids_json JSON NOT NULL,
	team_generation INTEGER NOT NULL,
	root_persisted_team_generation INTEGER NOT NULL,
	pending_child_session_ids_json JSON NOT NULL,
	trace_id VARCHAR(64),
	trace_url VARCHAR(2048),
	trace_status VARCHAR(32) NOT NULL,
	terminal_reason VARCHAR(64),
	error_json JSON,
	alert_id VARCHAR(256),
	case_id VARCHAR(256),
	metadata_json JSON NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	started_at VARCHAR(64),
	updated_at VARCHAR(64) NOT NULL,
	completed_at VARCHAR(64),
	PRIMARY KEY (run_id)
);

CREATE TABLE agent_test_run_items (
	test_run_item_id VARCHAR(128) NOT NULL,
	test_run_id VARCHAR(128) NOT NULL,
	nodeid VARCHAR(2048) NOT NULL,
	outcome VARCHAR(32) NOT NULL,
	phase VARCHAR(32) NOT NULL,
	duration_seconds FLOAT,
	detail TEXT,
	PRIMARY KEY (test_run_item_id),
	FOREIGN KEY(test_run_id) REFERENCES agent_test_runs (test_run_id) ON DELETE CASCADE
);

CREATE TABLE agent_test_runs (
	test_run_id VARCHAR(128) NOT NULL,
	agent_id VARCHAR(128) NOT NULL,
	commit_sha VARCHAR(64) NOT NULL,
	change_set_id VARCHAR(128),
	schedule_id VARCHAR(128),
	scheduled_for VARCHAR(64),
	source VARCHAR(64) NOT NULL,
	status VARCHAR(32) NOT NULL,
	cancel_requested BOOLEAN NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	started_at VARCHAR(64),
	completed_at VARCHAR(64),
	suite_digest VARCHAR(64),
	command_json JSON NOT NULL,
	suite_json JSON NOT NULL,
	report_json JSON NOT NULL,
	stdout_text TEXT NOT NULL,
	stderr_text TEXT NOT NULL,
	error_json JSON NOT NULL,
	PRIMARY KEY (test_run_id)
);

CREATE TABLE agent_test_schedule_events (
	schedule_event_id VARCHAR(128) NOT NULL,
	schedule_id VARCHAR(128) NOT NULL,
	agent_id VARCHAR(128) NOT NULL,
	scheduled_for VARCHAR(64) NOT NULL,
	status VARCHAR(32) NOT NULL,
	resolved_commit_sha VARCHAR(64),
	test_run_id VARCHAR(128),
	detail_json JSON NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	completed_at VARCHAR(64),
	PRIMARY KEY (schedule_event_id),
	CONSTRAINT ux_agent_test_schedule_events_occurrence UNIQUE (schedule_id, scheduled_for),
	FOREIGN KEY(schedule_id) REFERENCES agent_test_schedules (schedule_id) ON DELETE CASCADE
);

CREATE TABLE agent_test_schedules (
	schedule_id VARCHAR(128) NOT NULL,
	agent_id VARCHAR(128) NOT NULL,
	enabled BOOLEAN NOT NULL,
	cron_expression VARCHAR(128) NOT NULL,
	timezone VARCHAR(128) NOT NULL,
	next_run_at VARCHAR(64),
	created_at VARCHAR(64) NOT NULL,
	updated_at VARCHAR(64) NOT NULL,
	PRIMARY KEY (schedule_id)
);

CREATE TABLE agent_workspace_import_records (
	import_id VARCHAR(128) NOT NULL,
	agent_id VARCHAR(128) NOT NULL,
	action VARCHAR(32) NOT NULL,
	status VARCHAR(32) NOT NULL,
	package_sha256 VARCHAR(64),
	tree_sha256 VARCHAR(64),
	commit_sha VARCHAR(64),
	created_at VARCHAR(64) NOT NULL,
	completed_at VARCHAR(64),
	suite_json JSON NOT NULL,
	warnings_json JSON NOT NULL,
	error_json JSON NOT NULL,
	PRIMARY KEY (import_id)
);

CREATE TABLE agent_worktree_cleanup_tasks (
	change_set_id VARCHAR(128) NOT NULL,
	agent_id VARCHAR(128) NOT NULL,
	status VARCHAR(32) NOT NULL,
	delete_branch BOOLEAN NOT NULL,
	attempt_count INTEGER NOT NULL,
	claim_token VARCHAR(128),
	claim_generation INTEGER NOT NULL,
	claim_expires_at VARCHAR(64),
	next_retry_at VARCHAR(64),
	last_error_json JSON NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	updated_at VARCHAR(64) NOT NULL,
	completed_at VARCHAR(64),
	PRIMARY KEY (change_set_id),
	FOREIGN KEY(change_set_id) REFERENCES agent_change_sets (change_set_id) ON DELETE CASCADE
);

CREATE TABLE attributions (
	attribution_id VARCHAR(128) NOT NULL,
	improvement_id VARCHAR(128) NOT NULL,
	summary TEXT NOT NULL,
	responsibility_boundary_json JSON NOT NULL,
	evidence_json JSON NOT NULL,
	counter_evidence_json JSON NOT NULL,
	uncertainty_factors_json JSON NOT NULL,
	verification_suggestions_json JSON NOT NULL,
	status VARCHAR(32) NOT NULL,
	generated_by VARCHAR(32) NOT NULL,
	generation_trace_id VARCHAR(256) NOT NULL,
	generation_trace_url VARCHAR(2048) NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	updated_at VARCHAR(64) NOT NULL,
	PRIMARY KEY (attribution_id)
);

CREATE TABLE evidence_files (
	evidence_package_id VARCHAR(128) NOT NULL,
	file_name VARCHAR(256) NOT NULL,
	file_type VARCHAR(128) NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	content_json JSON NOT NULL,
	PRIMARY KEY (evidence_package_id, file_name),
	FOREIGN KEY(evidence_package_id) REFERENCES evidence_packages (evidence_package_id) ON DELETE CASCADE
);

CREATE TABLE evidence_packages (
	evidence_package_id VARCHAR(128) NOT NULL,
	feedback_case_id VARCHAR(128) NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	manifest_json JSON NOT NULL,
	PRIMARY KEY (evidence_package_id),
	FOREIGN KEY(feedback_case_id) REFERENCES feedback_cases (feedback_case_id)
);

CREATE TABLE execution_records (
	execution_id VARCHAR(128) NOT NULL,
	improvement_id VARCHAR(128) NOT NULL,
	summary TEXT NOT NULL,
	changes_applied_json JSON NOT NULL,
	agent_version VARCHAR(128) NOT NULL,
	risk_level VARCHAR(32) NOT NULL,
	rollback_strategy TEXT NOT NULL,
	rollback_instructions_json JSON NOT NULL,
	status VARCHAR(32) NOT NULL,
	generated_by VARCHAR(32) NOT NULL,
	change_set_id VARCHAR(128) NOT NULL,
	applied_agent_version_id VARCHAR(128) NOT NULL,
	applied_diff_json JSON NOT NULL,
	generation_trace_id VARCHAR(256) NOT NULL,
	generation_trace_url VARCHAR(2048) NOT NULL,
	base_commit_sha VARCHAR(64) NOT NULL,
	source_optimization_plan_id VARCHAR(128) NOT NULL,
	source_optimization_plan_updated_at VARCHAR(64) NOT NULL,
	source_attribution_id VARCHAR(128) NOT NULL,
	source_attribution_updated_at VARCHAR(64) NOT NULL,
	claim_token VARCHAR(128) NOT NULL,
	claim_generation INTEGER NOT NULL,
	claim_expires_at VARCHAR(64) NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	updated_at VARCHAR(64) NOT NULL,
	PRIMARY KEY (execution_id)
);

CREATE TABLE feedback_case_sources (
	source_kind VARCHAR(32) NOT NULL,
	source_id VARCHAR(128) NOT NULL,
	case_id VARCHAR(128) NOT NULL,
	agent_id VARCHAR(128) NOT NULL,
	is_direct BOOLEAN NOT NULL,
	direct_position INTEGER,
	created_at VARCHAR(64) NOT NULL,
	PRIMARY KEY (source_kind, source_id),
	FOREIGN KEY(case_id) REFERENCES feedback_cases (feedback_case_id) ON DELETE CASCADE
);

CREATE TABLE feedback_cases (
	feedback_case_id VARCHAR(128) NOT NULL,
	agent_id VARCHAR(128) NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	updated_at VARCHAR(64) NOT NULL,
	status VARCHAR(64) NOT NULL,
	title VARCHAR(512) NOT NULL,
	priority VARCHAR(32) NOT NULL,
	current_evidence_package_id VARCHAR(128),
	current_attribution_job_id VARCHAR(128),
	source_ids_json JSON NOT NULL,
	signal_ids_json JSON NOT NULL,
	event_ids_json JSON NOT NULL,
	pending_correlation_ids_json JSON NOT NULL,
	run_ids_json JSON NOT NULL,
	session_ids_json JSON NOT NULL,
	alert_ids_json JSON NOT NULL,
	case_ids_json JSON NOT NULL,
	PRIMARY KEY (feedback_case_id)
);

CREATE TABLE feedback_signals (
	signal_id VARCHAR(128) NOT NULL,
	source_type VARCHAR(64) NOT NULL,
	agent_id VARCHAR(128),
	run_id VARCHAR(128),
	matched_run_id VARCHAR(128),
	session_id VARCHAR(128),
	alert_id VARCHAR(256),
	case_id VARCHAR(256),
	created_at VARCHAR(64) NOT NULL,
	payload_json JSON NOT NULL,
	PRIMARY KEY (signal_id)
);

CREATE TABLE feedback_source_annotations (
	annotation_id VARCHAR(256) NOT NULL,
	source_kind VARCHAR(64) NOT NULL,
	source_id VARCHAR(128) NOT NULL,
	status VARCHAR(64) NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	updated_at VARCHAR(64) NOT NULL,
	payload_json JSON NOT NULL,
	PRIMARY KEY (annotation_id)
);

CREATE TABLE governance_assets (
	asset_id VARCHAR(128) NOT NULL,
	agent_id VARCHAR(128) NOT NULL,
	asset_type VARCHAR(32) NOT NULL,
	title VARCHAR(512) NOT NULL,
	body TEXT NOT NULL,
	source_improvement_id VARCHAR(128) NOT NULL,
	inherited_from VARCHAR(128) NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	updated_at VARCHAR(64) NOT NULL,
	PRIMARY KEY (asset_id)
);

CREATE TABLE improvement_feedback_case_assignments (
	feedback_case_id VARCHAR(128) NOT NULL,
	improvement_id VARCHAR(128) NOT NULL,
	feedback_id VARCHAR(128) NOT NULL,
	agent_id VARCHAR(128) NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	PRIMARY KEY (feedback_case_id)
);

CREATE TABLE improvement_feedbacks (
	feedback_id VARCHAR(128) NOT NULL,
	improvement_id VARCHAR(128) NOT NULL,
	agent_id VARCHAR(128) NOT NULL,
	summary VARCHAR(1024) NOT NULL,
	source VARCHAR(64) NOT NULL,
	status VARCHAR(32) NOT NULL,
	raw_text TEXT NOT NULL,
	run_id VARCHAR(128) NOT NULL,
	session_id VARCHAR(128) NOT NULL,
	agent_version_id VARCHAR(256) NOT NULL,
	scenario VARCHAR(256) NOT NULL,
	task_id VARCHAR(256) NOT NULL,
	alert_id VARCHAR(256) NOT NULL,
	case_id VARCHAR(256) NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	PRIMARY KEY (feedback_id)
);

CREATE TABLE improvement_items (
	improvement_id VARCHAR(128) NOT NULL,
	agent_id VARCHAR(128) NOT NULL,
	title VARCHAR(512) NOT NULL,
	summary VARCHAR(4096) NOT NULL,
	improvement_stage VARCHAR(64) NOT NULL,
	improvement_status VARCHAR(32) NOT NULL,
	source_feedback_refs_json JSON NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	updated_at VARCHAR(64) NOT NULL,
	PRIMARY KEY (improvement_id)
);

CREATE TABLE improvement_links (
	link_id VARCHAR(128) NOT NULL,
	improvement_id VARCHAR(128) NOT NULL,
	kind VARCHAR(32) NOT NULL,
	ref_id VARCHAR(256) NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	PRIMARY KEY (link_id)
);

CREATE TABLE normalized_feedbacks (
	normalized_feedback_id VARCHAR(128) NOT NULL,
	improvement_id VARCHAR(128) NOT NULL,
	problem VARCHAR(1024) NOT NULL,
	possible_reason VARCHAR(1024) NOT NULL,
	possible_object VARCHAR(512) NOT NULL,
	impact VARCHAR(128) NOT NULL,
	suggestion VARCHAR(1024) NOT NULL,
	user_quote TEXT NOT NULL,
	status VARCHAR(32) NOT NULL,
	generated_by VARCHAR(32) NOT NULL,
	generation_trace_id VARCHAR(256) NOT NULL,
	generation_trace_url VARCHAR(2048) NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	updated_at VARCHAR(64) NOT NULL,
	PRIMARY KEY (normalized_feedback_id)
);

CREATE TABLE optimization_plans (
	optimization_plan_id VARCHAR(128) NOT NULL,
	improvement_id VARCHAR(128) NOT NULL,
	summary TEXT NOT NULL,
	changes_json JSON NOT NULL,
	risk_level VARCHAR(32) NOT NULL,
	status VARCHAR(32) NOT NULL,
	generated_by VARCHAR(32) NOT NULL,
	generation_trace_id VARCHAR(256) NOT NULL,
	generation_trace_url VARCHAR(2048) NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	updated_at VARCHAR(64) NOT NULL,
	PRIMARY KEY (optimization_plan_id)
);

CREATE TABLE pending_correlations (
	pending_id VARCHAR(128) NOT NULL,
	event_id VARCHAR(128) NOT NULL,
	status VARCHAR(64) NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	updated_at VARCHAR(64) NOT NULL,
	payload_json JSON NOT NULL,
	PRIMARY KEY (pending_id)
);

CREATE TABLE regression_test_designs (
	regression_test_design_id VARCHAR(128) NOT NULL,
	improvement_id VARCHAR(128) NOT NULL,
	summary TEXT NOT NULL,
	tests_json JSON NOT NULL,
	no_action_reason TEXT NOT NULL,
	status VARCHAR(32) NOT NULL,
	generated_by VARCHAR(32) NOT NULL,
	generation_trace_id VARCHAR(256) NOT NULL,
	generation_trace_url VARCHAR(2048) NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	updated_at VARCHAR(64) NOT NULL,
	PRIMARY KEY (regression_test_design_id)
);

CREATE TABLE runtime_agent_deletion_intents (
	intent_id VARCHAR(128) NOT NULL,
	agent_id VARCHAR(128) NOT NULL,
	agent_generation VARCHAR(64) NOT NULL,
	workspace_dir VARCHAR(2048) NOT NULL,
	versions_json JSON NOT NULL,
	sessions_json JSON NOT NULL,
	enumerated_runtime_agent_ids_json JSON NOT NULL,
	deleted_session_ids_json JSON NOT NULL,
	deleted_runtime_agent_ids_json JSON NOT NULL,
	removed_snapshot_ids_json JSON NOT NULL,
	tombstoned BOOLEAN NOT NULL,
	workspace_removed BOOLEAN NOT NULL,
	status VARCHAR(32) NOT NULL,
	attempts INTEGER NOT NULL,
	error_json JSON,
	created_at VARCHAR(64) NOT NULL,
	updated_at VARCHAR(64) NOT NULL,
	completed_at VARCHAR(64),
	PRIMARY KEY (intent_id)
);

CREATE TABLE runtime_agent_versions (
	agent_id VARCHAR(128) NOT NULL,
	agent_version_id VARCHAR(256) NOT NULL,
	harness_digest VARCHAR(64) NOT NULL,
	runtime_agent_id VARCHAR(128) NOT NULL,
	governance_agent_id VARCHAR(128) NOT NULL,
	source_kind VARCHAR(32) NOT NULL,
	source_id VARCHAR(128),
	created_at VARCHAR(64) NOT NULL,
	PRIMARY KEY (agent_id, agent_version_id, harness_digest)
);

CREATE TABLE runtime_chat_operations (
	operation_key VARCHAR(384) NOT NULL,
	client_operation_id VARCHAR(128) NOT NULL,
	operation_kind VARCHAR(32) NOT NULL,
	request_fingerprint VARCHAR(64) NOT NULL,
	run_id VARCHAR(128) NOT NULL,
	root_session_id VARCHAR(128) NOT NULL,
	action_session_id VARCHAR(128) NOT NULL,
	runtime_agent_id VARCHAR(128) NOT NULL,
	reply_id VARCHAR(128),
	action_ids_json JSON NOT NULL,
	tool_call_ids_json JSON NOT NULL,
	confirmation_scope VARCHAR(32),
	response_status INTEGER,
	response_body BLOB,
	response_content_type VARCHAR(256),
	response_headers_json JSON,
	created_at VARCHAR(64) NOT NULL,
	updated_at VARCHAR(64) NOT NULL,
	PRIMARY KEY (operation_key),
	FOREIGN KEY(run_id) REFERENCES agent_runs (run_id) ON DELETE CASCADE
);

CREATE TABLE runtime_cutover_ledger (
	cutover_id VARCHAR(128) NOT NULL,
	phase VARCHAR(64) NOT NULL,
	status VARCHAR(32) NOT NULL,
	detail TEXT NOT NULL,
	artifacts_json JSON NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	PRIMARY KEY (cutover_id)
);

CREATE TABLE runtime_ephemeral_resources (
	cache_key VARCHAR(256) NOT NULL,
	business_agent_id VARCHAR(128) NOT NULL,
	version_owner_id VARCHAR(128) NOT NULL,
	agent_version_id VARCHAR(256) NOT NULL,
	harness_digest VARCHAR(64) NOT NULL,
	source_id VARCHAR(128) NOT NULL,
	source_kind VARCHAR(32) NOT NULL,
	runtime_agent_id VARCHAR(128),
	session_id VARCHAR(128),
	workspace_id VARCHAR(384) NOT NULL,
	status VARCHAR(32) NOT NULL,
	error_json JSON,
	created_at VARCHAR(64) NOT NULL,
	updated_at VARCHAR(64) NOT NULL,
	completed_at VARCHAR(64),
	PRIMARY KEY (cache_key)
);

CREATE TABLE runtime_pending_actions (
	action_id VARCHAR(384) NOT NULL,
	session_id VARCHAR(128) NOT NULL,
	run_id VARCHAR(128) NOT NULL,
	reply_id VARCHAR(128) NOT NULL,
	tool_call_id VARCHAR(128) NOT NULL,
	kind VARCHAR(32) NOT NULL,
	tool_call_name VARCHAR(256) NOT NULL,
	tool_call_json JSON NOT NULL,
	status VARCHAR(32) NOT NULL,
	run_rules_json JSON NOT NULL,
	run_rules_granted_at VARCHAR(64),
	run_rules_expired_at VARCHAR(64),
	created_at VARCHAR(64) NOT NULL,
	updated_at VARCHAR(64) NOT NULL,
	PRIMARY KEY (action_id)
);

CREATE TABLE runtime_receipts (
	receipt_id VARCHAR(128) NOT NULL,
	event_id VARCHAR(128) NOT NULL,
	run_id VARCHAR(128) NOT NULL,
	session_id VARCHAR(128) NOT NULL,
	reply_id VARCHAR(128),
	event_type VARCHAR(64) NOT NULL,
	payload_json JSON NOT NULL,
	received_at VARCHAR(64) NOT NULL,
	PRIMARY KEY (receipt_id)
);

CREATE TABLE runtime_session_bindings (
	session_id VARCHAR(128) NOT NULL,
	agent_id VARCHAR(128) NOT NULL,
	agent_version_id VARCHAR(256) NOT NULL,
	runtime_agent_id VARCHAR(128) NOT NULL,
	harness_digest VARCHAR(64) NOT NULL,
	root_session_id VARCHAR(128) NOT NULL,
	team_id VARCHAR(128),
	active_run_id VARCHAR(128),
	active_team_generation INTEGER NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	updated_at VARCHAR(64) NOT NULL,
	PRIMARY KEY (session_id)
);

CREATE TABLE runtime_session_creation_intents (
	intent_id VARCHAR(128) NOT NULL,
	idempotency_key VARCHAR(256),
	agent_id VARCHAR(128) NOT NULL,
	agent_version_id VARCHAR(256) NOT NULL,
	runtime_agent_id VARCHAR(128) NOT NULL,
	harness_digest VARCHAR(64) NOT NULL,
	workspace_id VARCHAR(320) NOT NULL,
	request_fingerprint VARCHAR(64) NOT NULL,
	session_id VARCHAR(128),
	status VARCHAR(32) NOT NULL,
	error_json JSON,
	cleanup_attempts INTEGER NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	updated_at VARCHAR(64) NOT NULL,
	completed_at VARCHAR(64),
	PRIMARY KEY (intent_id)
);

CREATE TABLE runtime_team_deliveries (
	event_id VARCHAR(128) NOT NULL,
	run_id VARCHAR(128) NOT NULL,
	source_session_id VARCHAR(128) NOT NULL,
	target_session_id VARCHAR(128) NOT NULL,
	generation INTEGER NOT NULL,
	created_at VARCHAR(64) NOT NULL,
	PRIMARY KEY (event_id)
);

CREATE TABLE schema_migrations (
	version VARCHAR(64) NOT NULL,
	applied_at VARCHAR(64) NOT NULL,
	PRIMARY KEY (version)
);

CREATE TABLE soc_events (
	event_id VARCHAR(128) NOT NULL,
	event_type VARCHAR(128) NOT NULL,
	source_system VARCHAR(128) NOT NULL,
	agent_id VARCHAR(128),
	run_id VARCHAR(128),
	matched_run_id VARCHAR(128),
	session_id VARCHAR(128),
	alert_id VARCHAR(256),
	case_id VARCHAR(256),
	created_at VARCHAR(64) NOT NULL,
	payload_json JSON NOT NULL,
	PRIMARY KEY (event_id)
);

CREATE INDEX ix_agent_admission_states_created_at ON agent_admission_states (created_at);

CREATE INDEX ix_agent_admission_states_maintenance_expires_at ON agent_admission_states (maintenance_expires_at);

CREATE INDEX ix_agent_admission_states_maintenance_kind ON agent_admission_states (maintenance_kind);

CREATE INDEX ix_agent_admission_states_maintenance_owner_id ON agent_admission_states (maintenance_owner_id);

CREATE INDEX ix_agent_admission_states_maintenance_token ON agent_admission_states (maintenance_token);

CREATE INDEX ix_agent_admission_states_updated_at ON agent_admission_states (updated_at);

CREATE INDEX ix_agent_change_set_events_action ON agent_change_set_events (action);

CREATE INDEX ix_agent_change_set_events_change_created ON agent_change_set_events (change_set_id, created_at);

CREATE INDEX ix_agent_change_set_events_change_set_id ON agent_change_set_events (change_set_id);

CREATE INDEX ix_agent_change_set_events_created_at ON agent_change_set_events (created_at);

CREATE INDEX ix_agent_change_set_events_operator ON agent_change_set_events (operator);

CREATE INDEX ix_agent_change_sets_agent_id ON agent_change_sets (agent_id);

CREATE INDEX ix_agent_change_sets_base_commit_sha ON agent_change_sets (base_commit_sha);

CREATE INDEX ix_agent_change_sets_branch_name ON agent_change_sets (branch_name);

CREATE INDEX ix_agent_change_sets_candidate_commit_sha ON agent_change_sets (candidate_commit_sha);

CREATE INDEX ix_agent_change_sets_created_at ON agent_change_sets (created_at);

CREATE INDEX ix_agent_change_sets_execution_job_id ON agent_change_sets (execution_job_id);

CREATE INDEX ix_agent_change_sets_status ON agent_change_sets (status);

CREATE INDEX ix_agent_change_sets_status_updated ON agent_change_sets (status, updated_at);

CREATE INDEX ix_agent_change_sets_updated_at ON agent_change_sets (updated_at);

CREATE INDEX ix_agent_jobs_created_at ON agent_jobs (created_at);

CREATE INDEX ix_agent_jobs_job_type ON agent_jobs (job_type);

CREATE INDEX ix_agent_jobs_profile_name ON agent_jobs (profile_name);

CREATE INDEX ix_agent_jobs_scope_id ON agent_jobs (scope_id);

CREATE INDEX ix_agent_jobs_scope_kind ON agent_jobs (scope_kind);

CREATE INDEX ix_agent_jobs_scope_type_created ON agent_jobs (scope_kind, scope_id, job_type, created_at);

CREATE INDEX ix_agent_jobs_status ON agent_jobs (status);

CREATE INDEX ix_agent_jobs_type_status_created ON agent_jobs (job_type, status, created_at);

CREATE INDEX ix_agent_registry_category ON agent_registry (category);

CREATE INDEX ix_agent_registry_created_at ON agent_registry (created_at);

CREATE INDEX ix_agent_registry_provision_state ON agent_registry (provision_state);

CREATE INDEX ix_agent_registry_status ON agent_registry (status);

CREATE INDEX ix_agent_release_source_claims_change_set_id ON agent_release_source_claims (change_set_id);

CREATE INDEX ix_agent_release_source_claims_created_at ON agent_release_source_claims (created_at);

CREATE INDEX ix_agent_release_source_claims_release_id ON agent_release_source_claims (release_id);

CREATE INDEX ix_agent_release_tag_claims_change_set_id ON agent_release_tag_claims (change_set_id);

CREATE INDEX ix_agent_release_tag_claims_created_at ON agent_release_tag_claims (created_at);

CREATE INDEX ix_agent_release_tag_claims_release_id ON agent_release_tag_claims (release_id);

CREATE INDEX ix_agent_releases_agent_id ON agent_releases (agent_id);

CREATE INDEX ix_agent_releases_change_set_id ON agent_releases (change_set_id);

CREATE INDEX ix_agent_releases_commit_sha ON agent_releases (commit_sha);

CREATE INDEX ix_agent_releases_created_at ON agent_releases (created_at);

CREATE INDEX ix_agent_releases_rollback_of_release_id ON agent_releases (rollback_of_release_id);

CREATE INDEX ix_agent_releases_status ON agent_releases (status);

CREATE INDEX ix_agent_releases_status_created ON agent_releases (status, created_at);

CREATE INDEX ix_agent_releases_tag_name ON agent_releases (tag_name);

CREATE INDEX ix_agent_releases_updated_at ON agent_releases (updated_at);

CREATE INDEX ix_agent_runs_agent_id ON agent_runs (agent_id);

CREATE INDEX ix_agent_runs_agent_version_id ON agent_runs (agent_version_id);

CREATE INDEX ix_agent_runs_alert_id ON agent_runs (alert_id);

CREATE INDEX ix_agent_runs_case_id ON agent_runs (case_id);

CREATE INDEX ix_agent_runs_created_at ON agent_runs (created_at);

CREATE INDEX ix_agent_runs_runtime_agent_id ON agent_runs (runtime_agent_id);

CREATE INDEX ix_agent_runs_session_id ON agent_runs (session_id);

CREATE INDEX ix_agent_runs_status ON agent_runs (status);

CREATE UNIQUE INDEX ix_agent_runs_trace_id ON agent_runs (trace_id);

CREATE INDEX ix_agent_runs_trace_status ON agent_runs (trace_status);

CREATE INDEX ix_agent_runs_updated_at ON agent_runs (updated_at);

CREATE INDEX ix_agent_test_run_items_outcome ON agent_test_run_items (outcome);

CREATE INDEX ix_agent_test_run_items_test_run_id ON agent_test_run_items (test_run_id);

CREATE INDEX ix_agent_test_runs_agent_created ON agent_test_runs (agent_id, created_at);

CREATE INDEX ix_agent_test_runs_agent_id ON agent_test_runs (agent_id);

CREATE INDEX ix_agent_test_runs_change_commit ON agent_test_runs (change_set_id, commit_sha);

CREATE INDEX ix_agent_test_runs_change_set_id ON agent_test_runs (change_set_id);

CREATE INDEX ix_agent_test_runs_commit_sha ON agent_test_runs (commit_sha);

CREATE INDEX ix_agent_test_runs_created_at ON agent_test_runs (created_at);

CREATE INDEX ix_agent_test_runs_schedule_id ON agent_test_runs (schedule_id);

CREATE INDEX ix_agent_test_runs_schedule_occurrence ON agent_test_runs (schedule_id, scheduled_for);

CREATE INDEX ix_agent_test_runs_scheduled_for ON agent_test_runs (scheduled_for);

CREATE INDEX ix_agent_test_runs_source ON agent_test_runs (source);

CREATE INDEX ix_agent_test_runs_status ON agent_test_runs (status);

CREATE INDEX ix_agent_test_schedule_events_agent_created ON agent_test_schedule_events (agent_id, created_at);

CREATE INDEX ix_agent_test_schedule_events_agent_id ON agent_test_schedule_events (agent_id);

CREATE INDEX ix_agent_test_schedule_events_created_at ON agent_test_schedule_events (created_at);

CREATE INDEX ix_agent_test_schedule_events_resolved_commit_sha ON agent_test_schedule_events (resolved_commit_sha);

CREATE INDEX ix_agent_test_schedule_events_schedule_id ON agent_test_schedule_events (schedule_id);

CREATE INDEX ix_agent_test_schedule_events_scheduled_for ON agent_test_schedule_events (scheduled_for);

CREATE INDEX ix_agent_test_schedule_events_status ON agent_test_schedule_events (status);

CREATE INDEX ix_agent_test_schedule_events_test_run_id ON agent_test_schedule_events (test_run_id);

CREATE UNIQUE INDEX ix_agent_test_schedules_agent_id ON agent_test_schedules (agent_id);

CREATE INDEX ix_agent_test_schedules_enabled ON agent_test_schedules (enabled);

CREATE INDEX ix_agent_test_schedules_next_run_at ON agent_test_schedules (next_run_at);

CREATE INDEX ix_agent_workspace_import_records_action ON agent_workspace_import_records (action);

CREATE INDEX ix_agent_workspace_import_records_agent_id ON agent_workspace_import_records (agent_id);

CREATE INDEX ix_agent_workspace_import_records_commit_sha ON agent_workspace_import_records (commit_sha);

CREATE INDEX ix_agent_workspace_import_records_created_at ON agent_workspace_import_records (created_at);

CREATE INDEX ix_agent_workspace_import_records_status ON agent_workspace_import_records (status);

CREATE INDEX ix_agent_worktree_cleanup_tasks_agent_id ON agent_worktree_cleanup_tasks (agent_id);

CREATE INDEX ix_agent_worktree_cleanup_tasks_claim_expires_at ON agent_worktree_cleanup_tasks (claim_expires_at);

CREATE INDEX ix_agent_worktree_cleanup_tasks_claim_token ON agent_worktree_cleanup_tasks (claim_token);

CREATE INDEX ix_agent_worktree_cleanup_tasks_created_at ON agent_worktree_cleanup_tasks (created_at);

CREATE INDEX ix_agent_worktree_cleanup_tasks_next_retry_at ON agent_worktree_cleanup_tasks (next_retry_at);

CREATE INDEX ix_agent_worktree_cleanup_tasks_status ON agent_worktree_cleanup_tasks (status);

CREATE INDEX ix_agent_worktree_cleanup_tasks_updated_at ON agent_worktree_cleanup_tasks (updated_at);

CREATE UNIQUE INDEX ix_attributions_improvement_id ON attributions (improvement_id);

CREATE INDEX ix_evidence_files_file_type ON evidence_files (file_type);

CREATE INDEX ix_evidence_packages_created_at ON evidence_packages (created_at);

CREATE INDEX ix_evidence_packages_feedback_case_id ON evidence_packages (feedback_case_id);

CREATE UNIQUE INDEX ix_execution_records_improvement_id ON execution_records (improvement_id);

CREATE INDEX ix_feedback_case_sources_agent_id ON feedback_case_sources (agent_id);

CREATE INDEX ix_feedback_case_sources_case_id ON feedback_case_sources (case_id);

CREATE INDEX ix_feedback_case_sources_created_at ON feedback_case_sources (created_at);

CREATE INDEX ix_feedback_cases_agent_id ON feedback_cases (agent_id);

CREATE INDEX ix_feedback_cases_created_at ON feedback_cases (created_at);

CREATE INDEX ix_feedback_cases_priority ON feedback_cases (priority);

CREATE INDEX ix_feedback_cases_status ON feedback_cases (status);

CREATE INDEX ix_feedback_cases_updated_at ON feedback_cases (updated_at);

CREATE INDEX ix_feedback_signals_agent_id ON feedback_signals (agent_id);

CREATE INDEX ix_feedback_signals_alert_id ON feedback_signals (alert_id);

CREATE INDEX ix_feedback_signals_case_id ON feedback_signals (case_id);

CREATE INDEX ix_feedback_signals_created_at ON feedback_signals (created_at);

CREATE INDEX ix_feedback_signals_matched_run_id ON feedback_signals (matched_run_id);

CREATE INDEX ix_feedback_signals_run_id ON feedback_signals (run_id);

CREATE INDEX ix_feedback_signals_session_id ON feedback_signals (session_id);

CREATE INDEX ix_feedback_signals_source_type ON feedback_signals (source_type);

CREATE INDEX ix_feedback_source_annotations_created_at ON feedback_source_annotations (created_at);

CREATE UNIQUE INDEX ix_feedback_source_annotations_source ON feedback_source_annotations (source_kind, source_id);

CREATE INDEX ix_feedback_source_annotations_source_id ON feedback_source_annotations (source_id);

CREATE INDEX ix_feedback_source_annotations_source_kind ON feedback_source_annotations (source_kind);

CREATE INDEX ix_feedback_source_annotations_status ON feedback_source_annotations (status);

CREATE INDEX ix_feedback_source_annotations_updated_at ON feedback_source_annotations (updated_at);

CREATE INDEX ix_governance_assets_agent_id ON governance_assets (agent_id);

CREATE INDEX ix_governance_assets_asset_type ON governance_assets (asset_type);

CREATE INDEX ix_governance_assets_created_at ON governance_assets (created_at);

CREATE INDEX ix_governance_assets_updated_at ON governance_assets (updated_at);

CREATE INDEX ix_improvement_feedback_case_assignments_agent_id ON improvement_feedback_case_assignments (agent_id);

CREATE INDEX ix_improvement_feedback_case_assignments_created_at ON improvement_feedback_case_assignments (created_at);

CREATE UNIQUE INDEX ix_improvement_feedback_case_assignments_feedback_id ON improvement_feedback_case_assignments (feedback_id);

CREATE INDEX ix_improvement_feedback_case_assignments_improvement_id ON improvement_feedback_case_assignments (improvement_id);

CREATE INDEX ix_improvement_feedbacks_created_at ON improvement_feedbacks (created_at);

CREATE INDEX ix_improvement_feedbacks_improvement_id ON improvement_feedbacks (improvement_id);

CREATE INDEX ix_improvement_items_agent_id ON improvement_items (agent_id);

CREATE INDEX ix_improvement_items_created_at ON improvement_items (created_at);

CREATE INDEX ix_improvement_items_improvement_stage ON improvement_items (improvement_stage);

CREATE INDEX ix_improvement_items_improvement_status ON improvement_items (improvement_status);

CREATE INDEX ix_improvement_items_updated_at ON improvement_items (updated_at);

CREATE INDEX ix_improvement_links_created_at ON improvement_links (created_at);

CREATE INDEX ix_improvement_links_improvement_id ON improvement_links (improvement_id);

CREATE UNIQUE INDEX ix_normalized_feedbacks_improvement_id ON normalized_feedbacks (improvement_id);

CREATE UNIQUE INDEX ix_optimization_plans_improvement_id ON optimization_plans (improvement_id);

CREATE INDEX ix_pending_correlations_created_at ON pending_correlations (created_at);

CREATE INDEX ix_pending_correlations_event_id ON pending_correlations (event_id);

CREATE INDEX ix_pending_correlations_status ON pending_correlations (status);

CREATE INDEX ix_pending_correlations_updated_at ON pending_correlations (updated_at);

CREATE UNIQUE INDEX ix_regression_test_designs_improvement_id ON regression_test_designs (improvement_id);

CREATE INDEX ix_runtime_agent_deletion_intents_agent_generation ON runtime_agent_deletion_intents (agent_generation);

CREATE INDEX ix_runtime_agent_deletion_intents_agent_id ON runtime_agent_deletion_intents (agent_id);

CREATE INDEX ix_runtime_agent_deletion_intents_created_at ON runtime_agent_deletion_intents (created_at);

CREATE INDEX ix_runtime_agent_deletion_intents_status ON runtime_agent_deletion_intents (status);

CREATE INDEX ix_runtime_agent_deletion_intents_updated_at ON runtime_agent_deletion_intents (updated_at);

CREATE INDEX ix_runtime_agent_versions_created_at ON runtime_agent_versions (created_at);

CREATE INDEX ix_runtime_agent_versions_governance_agent_id ON runtime_agent_versions (governance_agent_id);

CREATE UNIQUE INDEX ix_runtime_agent_versions_runtime_agent_id ON runtime_agent_versions (runtime_agent_id);

CREATE INDEX ix_runtime_agent_versions_source_id ON runtime_agent_versions (source_id);

CREATE INDEX ix_runtime_agent_versions_source_kind ON runtime_agent_versions (source_kind);

CREATE INDEX ix_runtime_chat_operations_action_session_id ON runtime_chat_operations (action_session_id);

CREATE INDEX ix_runtime_chat_operations_client_operation_id ON runtime_chat_operations (client_operation_id);

CREATE INDEX ix_runtime_chat_operations_created_at ON runtime_chat_operations (created_at);

CREATE INDEX ix_runtime_chat_operations_operation_kind ON runtime_chat_operations (operation_kind);

CREATE INDEX ix_runtime_chat_operations_reply_id ON runtime_chat_operations (reply_id);

CREATE INDEX ix_runtime_chat_operations_root_session_id ON runtime_chat_operations (root_session_id);

CREATE INDEX ix_runtime_chat_operations_run_id ON runtime_chat_operations (run_id);

CREATE INDEX ix_runtime_chat_operations_runtime_agent_id ON runtime_chat_operations (runtime_agent_id);

CREATE INDEX ix_runtime_chat_operations_updated_at ON runtime_chat_operations (updated_at);

CREATE INDEX ix_runtime_cutover_ledger_created_at ON runtime_cutover_ledger (created_at);

CREATE INDEX ix_runtime_cutover_ledger_phase ON runtime_cutover_ledger (phase);

CREATE INDEX ix_runtime_cutover_ledger_status ON runtime_cutover_ledger (status);

CREATE INDEX ix_runtime_ephemeral_resources_agent_version_id ON runtime_ephemeral_resources (agent_version_id);

CREATE INDEX ix_runtime_ephemeral_resources_business_agent_id ON runtime_ephemeral_resources (business_agent_id);

CREATE INDEX ix_runtime_ephemeral_resources_created_at ON runtime_ephemeral_resources (created_at);

CREATE INDEX ix_runtime_ephemeral_resources_runtime_agent_id ON runtime_ephemeral_resources (runtime_agent_id);

CREATE INDEX ix_runtime_ephemeral_resources_session_id ON runtime_ephemeral_resources (session_id);

CREATE INDEX ix_runtime_ephemeral_resources_source_id ON runtime_ephemeral_resources (source_id);

CREATE INDEX ix_runtime_ephemeral_resources_source_kind ON runtime_ephemeral_resources (source_kind);

CREATE INDEX ix_runtime_ephemeral_resources_status ON runtime_ephemeral_resources (status);

CREATE INDEX ix_runtime_ephemeral_resources_updated_at ON runtime_ephemeral_resources (updated_at);

CREATE INDEX ix_runtime_ephemeral_resources_version_owner_id ON runtime_ephemeral_resources (version_owner_id);

CREATE INDEX ix_runtime_pending_actions_reply_id ON runtime_pending_actions (reply_id);

CREATE INDEX ix_runtime_pending_actions_run_id ON runtime_pending_actions (run_id);

CREATE INDEX ix_runtime_pending_actions_session_id ON runtime_pending_actions (session_id);

CREATE INDEX ix_runtime_pending_actions_status ON runtime_pending_actions (status);

CREATE INDEX ix_runtime_pending_actions_tool_call_id ON runtime_pending_actions (tool_call_id);

CREATE UNIQUE INDEX ix_runtime_receipts_event_id ON runtime_receipts (event_id);

CREATE INDEX ix_runtime_receipts_event_type ON runtime_receipts (event_type);

CREATE INDEX ix_runtime_receipts_received_at ON runtime_receipts (received_at);

CREATE INDEX ix_runtime_receipts_reply_id ON runtime_receipts (reply_id);

CREATE INDEX ix_runtime_receipts_run_id ON runtime_receipts (run_id);

CREATE INDEX ix_runtime_receipts_session_id ON runtime_receipts (session_id);

CREATE INDEX ix_runtime_session_bindings_active_run_id ON runtime_session_bindings (active_run_id);

CREATE INDEX ix_runtime_session_bindings_agent_id ON runtime_session_bindings (agent_id);

CREATE INDEX ix_runtime_session_bindings_agent_version_id ON runtime_session_bindings (agent_version_id);

CREATE INDEX ix_runtime_session_bindings_created_at ON runtime_session_bindings (created_at);

CREATE INDEX ix_runtime_session_bindings_root_session_id ON runtime_session_bindings (root_session_id);

CREATE INDEX ix_runtime_session_bindings_runtime_agent_id ON runtime_session_bindings (runtime_agent_id);

CREATE INDEX ix_runtime_session_bindings_team_id ON runtime_session_bindings (team_id);

CREATE INDEX ix_runtime_session_bindings_updated_at ON runtime_session_bindings (updated_at);

CREATE INDEX ix_runtime_session_creation_intents_agent_id ON runtime_session_creation_intents (agent_id);

CREATE INDEX ix_runtime_session_creation_intents_agent_version_id ON runtime_session_creation_intents (agent_version_id);

CREATE INDEX ix_runtime_session_creation_intents_created_at ON runtime_session_creation_intents (created_at);

CREATE UNIQUE INDEX ix_runtime_session_creation_intents_idempotency_key ON runtime_session_creation_intents (idempotency_key);

CREATE INDEX ix_runtime_session_creation_intents_runtime_agent_id ON runtime_session_creation_intents (runtime_agent_id);

CREATE INDEX ix_runtime_session_creation_intents_session_id ON runtime_session_creation_intents (session_id);

CREATE INDEX ix_runtime_session_creation_intents_status ON runtime_session_creation_intents (status);

CREATE INDEX ix_runtime_session_creation_intents_updated_at ON runtime_session_creation_intents (updated_at);

CREATE INDEX ix_runtime_session_creation_intents_workspace_id ON runtime_session_creation_intents (workspace_id);

CREATE INDEX ix_runtime_session_intents_recovery ON runtime_session_creation_intents (status, updated_at);

CREATE INDEX ix_runtime_team_deliveries_created_at ON runtime_team_deliveries (created_at);

CREATE INDEX ix_runtime_team_deliveries_run_id ON runtime_team_deliveries (run_id);

CREATE INDEX ix_runtime_team_deliveries_source_session_id ON runtime_team_deliveries (source_session_id);

CREATE INDEX ix_runtime_team_deliveries_target_session_id ON runtime_team_deliveries (target_session_id);

CREATE INDEX ix_soc_events_agent_id ON soc_events (agent_id);

CREATE INDEX ix_soc_events_alert_id ON soc_events (alert_id);

CREATE INDEX ix_soc_events_case_id ON soc_events (case_id);

CREATE INDEX ix_soc_events_created_at ON soc_events (created_at);

CREATE INDEX ix_soc_events_event_type ON soc_events (event_type);

CREATE INDEX ix_soc_events_matched_run_id ON soc_events (matched_run_id);

CREATE INDEX ix_soc_events_run_id ON soc_events (run_id);

CREATE INDEX ix_soc_events_session_id ON soc_events (session_id);

CREATE INDEX ix_soc_events_source_system ON soc_events (source_system);

CREATE UNIQUE INDEX ux_agent_release_source_claims_change_set ON agent_release_source_claims (change_set_id);

CREATE UNIQUE INDEX ux_agent_release_source_claims_release ON agent_release_source_claims (release_id);

CREATE UNIQUE INDEX ux_agent_release_tag_claims_change_set ON agent_release_tag_claims (change_set_id);

CREATE UNIQUE INDEX ux_agent_release_tag_claims_release ON agent_release_tag_claims (release_id);

CREATE UNIQUE INDEX ux_agent_runs_client_operation ON agent_runs (client_operation_id) WHERE client_operation_id IS NOT NULL;

CREATE UNIQUE INDEX ux_agent_runs_one_active_per_session ON agent_runs (session_id) WHERE status IN ('queued','running','waiting_human','waiting_external','finalizing');

CREATE UNIQUE INDEX ux_agent_test_run_items_nodeid ON agent_test_run_items (test_run_id, nodeid);

CREATE UNIQUE INDEX ux_improvement_links_identity ON improvement_links (improvement_id, kind, ref_id);

CREATE UNIQUE INDEX ux_runtime_agent_deletion_one_pending ON runtime_agent_deletion_intents (agent_id) WHERE status = 'cleanup_pending';

CREATE UNIQUE INDEX ux_runtime_pending_tool_call ON runtime_pending_actions (session_id, reply_id, tool_call_id);

CREATE UNIQUE INDEX ux_runtime_team_delivery_generation ON runtime_team_deliveries (run_id, generation);
