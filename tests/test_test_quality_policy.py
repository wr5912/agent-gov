from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from scripts.test_quality.collection import CollectionResult, collect_pytest_nodeids, collect_pytest_nodes, nodeid_digest
from scripts.test_quality.coverage import CoverageSnapshot, compare_coverage_snapshots, evaluate_coverage
from scripts.test_quality.impact import select_impacted_nodes
from scripts.test_quality.models import PortfolioPolicy, PortfolioRule, QualityPolicy
from scripts.test_quality.policy import classify_nodes, load_quality_policy, main_flow_bindings, validate_quality_policy

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = REPO_ROOT / "tests/quality_policy.json"


def _policy() -> QualityPolicy:
    return load_quality_policy(POLICY_PATH)


def test_quality_policy_schema_forbids_unknown_fields() -> None:
    raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    raw["portfolio"]["rules"][0]["classification"]["legacy_bucket"] = "slow"

    with pytest.raises(ValidationError, match="legacy_bucket"):
        QualityPolicy.model_validate(raw)


def test_quality_policy_requires_explicit_collection_selectors() -> None:
    raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    raw.pop("collection")

    with pytest.raises(ValidationError, match="collection"):
        QualityPolicy.model_validate(raw)


def test_phase_lanes_expose_ownership_resources_entrypoints_and_collection_authority() -> None:
    lanes = {lane.id: lane for lane in _policy().lanes}

    exact = lanes["p0-exact-commit"]
    assert exact.owner == "agent-lifecycle"
    assert exact.capabilities == ["agent-lifecycle", "security-boundaries"]
    assert set(exact.resources) == {"hermetic", "git", "process", "docker", "serial"}
    assert exact.enforcement == "blocking"
    assert exact.implementation_status == "active"
    assert exact.entrypoint == "make container-workspace-pytest-test"
    assert exact.collection_boundary.authority == "agent-workspace-git"
    assert exact.collection_boundary.included_in_root_collection is False

    p0_mcp = lanes["p0-mcp"]
    p1_live = lanes["p1-live"]
    assert p0_mcp.enforcement == p1_live.enforcement == "blocking"
    assert p0_mcp.implementation_status == p1_live.implementation_status == "planned"
    assert p0_mcp.entrypoint == "make container-security-mcp-test"
    assert p1_live.entrypoint == "make container-agent-evaluation-test"
    assert p0_mcp.collection_boundary.authority == "platform-fixture"
    assert p1_live.collection_boundary.authority == "evaluator-owned-git"
    assert p0_mcp.collection_boundary.included_in_root_collection is False
    assert p1_live.collection_boundary.included_in_root_collection is False


def test_lane_entrypoint_rejects_shell_composition() -> None:
    raw = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    raw["lanes"][0]["entrypoint"] = "make test-backend && env"

    with pytest.raises(ValidationError, match="entrypoint"):
        QualityPolicy.model_validate(raw)


def test_lane_validation_rejects_unknown_references_and_inconsistent_collection_authority() -> None:
    policy = _policy()
    exact = next(lane for lane in policy.lanes if lane.id == "p0-exact-commit")
    invalid = exact.model_copy(
        update={
            "owner": "missing-owner",
            "capabilities": [*exact.capabilities, "missing-capability"],
            "collection_boundary": exact.collection_boundary.model_copy(update={"included_in_root_collection": True}),
        }
    )
    mutated = policy.model_copy(update={"lanes": [invalid if lane.id == invalid.id else lane for lane in policy.lanes]})

    validation = validate_quality_policy(
        mutated,
        repo_root=REPO_ROOT,
        collection=CollectionResult((), nodeid_digest([])),
    )

    assert "lane has unknown owner: p0-exact-commit: missing-owner" in validation.errors
    assert "lane has unknown capability: p0-exact-commit: missing-capability" in validation.errors
    assert any("lane collection boundary disagrees with its authority: p0-exact-commit" in error for error in validation.errors)


@pytest.mark.parametrize(
    ("lane_id", "expected_error"),
    [
        ("p0-exact-commit", "root pytest classification references external collection lane"),
        ("p0-mcp", "root pytest classification references planned lane"),
    ],
)
def test_root_portfolio_cannot_claim_external_or_planned_lane(lane_id: str, expected_error: str) -> None:
    policy = _policy()
    rule = policy.portfolio.rules[0]
    classification = rule.classification.model_copy(update={"lanes": [lane_id]})
    mutated = policy.model_copy(update={"portfolio": PortfolioPolicy(rules=[rule.model_copy(update={"classification": classification})])})
    nodeid = "tests/test_agent_config_files.py::test_example"

    validation = validate_quality_policy(
        mutated,
        repo_root=REPO_ROOT,
        collection=CollectionResult((nodeid,), nodeid_digest([nodeid])),
    )

    assert any(expected_error in error and lane_id in error for error in validation.errors)


