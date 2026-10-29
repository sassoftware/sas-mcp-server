# SAS Viya MCP Server: Viya compatibility by tool

_SAS Viya MCP Server 1.19.0. Checked 2026-10-29 against the public API specifications on developer.sas.com._

Tools are grouped by the tiers used for `MCP_TIERS` in the [README](README.md#limiting-exposed-tools-tiers).

## Summary

**109 of the 115 tools make no call that differs between LTS 2024.09 and Stable 2026.10.** Every endpoint, method, query parameter and media type they use exists unchanged in every release checked. No release requires a parameter that the current release does not.

The six tools that differ are in Tier 3 (Reports & Visualization) and Tier 9 (Business Glossary):

- **Report authoring and management** (`create_report`, `apply_report_operations`, `copy_report`, `delete_report`) is limited on LTS 2024.09.
- **`export_report` with `format=summary`** needs 2026.03 or later.
- **Publishing a glossary term** (`update_glossary_term` with a state change) needs 2025.03 or later.

All 44 decisioning tools (Tier 7 and Tier 10) work the same on every release checked, including the lookup table, segmentation tree, code file, value list and custom function tools that are new in 1.19.0.

Legend: ✓ no difference from the current release · ⚠ works with gaps · ✗ the call does not exist in that release

Tools whose release columns are all ✓ also carry spec-versus-code gaps. Those gaps are identical in every release, and the tools work on the current release.

## Tier 0 — Compute Contexts & Code Execution

| Tool                    | Viya services  | 2024.09 LTS | 2025.03 LTS | 2025.09 LTS | 2026.03 LTS | 2026.07 | 2026.08 | 2026.09 | 2026.10 |
| ----------------------- | -------------- | ----------- | ----------- | ----------- | ----------- | ------- | ------- | ------- | ------- |
| `execute_sas_code`      | compute, files | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `list_compute_contexts` | compute        | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `reset_compute_session` | compute        | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |

## Tier 1 — Data Discovery

| Tool                             | Viya services       | 2024.09 LTS | 2025.03 LTS | 2025.09 LTS | 2026.03 LTS | 2026.07 | 2026.08 | 2026.09 | 2026.10 |
| -------------------------------- | ------------------- | ----------- | ----------- | ----------- | ----------- | ------- | ------- | ------- | ------- |
| `catalog_search`                 | catalog             | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `catalog_search_helper`          | catalog             | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `catalog_find_instance`          | catalog             | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `catalog_run_adhoc_analysis`     | catalog             | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `catalog_get_adhoc_analysis`     | catalog             | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `catalog_download_table_profile` | catalog             | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `catalog_list_agents`            | catalog             | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `catalog_run_agent`              | catalog             | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `catalog_get_agent_history`      | catalog             | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `list_cas_servers`               | casManagement       | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `list_caslibs`                   | casManagement       | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `list_castables`                 | casManagement       | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `list_source_tables`             | casManagement       | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `get_castable_info`              | casManagement       | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `get_castable_columns`           | casManagement       | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `get_castable_data`              | dataTables, rowSets | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `query_data`                     | compute             | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `list_compute_libraries`         | compute             | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `list_compute_tables`            | compute             | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `list_compute_columns`           | compute             | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `get_compute_table_data`         | compute             | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |

## Tier 2 — Data Operations & Files

| Tool                      | Viya services | 2024.09 LTS | 2025.03 LTS | 2025.09 LTS | 2026.03 LTS | 2026.07 | 2026.08 | 2026.09 | 2026.10 |
| ------------------------- | ------------- | ----------- | ----------- | ----------- | ----------- | ------- | ------- | ------- | ------- |
| `upload_data`             | casManagement | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `upload_inline_data`      | casManagement | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `promote_table_to_memory` | casManagement | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `list_files`              | files         | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `upload_file`             | files         | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `download_file`           | files         | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `find_folder`             | folders       | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |

## Tier 3 — Reports & Visualization

| Tool                      | Viya services            | 2024.09 LTS          | 2025.03 LTS    | 2025.09 LTS    | 2026.03 LTS | 2026.07 | 2026.08 | 2026.09 | 2026.10 |
| ------------------------- | ------------------------ | -------------------- | -------------- | -------------- | ----------- | ------- | ------- | ------- | ------- |
| `list_reports`            | reports                  | ✓                    | ✓              | ✓              | ✓           | ✓       | ✓       | ✓       | ✓       |
| `get_report`              | reports                  | ✓                    | ✓              | ✓              | ✓           | ✓       | ✓       | ✓       | ✓       |
| `export_report`           | visualAnalytics          | ⚠ no `summary`       | ⚠ no `summary` | ⚠ no `summary` | ✓           | ✓       | ✓       | ✓       | ✓       |
| `describe_report_objects` | none (built-in catalog)  | ✓                    | ✓              | ✓              | ✓           | ✓       | ✓       | ✓       | ✓       |
| `create_report`           | reports, visualAnalytics | ⚠ A                  | ✓              | ✓              | ✓           | ✓       | ✓       | ✓       | ✓       |
| `apply_report_operations` | reports, visualAnalytics | ⚠ B                  | ✓              | ✓              | ✓           | ✓       | ✓       | ✓       | ✓       |
| `get_report_outline`      | reports                  | ✓                    | ✓              | ✓              | ✓           | ✓       | ✓       | ✓       | ✓       |
| `copy_report`             | visualAnalytics          | ✗ no copy endpoint   | ✓              | ✓              | ✓           | ✓       | ✓       | ✓       | ✓       |
| `delete_report`           | visualAnalytics          | ✗ no delete endpoint | ✓              | ✓              | ✓           | ✓       | ✓       | ✓       | ✓       |

- **A:** Creates reports with most objects. Cannot add page titles, text or images, set data item names and formats, or add 8 chart types. Forecasting objects fail.
- **B:** Adds pages and most objects. Cannot change existing objects or data, add page titles, text or images, set data item names and formats, or add 8 chart types.
- Details of each gap are in [What the differences are](#what-the-differences-are-by-api).

## Tier 4 — Batch Jobs & Async Execution

| Tool               | Viya services       | 2024.09 LTS | 2025.03 LTS | 2025.09 LTS | 2026.03 LTS | 2026.07 | 2026.08 | 2026.09 | 2026.10 |
| ------------------ | ------------------- | ----------- | ----------- | ----------- | ----------- | ------- | ------- | ------- | ------- |
| `submit_batch_job` | jobExecution        | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `get_job_status`   | jobExecution        | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `list_jobs`        | jobExecution        | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `cancel_job`       | jobExecution        | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `get_job_log`      | files, jobExecution | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |

## Tier 5 — Automated Machine Learning

| Tool                         | Viya services                       | 2024.09 LTS | 2025.03 LTS | 2025.09 LTS | 2026.03 LTS | 2026.07 | 2026.08 | 2026.09 | 2026.10 |
| ---------------------------- | ----------------------------------- | ----------- | ----------- | ----------- | ----------- | ------- | ------- | ------- | ------- |
| `list_ml_projects`           | mlPipelineAutomation                | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `create_ml_project`          | casManagement, mlPipelineAutomation | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `run_ml_project`             | mlPipelineAutomation                | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `register_ml_champion_model` | mlPipelineAutomation                | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `publish_ml_champion_model`  | mlPipelineAutomation                | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |

## Tier 6 — Model Management & Scoring

| Tool                            | Viya services      | 2024.09 LTS | 2025.03 LTS | 2025.09 LTS | 2026.03 LTS | 2026.07 | 2026.08 | 2026.09 | 2026.10 |
| ------------------------------- | ------------------ | ----------- | ----------- | ----------- | ----------- | ------- | ------- | ------- | ------- |
| `list_registered_models`        | modelRepository    | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `list_publishing_destinations`  | modelPublish       | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `list_mas_modules`              | microanalyticScore | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `get_mas_module_step_signature` | microanalyticScore | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `score_data`                    | microanalyticScore | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |

## Tier 7 — Decisioning Foundation (SAS Intelligent Decisioning)

| Tool                               | Viya services                                                          | 2024.09 LTS | 2025.03 LTS | 2025.09 LTS | 2026.03 LTS | 2026.07 | 2026.08 | 2026.09 | 2026.10 |
| ---------------------------------- | ---------------------------------------------------------------------- | ----------- | ----------- | ----------- | ----------- | ------- | ------- | ------- | ------- |
| `create_business_ruleset`          | businessRules, folders                                                 | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `update_business_ruleset`          | businessRules, folders                                                 | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `get_business_ruleset`             | businessRules                                                          | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `list_business_rulesets`           | businessRules                                                          | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `delete_business_ruleset`          | businessRules                                                          | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `lock_business_ruleset_revision`   | businessRules                                                          | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `list_business_ruleset_revisions`  | businessRules                                                          | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `create_business_rule`             | businessRules, referenceData                                           | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `update_business_rule`             | businessRules, referenceData                                           | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `get_business_rule`                | businessRules                                                          | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `list_business_rules`              | businessRules                                                          | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `delete_business_rule`             | businessRules                                                          | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `create_decision_flow`             | businessRules, decisions, files, folders, modelRepository              | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `update_decision_flow`             | businessRules, decisions, files, folders, modelRepository              | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `get_decision_flow`                | decisions                                                              | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `list_decision_flows`              | decisions                                                              | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `delete_decision_flow`             | decisions                                                              | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `get_decision_flow_code`           | decisions                                                              | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `lock_decision_flow_revision`      | decisions                                                              | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `list_decision_flow_revisions`     | decisions                                                              | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `get_decision_flow_revision`       | decisions                                                              | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `publish_decision_flow`            | decisions, microanalyticScore, modelPublish                            | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `validate_decision_expression`     | decisions                                                              | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `list_decisioning_assets`          | businessRules, decisions, referenceData                                | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `get_decisioning_asset`            | businessRules, decisions, files, folders, referenceData, relationships | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `get_decisioning_asset_code`       | businessRules, decisions, files                                        | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `list_decisioning_asset_revisions` | businessRules, decisions                                               | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `lock_decisioning_asset_revision`  | businessRules, decisions, files                                        | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `copy_decisioning_asset`           | businessRules, decisions, files, folders, referenceData, relationships | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `delete_decisioning_asset`         | businessRules, decisions, referenceData, relationships                 | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `get_lookup_table`                 | businessRules, folders, referenceData, relationships                   | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `create_lookup_table`              | folders, referenceData                                                 | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `update_lookup_table`              | folders, referenceData                                                 | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `activate_lookup_table`            | referenceData                                                          | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |

Decisioning asset summaries (from `get_decisioning_asset`, `copy_decisioning_asset`, and from create and update results too large to return in full) include `hasWarnings` and `hasWarningsInSubDecisions` only from 2026.03 (decisions API v27), because earlier releases do not return them. The tools work the same otherwise.

## Tier 8 — Workbench (Execute Code Only)

| Tool               | Viya services  | 2024.09 LTS | 2025.03 LTS | 2025.09 LTS | 2026.03 LTS | 2026.07 | 2026.08 | 2026.09 | 2026.10 |
| ------------------ | -------------- | ----------- | ----------- | ----------- | ----------- | ------- | ------- | ------- | ------- |
| `execute_sas_code` | compute, files | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |

Same tool as in Tier 0. This tier exposes it on its own.

## Tier 9 — Business Glossary (SAS Data Governance)

| Tool                        | Viya services                 | 2024.09 LTS | 2025.03 LTS | 2025.09 LTS | 2026.03 LTS | 2026.07 | 2026.08 | 2026.09 | 2026.10 |
| --------------------------- | ----------------------------- | ----------- | ----------- | ----------- | ----------- | ------- | ------- | ------- | ------- |
| `search_glossary_terms`     | catalog, glossary             | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `list_glossary_terms`       | glossary                      | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `get_glossary_term`         | catalog, glossary             | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `list_glossary_term_types`  | glossary                      | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `get_glossary_term_type`    | glossary                      | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `list_term_assets`          | catalog, glossary             | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `list_table_terms`          | catalog                       | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `create_glossary_term`      | glossary                      | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `update_glossary_term`      | glossary                      | ✗ C         | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `delete_glossary_term`      | glossary                      | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `assign_glossary_term`      | catalog, glossary             | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `unassign_glossary_term`    | catalog, glossary             | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `create_glossary_term_type` | glossary                      | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `update_glossary_term_type` | glossary                      | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `delete_glossary_term_type` | glossary                      | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `import_glossary_terms`     | files, glossary, jobExecution | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |

- **C:** Cannot change a term's draft state, so terms cannot be published (glossary API v1). Other updates work.

## Tier 10 — Decisioning Advanced (SAS Intelligent Decisioning)

| Tool                                  | Viya services                                          | 2024.09 LTS | 2025.03 LTS | 2025.09 LTS | 2026.03 LTS | 2026.07 | 2026.08 | 2026.09 | 2026.10 |
| ------------------------------------- | ------------------------------------------------------ | ----------- | ----------- | ----------- | ----------- | ------- | ------- | ------- | ------- |
| `create_segmentation_tree`            | decisions, folders                                     | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `update_segmentation_tree`            | decisions, folders                                     | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `create_segmentation_tree_from_table` | dataTables, decisions, folders, referenceData, rowSets | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `create_code_file`                    | decisions, files, folders                              | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `update_code_file`                    | decisions, files, folders                              | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `create_value_list`                   | folders, referenceData                                 | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `update_value_list_entries`           | referenceData                                          | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `create_custom_function`              | businessRules                                          | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `update_custom_function`              | businessRules                                          | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |
| `validate_custom_function`            | businessRules                                          | ✓           | ✓           | ✓           | ✓           | ✓       | ✓       | ✓       | ✓       |

Tier 10 is meant to run with Tier 7, which puts this content into decisions and reads, locks, copies and deletes it.

---

## What the differences are, by API

### Visual Analytics API (Tier 3)

**LTS 2024.09** lacks:

- the `copy` and `DELETE` report endpoints
- the `updateObject` and `updateData` operations
- `dataItems` on `addData` (renames, formats, geography)
- the `text` and `image` objects, which `addPage`'s `title` is built from
- eight object types:
    - Bayesian network
    - forest
    - gradient boosting
    - neural network
    - nonparametric logistic regression
    - geo line
    - geo line coordinate
    - geo network

It also spells the forecasting object `forcasting`, so a `forecasting` object is rejected there.

**LTS 2025.03 and 2025.09** have no `summary` export.

### Glossary API (Tier 9)

**LTS 2024.09** has no `PUT /terms/{id}/draft/state`, so a term cannot be published through the tool.

---

## Changes since the 1.18.0 matrix (2026-09-29)

- **23 new tools, all ✓ in every release:**
    - `find_folder` (Tier 2);
    - in Tier 7: `validate_decision_expression`, the seven decisioning life-cycle tools, and the four lookup table tools;
    - all ten tools of the new Tier 10.
- **Tier 7 is split.** It is now Decisioning Foundation, with 34 tools. Trees, value lists, code files and custom functions form the new Tier 10, Decisioning Advanced.
- **Changed calls in existing tools, all ✓:**
    - Rule set and decision creates and updates file the asset in a folder (folders service).
    - Rule tools resolve lookup tables (reference data service).
    - Decision flows read every kind of step they wire: model repository, files and code files.
    - `publish_decision_flow` asks for MAS code (`codeTarget=microAnalyticService`).
    - `score_data` retries with renamed inputs after reading the step signature.
- **New release 2026.10.** The only API version that moves from 2026.09 is the catalog API, from v8 to v9. No tool's calls are affected.
- **2026.06 left support.** Stable is supported for the four most recent releases, so 2026.06 went out of support when 2026.10 came out. It is no longer in the matrix. In the 1.18.0 matrix every tool was ✓ there.

## How this was checked

- **Inventory:** every Viya REST call each tool can make was listed from the source code. That includes conditional branches, retries, polling and rollback calls: 564 calls across 114 tools. `describe_report_objects` makes no call.
- **Spec match:** each call's path, method, query parameters, request media type and Accept type was checked against the API version that developer.sas.com lists for each release.
- **Request bodies:** for Visual Analytics, the operation names, object types and data-item properties the tools send were also compared. For decisions, the step types and other enumerated values were compared too.
- **Verdicts:** a cell is ✓ when a call's result in that release is the same as in 2026.10.
- **Limits:**
    - Response fields were not compared, apart from the validation summary noted under Tier 7.
    - Behaviour was tested live on one release (2026.03 and later), not on every release in the matrix.
    - Identities calls are excluded.