def test_quality_policy_rejects_coverage_regression() -> None:
    coverage = {
        "totals": {
            "percent_statements_covered": 50.0,
            "num_branches": 10,
            "missing_branches": 6,
        },
        "files": {},
    }

    errors = evaluate_coverage(coverage, _policy().coverage)

    assert any("line coverage 50.00%" in error for error in errors)
    assert any("branch coverage 40.00%" in error for error in errors)


def test_parallel_coverage_comparison_allows_only_bounded_order_noise() -> None:
    reference = CoverageSnapshot(1000, 200, 10, 80.0, 70.0)
    bounded = CoverageSnapshot(1000, 200, 10, 80.05, 69.95)
    drifted = CoverageSnapshot(1001, 200, 10, 80.2, 70.0)

    bounded_errors, _, _ = compare_coverage_snapshots(reference, bounded, max_delta_percentage_points=0.1)
    drifted_errors, _, _ = compare_coverage_snapshots(reference, drifted, max_delta_percentage_points=0.1)

    assert bounded_errors == []
    assert "coverage instrumentation universe mismatch" in drifted_errors
    assert any("coverage delta exceeds 0.10" in error for error in drifted_errors)


def test_portfolio_requires_exactly_one_effective_classification() -> None:
    policy = _policy()
    nodeid = "tests/test_agent_config_files.py::test_example"
    collection = CollectionResult((nodeid,), nodeid_digest([nodeid]))
    duplicate = PortfolioRule(
        id="overlap",
        selectors=["tests/test_agent_*.py"],
        classification=policy.portfolio.rules[0].classification,
    )
    overlapping = policy.model_copy(update={"portfolio": PortfolioPolicy(rules=[*policy.portfolio.rules, duplicate])})

    classifications, errors = classify_nodes(overlapping, collection)

    assert classifications == {}
    assert any("matched: agent-lifecycle-suite, overlap" in error for error in errors)


def test_repository_quality_policy_covers_every_collected_leaf() -> None:
    validation = validate_quality_policy(_policy(), repo_root=REPO_ROOT)

    assert validation.errors == ()
    assert len(validation.collection.nodeids) == len(validation.classifications)
    assert len(validation.collection.nodeids) >= 1000
    assert any(nodeid.startswith("docker/runtime-bootstrap/governor-workspace/tests/") for nodeid in validation.collection.nodeids)
    assert not any("/business-agents/" in nodeid.split("::", 1)[0] for nodeid in validation.collection.nodeids)
    assert {classification.owner for classification in validation.classifications.values()} == {
        "agent-lifecycle",
        "engineering-governance",
        "frontend-experience",
        "improvement-governance",
        "integrations",
        "runtime-platform",
        "security-response",
    }


def test_main_flow_bindings_are_deduplicated() -> None:
    pytest_selectors, ui_scripts = main_flow_bindings(_policy())

    assert len(pytest_selectors) == len(set(pytest_selectors))
    assert len(ui_scripts) == len(set(ui_scripts))
    assert "test:unit" in ui_scripts
    assert "verify:design-parity" in ui_scripts
    assert {
        "tests/test_runtime_db_0054.py::test_0054_backfills_complete_import_diagnostics_and_suite_status_idempotently",
        "tests/test_container_acceptance_cold_start.py::test_public_inline_loader_carries_actual_source_authority_through_fd_exec",
        "tests/test_container_acceptance_source_authority.py::test_snapshot_authority_cold_load_needs_no_repository_package_and_restores_failed_module",
        "tests/test_container_acceptance_candidate_cleanup.py::test_prepared_cleanup_interruption_resumes_from_same_authority[root-renamed-before-fsync]",
        "tests/test_container_acceptance_candidate_git.py::test_fixed_candidate_git_run_does_not_depend_on_tmpdir",
        "tests/test_container_acceptance_candidate_cleanup.py::test_cleanup_budget_includes_dependency_snapshots_beyond_source_only_limit",
        "tests/test_container_acceptance_candidate_cleanup.py::test_cleanup_budget_remains_fail_closed_above_combined_limit",
        "tests/test_container_acceptance_make_gate.py::test_gate_allows_exact_make_process_without_full_authority_environment",
        "tests/test_container_acceptance_make_gate_nested.py::test_gate_allows_four_consecutive_nested_make_permits_without_closing_parent_descriptor",
        "tests/test_container_acceptance_make_gate_contract.py::test_browser_targets_require_fresh_runtime_authority_before_permit",
        "tests/test_container_acceptance_receipt_lifecycle.py::test_status_sets_and_transition_table_are_complete_and_frozen",
        "tests/test_container_acceptance_playwright_authority.py::test_managed_browser_keeps_relative_tmpdir_through_close_and_restores",
        "tests/test_container_acceptance_playwright_authority.py::test_managed_browser_rejects_invalid_runtime_and_options",
        "tests/test_container_acceptance_playwright_authority.py::test_managed_browser_restores_after_failure",
        "tests/test_container_acceptance_playwright_authority.py::test_real_managed_browser_scripts_have_bounded_output_contract",
        "tests/test_container_acceptance_toolchain_authority.py::test_browser_runtime_validation_rehashes_the_frozen_tree",
        "tests/test_container_acceptance_toolchain_authority.py::test_dependency_merkle_is_bounded_before_sort_and_rejects_escaping_links",
        "tests/test_container_acceptance_toolchain_authority.py::test_verified_tool_ignores_unrelated_ancestor_entries_but_rejects_ancestor_replacement",
        "tests/test_agent_test_acceptance_authority.py::test_isolated_cleanup_rejects_profile_volume_model_mismatch[False-True]",
        "tests/test_runtime_container_acceptance.py::test_cleanup_browser_drift_terminalizes_failed",
        "tests/test_runtime_container_acceptance.py::test_runner_failure_output_exposes_only_bounded_exception_types",
        "tests/test_runtime_container_acceptance.py::test_terminal_witness_closes_when_lifecycle_lock_drift_blocks_commit",
        "tests/test_runtime_container_acceptance.py::test_terminal_witness_closes_when_receipt_root_drift_blocks_commit",
        "tests/test_runtime_container_acceptance.py::test_terminal_is_published_only_after_runtime_and_candidate_cleanup",
        "tests/test_runtime_container_acceptance_images.py::test_browser_runtime_drift_invalidates_postflight",
        "tests/test_runtime_container_acceptance_images.py::test_terminal_docker_query_uses_stable_cwd_and_minimal_environment",
        "tests/test_runtime_container_acceptance_images.py::test_terminal_image_query_failure_closes_frozen_source_witness",
        "tests/test_runtime_health_diagnose.py::test_diagnose_never_projects_untrusted_readiness_fields",
        "tests/test_runtime_health_diagnose.py::test_isolated_health_verifier_reports_only_bounded_contract_mismatch",
        "tests/test_runtime_health_diagnose.py::test_isolated_health_verifier_suppresses_diagnosis_child_errors",
        "tests/test_runtime_health_diagnose.py::test_isolated_health_verifier_suppresses_diagnosis_payload",
        "tests/test_speech_summary_stream.py::test_coordinator_deadline_includes_queue_and_generation_without_late_event",
        "tests/test_settings.py::test_speech_summary_terminal_drain_must_outlive_generation_timeout",
        "tests/test_runtime_container_acceptance.py::test_speech_summary_verifier_keeps_each_surface_best_effort_but_requires_one_round_event",
        "tests/test_runtime_container_acceptance_verifier.py::test_managed_environment_rejects_caller_redirects_and_binds_preserved_transport",
        "tests/test_runtime_container_acceptance_verifier.py::test_terminal_environment_keeps_fixed_authority_after_candidate_cleanup",
        "tests/test_runtime_container_acceptance_verifier.py::test_public_managed_browser_verifiers_expose_only_bounded_results",
        "tests/test_runtime_container_acceptance_receipt.py::test_historical_terminal_receipt_is_self_contained_across_live_toolchain_drift",
        "tests/test_runtime_container_acceptance_receipt.py::test_terminal_is_single_atomic_transition",
        "tests/test_runtime_container_acceptance_receipt.py::test_terminal_retention_prunes_oldest_receipt_at_capacity",
        "tests/test_runtime_container_acceptance_receipt.py::test_terminal_retention_never_prunes_nonterminal_receipt",
        "tests/test_runtime_container_acceptance_receipt.py::test_terminal_retention_delete_failure_is_fail_closed",
        "tests/test_runtime_container_acceptance_signals.py::test_signal_during_terminal_transition_is_after_the_commit_point",
        "tests/test_agent_test_worker.py::test_worker_recovery_reports_container_and_temporary_path_cleanup_failures",
        "tests/test_business_agent_deletion_integration.py::test_completed_deletion_between_runtime_precheck_and_admission_creates_no_turn",
        "tests/test_business_agent_deletion_integration.py::test_runtime_admission_rejects_stale_public_instance_etag_without_turn",
        "tests/test_business_agent_deletion_integration.py::test_test_session_invoke_loses_to_completed_deletion_before_runtime_admission",
        "tests/test_business_agent_deletion_integration.py::test_legacy_import_append_after_completed_deletion_is_rejected_without_rows",
        "tests/test_business_agent_deletion_integration.py::test_legacy_import_append_wins_then_deletion_discards_staging",
        "tests/test_sdk_session_store.py::test_orphan_reconciler_preserves_valid_turn_and_public_import_staging",
        "tests/test_sdk_session_store.py::test_orphan_reconciler_discards_unowned_import_staging_idempotently",
        "tests/test_sdk_session_store.py::test_orphan_reconciler_processes_multiple_bounded_projection_batches",
        "tests/test_agent_test_acceptance_authority.py::test_external_runtime_container_binds_labels_and_remains_stable_on_fixed_docker",
        "tests/test_runtime_container_acceptance_environment.py::test_isolated_health_cleanup_uses_network_only_model",
    } <= set(pytest_selectors)


def test_main_flow_bindings_include_workspace_activation_authorities() -> None:
    pytest_selectors, _ = main_flow_bindings(_policy())

    assert {
        "tests/test_agent_workspace_activation_terminal_invariants.py::test_terminal_commit_ack_loss_resolves_from_durable_state[completed]",
        "tests/test_agent_workspace_activation_terminal_invariants.py::test_terminal_graph_journal_is_immutable_after_preparation[completed]",
        "tests/test_agent_workspace_activation_terminal_invariants.py::test_operation_graph_rejects_noncanonical_object_identity[merge_candidate]",
        "tests/test_agent_workspace_activation_terminal_invariants.py::test_governed_graph_ignores_git_replace_objects",
        "tests/test_agent_workspace_activation_terminal_invariants.py::test_governed_index_reads_fsmonitor_flags_without_running_repository_hook",
        "tests/test_agent_git_authority.py::test_repository_filter_driver_never_executes_during_core_or_operator_status",
        "tests/test_agent_git_authority.py::test_repository_signing_program_never_executes_during_commit",
        "tests/test_agent_git_authority.py::test_repository_core_worktree_cannot_redirect_governed_commands",
        "tests/test_agent_git_authority.py::test_repository_alternates_cannot_import_another_agent_object_graph",
        "tests/test_agent_git_authority.py::test_repository_grafts_cannot_rewrite_commit_parent_evidence",
        "tests/test_agent_git_authority.py::test_repository_include_fifo_is_rejected_without_following_it",
        "tests/test_agent_git_authority.py::test_repository_archive_command_never_executes",
        "tests/test_agent_git_authority.py::test_commit_blob_reader_ignores_repository_replace_refs",
        "tests/test_agent_git_authority.py::test_symbolic_operation_ref_is_rejected_without_deleting_branch",
        "tests/test_agent_git_authority.py::test_head_cannot_reference_operation_namespace_during_ref_cleanup",
        "tests/test_agent_git_authority.py::test_head_cannot_reference_operation_namespace_during_activation",
        "tests/test_agent_git_authority.py::test_repository_commondir_cannot_redirect_agent_authority",
        "tests/test_agent_git_authority.py::test_temporary_authority_rejects_replaced_parent_without_cleaning_new_target[workspace-package-worktrees]",
        "tests/test_agent_git_authority.py::test_temporary_authority_rejects_replaced_parent_without_cleaning_new_target[workspace-package-indexes]",
        "tests/test_agent_git_authority.py::test_temporary_authority_rejects_replaced_entry_without_cleaning_new_target[workspace-package-worktrees]",
        "tests/test_agent_git_authority.py::test_temporary_authority_rejects_replaced_entry_without_cleaning_new_target[workspace-package-indexes]",
        "tests/test_agent_git_store.py::test_git_metadata_writers_reject_linked_leaves_without_external_write",
        "tests/test_agent_git_store.py::test_raw_git_storage_rejects_leaf_identity_change_before_replace",
        "tests/test_agent_git_store.py::test_raw_git_storage_rejects_parent_identity_change_without_external_write",
        "tests/test_agent_git_store.py::test_raw_git_storage_detects_leaf_identity_change_after_replace",
        "tests/test_agent_workspace_activation_recovery_resilience.py::test_assume_unchanged_and_skip_worktree_flags_restore_byte_exactly",
        "tests/test_agent_workspace_activation_recovery_resilience.py::test_durable_refs_survive_aggressive_gc_until_rejection_finishes",
        "tests/test_agent_workspace_activation_recovery_resilience.py::test_ref_cleanup_failure_keeps_completion_fenced_until_retry",
        "tests/test_runtime_db_0056.py::test_0056_orm_clean_and_historical_upgrade_have_exact_schema_parity",
        "tests/test_runtime_db_0057.py::test_0057_fresh_and_0056_upgrade_twice_have_exact_schema_authority",
        "tests/test_runtime_db_0057.py::test_0057_finish_commit_ack_loss_returns_exact_terminal_evidence",
        "tests/test_runtime_db_0057.py::test_0057_raw_sql_rejects_invalid_terminal_evidence",
        "tests/test_runtime_db_0057.py::test_0057_projection_and_resume_reject_corrupt_persisted_completion",
        "tests/test_runtime_db_0058.py::test_0058_reinstalls_activation_and_recovery_authority_idempotently",
        "tests/test_runtime_db_0059.py::test_0059_fresh_install_is_idempotent_and_preserves_legal_recovery",
        "tests/test_workspace_activation_recovery_cli.py::test_read_only_inspection_blocks_hostile_git_execution_and_lazy_fetch",
        "tests/test_workspace_activation_recovery_cli.py::test_exact_recovery_wins_stale_periodic_race_without_deadlock",
        "tests/test_workspace_activation_recovery_cli.py::test_periodic_writer_finishes_before_operator_can_reserve",
        "tests/test_workspace_activation_recovery_cli.py::test_cli_list_inspect_apply_and_no_force_surface",
        "tests/test_workspace_activation_recovery_cli.py::test_resume_closes_core_terminal_crash_and_rejects_wrong_attempts",
    } <= set(pytest_selectors)


def test_impact_selection_is_targeted_and_unknown_paths_fail_closed() -> None:
    policy = _policy()
    nodes = (
        "tests/test_state_machines.py::test_transition",
        "tests/test_runtime_db.py::test_schema",
        "tests/test_improvement_api.py::test_create",
    )
    collection = CollectionResult(nodes, nodeid_digest(nodes))
    targeted = select_impacted_nodes(
        changed_paths=["app/runtime/state_machines.py"],
        policy=policy.impact,
        collection=collection,
        eligible_nodes=nodes,
    )
    unknown = select_impacted_nodes(
        changed_paths=["app/new_unmapped_module.py"],
        policy=policy.impact,
        collection=collection,
        eligible_nodes=nodes,
    )

    assert targeted.mode == "impacted"
    assert targeted.nodeids == (
        "tests/test_runtime_db.py::test_schema",
        "tests/test_state_machines.py::test_transition",
    )
    assert unknown.mode == "full"
    assert unknown.nodeids == tuple(sorted(nodes))


def test_impact_rules_reject_selectors_that_expand_to_zero_tests() -> None:
    policy = _policy()
    rule = policy.impact.rules[0].model_copy(update={"test_selectors": ["tests/test_missing_tia.py"]})
    impact = policy.impact.model_copy(update={"rules": [rule]})
    mutated = policy.model_copy(update={"impact": impact})
    nodeid = "tests/test_state_machines.py::test_transition"

    validation = validate_quality_policy(
        mutated,
        repo_root=REPO_ROOT,
        collection=CollectionResult((nodeid,), nodeid_digest([nodeid])),
    )

    assert "impact selector expands to zero leaf nodeids: runtime-state: tests/test_missing_tia.py" in validation.errors


@pytest.mark.parametrize(
    "workspace_selector",
    [
        ".",
        "docker/runtime-bootstrap",
        "docker/runtime-bootstrap/**",
        "docker/runtime-bootstrap/business-agents",
        "docker/runtime-bootstrap/business-agents/example",
        "docker/runtime-bootstrap/business-agents/example/workspace",
        "docker/runtime-bootstrap/business-agents/example/workspace/tests",
    ],
)
def test_quality_policy_never_imports_business_agent_workspace_tests_in_root_collection(
    tmp_path: Path,
    workspace_selector: str,
) -> None:
    policy = _policy()
    marker = tmp_path / "workspace-test-imported"
    root_test = tmp_path / "tests/test_root.py"
    workspace_test = tmp_path / "docker/runtime-bootstrap/business-agents/example/workspace/tests/test_asset.py"
    root_test.parent.mkdir(parents=True)
    workspace_test.parent.mkdir(parents=True)
    root_test.write_text("def test_root():\n    assert True\n", encoding="utf-8")
    workspace_test.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('imported')\n\ndef test_asset():\n    assert True\n",
        encoding="utf-8",
    )
    collection = policy.collection.model_copy(
        update={
            "selectors": [
                "tests",
                workspace_selector,
            ]
        }
    )

    validation = validate_quality_policy(policy.model_copy(update={"collection": collection}), repo_root=tmp_path)

    assert not marker.exists()
    assert any("business Agent Workspace tests must stay outside repository-root pytest: root collection" in error for error in validation.errors)


def test_activation_git_authority_paths_select_the_recovery_tia_lane() -> None:
    policy = _policy()
    nodes = (
        "tests/test_agent_git_authority.py::test_repository_filter_driver_never_executes_during_core_or_operator_status",
        "tests/test_agent_git_authority.py::test_repository_include_fifo_is_rejected_without_following_it",
        "tests/test_agent_git_authority.py::test_repository_archive_command_never_executes",
        "tests/test_agent_git_authority.py::test_head_cannot_reference_operation_namespace_during_activation",
        "tests/test_agent_git_authority.py::test_repository_commondir_cannot_redirect_agent_authority",
        "tests/test_agent_workspace_activation_recovery_resilience.py::test_ref_cleanup_failure_keeps_completion_fenced_until_retry",
        "tests/test_agent_workspace_activation_terminal_invariants.py::test_governed_graph_ignores_git_replace_objects",
        "tests/test_agent_workspace_git_evidence.py::test_workspace_fingerprint_rejects_single_file_over_limit",
        "tests/test_runtime_db_0055.py::test_0055_freezes_graph_identity_after_preparing_transition",
        "tests/test_runtime_db_0057.py::test_0057_raw_sql_rejects_invalid_terminal_evidence",
        "tests/test_runtime_db_0058.py::test_0058_reinstalls_activation_and_recovery_authority_idempotently",
    )
    collection = CollectionResult(nodes, nodeid_digest(nodes))

    for changed_path in (
        "app/runtime/agent_git_commit_evidence.py",
        "app/runtime/agent_git_environment.py",
        "app/runtime/agent_git_store.py",
        "app/runtime/agent_repository_guard.py",
        "app/runtime/runtime_db_migrations_0058.py",
        "app/runtime/workspace_activation_graph.py",
        "app/services/agent_workspace_git_operations.py",
        "app/services/agent_workspace_index_state.py",
        "app/services/agent_workspace_package_codec.py",
    ):
        selected = select_impacted_nodes(
            changed_paths=[changed_path],
            policy=policy.impact,
            collection=collection,
            eligible_nodes=nodes,
        )
        assert selected.mode == "impacted"
        assert selected.nodeids == tuple(sorted(nodes))

    fingerprint = select_impacted_nodes(
        changed_paths=["app/services/agent_workspace_fingerprint.py"],
        policy=policy.impact,
        collection=collection,
        eligible_nodes=nodes,
    )
    assert fingerprint.mode == "impacted"
    assert fingerprint.matched_rules == ("workspace-activation-recovery",)
    assert fingerprint.nodeids == tuple(sorted(nodes))

    runtime_db = select_impacted_nodes(
        changed_paths=["app/runtime/runtime_db.py"],
        policy=policy.impact,
        collection=collection,
        eligible_nodes=nodes,
    )
    assert runtime_db.mode == "full"
    assert "app/runtime/runtime_db.py" in runtime_db.reasons


def test_git_command_and_build_context_paths_select_exact_contracts() -> None:
    policy = _policy()
    activation_node = (
        "tests/test_agent_git_authority.py::test_temporary_authority_rejects_replaced_parent_without_cleaning_new_target[workspace-package-worktrees]"
    )
    package_nodes = (
        "tests/test_agent_git_store.py::test_raw_git_storage_rejects_leaf_identity_change_before_replace",
        "tests/test_agent_workspace_package_operations.py::test_workspace_git_export_failure_is_structured",
    )
    build_context_node = "tests/test_agent_test_acceptance_authority.py::test_sidecar_and_sandbox_build_contexts_are_deny_all_exact_allowlists"
    unrelated_node = "tests/test_state_machines.py::test_transition"
    nodes = (*package_nodes, activation_node, build_context_node, unrelated_node)
    collection = CollectionResult(nodes, nodeid_digest(nodes))

    git_command = select_impacted_nodes(
        changed_paths=["app/services/agent_workspace_git_command.py"],
        policy=policy.impact,
        collection=collection,
        eligible_nodes=nodes,
    )
    assert git_command.mode == "impacted"
    assert git_command.matched_rules == ("agent-workspace-package", "workspace-activation-recovery")
    assert git_command.nodeids == tuple(sorted((*package_nodes, activation_node)))

    raw_storage = select_impacted_nodes(
        changed_paths=["app/runtime/agent_git_raw_storage.py"],
        policy=policy.impact,
        collection=collection,
        eligible_nodes=nodes,
    )
    assert raw_storage.mode == "impacted"
    assert raw_storage.matched_rules == ("agent-workspace-package",)
    assert raw_storage.nodeids == tuple(sorted(package_nodes))

    for changed_path in (
        "docker/Dockerfile.dockerignore",
        "docker/frontend.Dockerfile.dockerignore",
        "docker/litellm-sidecar.Dockerfile.dockerignore",
        "docker/agent-test-sandbox.Dockerfile.dockerignore",
    ):
        selected = select_impacted_nodes(
            changed_paths=[changed_path],
            policy=policy.impact,
            collection=collection,
            eligible_nodes=nodes,
        )
        assert selected.mode == "impacted"
        assert selected.matched_rules == ("container-build-context-authority",)
        assert selected.nodeids == (build_context_node,)


def test_new_deletion_and_settings_paths_select_only_their_contracts() -> None:
    policy = _policy()
    nodes = (
        "tests/test_agent_workspace_frontend_contract.py::test_settings_workspace_flow_uses_package_only_creation_and_exposes_receipts",
        "tests/test_business_agent_deletion.py::test_delete_reports_cleanup_pending_without_claiming_disk_removal",
        "tests/test_documentation_contracts.py::test_workspace_deletion_docs_match_durable_contract",
        "tests/test_openapi_export.py::test_agent_deletion_openapi_requires_exact_headers_and_durable_receipt",
        "tests/test_runtime_db_0059.py::test_0059_fresh_install_is_idempotent_and_preserves_legal_recovery",
        "tests/test_state_machines.py::test_agent_deletion_has_one_way_complete_transition_and_terminal_does_not_reopen",
        "tests/test_business_agent_deletion_integration.py::test_completed_deletion_between_runtime_precheck_and_admission_creates_no_turn",
        "tests/test_business_agent_deletion_integration.py::test_runtime_admission_rejects_stale_public_instance_etag_without_turn",
        "tests/test_business_agent_deletion_integration.py::test_test_session_invoke_loses_to_completed_deletion_before_runtime_admission",
        "tests/test_business_agent_deletion_integration.py::test_legacy_import_append_after_completed_deletion_is_rejected_without_rows",
        "tests/test_business_agent_deletion_integration.py::test_legacy_import_append_wins_then_deletion_discards_staging",
        "tests/test_sdk_session_store.py::test_orphan_reconciler_preserves_valid_turn_and_public_import_staging",
        "tests/test_sdk_session_store.py::test_orphan_reconciler_discards_unowned_import_staging_idempotently[deleted]",
        "tests/test_sdk_session_store.py::test_orphan_reconciler_processes_multiple_bounded_projection_batches",
        "tests/test_sdk_session_migration.py::test_legacy_import_promotes_official_sdk_transcript_once",
        "tests/test_sdk_session_migration.py::test_import_claim_is_cross_connection_fenced_and_expired_owner_cannot_finalize",
        "tests/test_sdk_session_migration.py::test_mapping_invalidation_discards_inflight_import_staging[clear]",
        "tests/test_sdk_session_migration.py::test_mapping_invalidation_discards_inflight_import_staging[delete]",
    )
    collection = CollectionResult(nodes, nodeid_digest(nodes))
    expected_deletion = tuple(sorted((nodes[0], nodes[1], *nodes[3:5], *nodes[6:])))
    for changed_path in (
        "app/runtime/agent_admission.py",
        "app/runtime/session_turn_admission.py",
        "app/runtime/sdk_session_store.py",
        "app/runtime/sdk_session_migration.py",
        "app/runtime/runtime_db_migrations_0059.py",
        "app/services/business_agent_deletion.py",
    ):
        deletion = select_impacted_nodes(changed_paths=[changed_path], policy=policy.impact, collection=collection, eligible_nodes=nodes)
        assert deletion.mode == "impacted"
        assert "business-agent-durable-deletion" in deletion.matched_rules
        assert deletion.nodeids == expected_deletion

    expected_frontend = tuple(sorted((nodes[0], nodes[2], nodes[3])))
    for changed_path in (
        "frontend/src/components/SettingsModal.requestContextRace.test.ts",
        "frontend/src/components/settingsRequestContext.ts",
    ):
        selected = select_impacted_nodes(
            changed_paths=[changed_path],
            policy=policy.impact,
            collection=collection,
            eligible_nodes=nodes,
        )
        assert selected.mode == "impacted"
        assert selected.matched_rules == ("agent-workspace-frontend", "frontend-contract")
        assert selected.nodeids == expected_frontend


def test_container_acceptance_contract_selects_verifier_and_receipt_authority() -> None:
    policy = _policy()
    nodes = (
        "tests/test_container_acceptance_cold_start.py::test_public_inline_loader_carries_actual_source_authority_through_fd_exec",
        "tests/test_container_acceptance_source_authority.py::test_snapshot_authority_cold_load_needs_no_repository_package_and_restores_failed_module",
        "tests/test_container_acceptance_candidate_cleanup.py::test_prepared_cleanup_interruption_resumes_from_same_authority[root-renamed-before-fsync]",
        "tests/test_container_acceptance_candidate_git.py::test_fixed_candidate_git_run_does_not_depend_on_tmpdir",
        "tests/test_container_acceptance_make_gate.py::test_gate_allows_exact_make_process_without_full_authority_environment",
        "tests/test_container_acceptance_make_gate_nested.py::test_gate_allows_four_consecutive_nested_make_permits_without_closing_parent_descriptor",
        "tests/test_container_acceptance_receipt_lifecycle.py::test_status_sets_and_transition_table_are_complete_and_frozen",
        "tests/test_runtime_container_acceptance_verifier.py::test_managed_environment_rejects_caller_redirects_and_binds_preserved_transport",
        "tests/test_runtime_container_acceptance_receipt.py::test_historical_terminal_receipt_is_self_contained_across_live_toolchain_drift",
        "tests/test_runtime_container_acceptance_receipt.py::test_terminal_is_single_atomic_transition",
        "tests/test_runtime_container_acceptance_signals.py::test_signal_during_terminal_transition_is_after_the_commit_point",
        "tests/test_agent_test_acceptance_authority.py::test_external_runtime_container_binds_labels_and_remains_stable_on_fixed_docker",
        "tests/test_agent_test_acceptance_authority.py::test_isolated_cleanup_rejects_profile_volume_model_mismatch[False-True]",
        "tests/test_runtime_container_acceptance_environment.py::test_isolated_health_cleanup_uses_network_only_model",
        "tests/test_runtime_health_diagnose.py::test_diagnose_never_projects_untrusted_readiness_fields",
        "tests/test_runtime_health_diagnose.py::test_isolated_health_verifier_reports_only_bounded_contract_mismatch",
        "tests/test_runtime_health_diagnose.py::test_isolated_health_verifier_suppresses_diagnosis_child_errors",
        "tests/test_runtime_health_diagnose.py::test_isolated_health_verifier_suppresses_diagnosis_payload",
        "tests/test_runtime_db_0059.py::test_0059_fresh_install_is_idempotent_and_preserves_legal_recovery",
    )
    collection = CollectionResult(nodes, nodeid_digest(nodes))

    for changed_path in (
        "scripts/container_acceptance_contract.py",
        "scripts/container_acceptance_candidate_git.py",
        "scripts/container_acceptance_image_authority.py",
        "scripts/container_acceptance_make_gate.py",
        "scripts/diagnose_runtime_health.py",
        "tests/test_container_acceptance_cold_start.py",
        "tests/test_container_acceptance_source_authority.py",
        "tests/test_runtime_container_acceptance_signals.py",
    ):
        selected = select_impacted_nodes(
            changed_paths=[changed_path],
            policy=policy.impact,
            collection=collection,
            eligible_nodes=nodes,
        )

        assert selected.mode == "impacted"
        assert selected.matched_rules == ("agent-test-execution",)
        assert selected.nodeids == tuple(sorted(nodes[:-1]))

    for changed_path in (
        "scripts/playwright_browser_authority.mjs",
        "scripts/verify_asset_registry.mjs",
        "scripts/verify_improvement_decision_ui.mjs",
        "scripts/verify_improvement_ui_real_container.mjs",
        "scripts/verify_message_actions_browser.mjs",
        "scripts/verify_openai_responses_container.mjs",
        "scripts/verify_openapi_docs.mjs",
        "scripts/verify_playground_cancel.mjs",
        "scripts/verify_provider_health_container.mjs",
    ):
        selected = select_impacted_nodes(
            changed_paths=[changed_path],
            policy=policy.impact,
            collection=collection,
            eligible_nodes=nodes,
        )

        assert selected.mode == "impacted"
        assert "agent-test-execution" in selected.matched_rules
        assert selected.nodeids == tuple(sorted(nodes[:-1]))


def test_defensive_runtime_scripts_select_exact_contract_lanes() -> None:
    policy = _policy()
    nodes = (
        "tests/test_deploy_agent_gov_to_host_script.py::test_deploy_script_without_explicit_change_contract_has_no_external_effect",
        "tests/test_repository_env_policy.py::test_langfuse_permission_init_reuses_all_stateful_mount_sources",
        "tests/test_responses_api.py::test_control_missing_agent_id_422",
        "tests/test_runtime_container_acceptance.py::test_frontend_real_container_scripts_expose_only_guarded_impls",
    )
    collection = CollectionResult(nodes, nodeid_digest(nodes))

    for changed_path in (
        "scripts/deploy_agent_gov_to_host",
        "scripts/fix_host_backend_volume_permissions.sh",
    ):
        selected = select_impacted_nodes(
            changed_paths=[changed_path],
            policy=policy.impact,
            collection=collection,
            eligible_nodes=nodes,
        )
        assert selected.mode == "impacted"
        assert selected.nodeids == tuple(sorted(nodes[:2]))

    responses = select_impacted_nodes(
        changed_paths=["scripts/verify_openai_responses_container.mjs"],
        policy=policy.impact,
        collection=collection,
        eligible_nodes=nodes,
    )
    assert responses.mode == "impacted"
    assert responses.nodeids == tuple(sorted(nodes[2:]))


def test_defensive_boundary_authority_paths_select_boundary_contracts() -> None:
    policy = _policy()
    nodeid = "tests/test_defensive_security_boundary.py::test_current_built_in_agents_keep_defensive_boundary"
    collection = CollectionResult((nodeid,), nodeid_digest([nodeid]))

    for changed_path in (
        ".codex/guidance/project.md",
        ".codex/hooks/codex_governance_stop.py",
        ".codex/skills/codex-config-optimizer/scripts/audit_codex_config.py",
        "app/runtime/protected_business_agents.py",
    ):
        selected = select_impacted_nodes(
            changed_paths=[changed_path],
            policy=policy.impact,
            collection=collection,
            eligible_nodes=(nodeid,),
        )
        assert selected.mode == "impacted"
        assert selected.nodeids == (nodeid,)


def test_tia_git_diff_includes_deleted_paths() -> None:
    selector = (REPO_ROOT / "scripts/select_impacted_tests.py").read_text(encoding="utf-8")

    assert "--diff-filter=ACMRD" in selector


def test_collect_pytest_nodeids_rejects_unknown_parametrized_case(tmp_path: Path) -> None:
    test_file = tmp_path / "tests/test_policy.py"
    test_file.parent.mkdir()
    test_file.write_text(
        "import pytest\n\n@pytest.mark.parametrize('value', [1], ids=['known'])\ndef test_case(value):\n    assert value\n",
        encoding="utf-8",
    )

    errors = collect_pytest_nodeids(["tests/test_policy.py::test_case[missing]"], repo_root=tmp_path)

    assert len(errors) == 1
    assert "could not collect" in errors[0]


def test_collect_pytest_nodes_accepts_explicit_repository_test_roots(tmp_path: Path) -> None:
    root_test = tmp_path / "tests" / "test_root.py"
    workspace_test = tmp_path / "docker" / "runtime-bootstrap" / "agent" / "workspace" / "tests" / "test_workspace.py"
    root_test.parent.mkdir(parents=True)
    workspace_test.parent.mkdir(parents=True)
    root_test.write_text("def test_root():\n    assert True\n", encoding="utf-8")
    workspace_test.write_text("def test_workspace():\n    assert True\n", encoding="utf-8")

    collection = collect_pytest_nodes(
        ["tests", "docker/runtime-bootstrap/agent/workspace/tests"],
        repo_root=tmp_path,
    )

    assert collection.nodeids == (
        "docker/runtime-bootstrap/agent/workspace/tests/test_workspace.py::test_workspace",
        "tests/test_root.py::test_root",
    )
    assert list(tmp_path.rglob("__pycache__")) == []
