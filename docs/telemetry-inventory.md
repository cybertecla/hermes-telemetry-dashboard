# Hermes Shared Metrics — Data Inventory (canonical reference)

> HER-27 — frozen 2026-09-30. Authoritative sources:
> - Schema: `hermes_cli/observability/schemas/hermes.shared_metrics.v3.schema.json` (76 `$defs`)
> - Contract: `hermes_cli/observability/shared_metrics_contract.py`
> - Fields: `hermes_cli/observability/shared_metrics_fields.py`
> - Live DB: `~/.hermes/telemetry/shared_metrics/metrics.sqlite3`
>
> If the shipped schema changes, re-derive this file from the schema JSON — never from memory.

## 1. Where the data lives

| Path | What |
|---|---|
| `~/.hermes/telemetry/shared_metrics/metrics.sqlite3` | Collection DB (live-writer; read with `mode=ro` + busy_timeout or copy first) |
| `~/.hermes/telemetry/shared_metrics/outbox/*.json` | Packaged payloads staged for export (one file per package) |
| `~/.hermes/telemetry/shared_metrics/process_markers/` | Crash markers per CLI process |
| `~/.hermes/telemetry/shared_metrics/desktop_onboarding/` | Consent + first-message stamps |
| `~/.hermes/config.yaml` → `telemetry.shared_metrics.{enabled,send}` | Collection / sending toggles |

Current state on this machine: `enabled: true, send: false` — collecting locally, **nothing has ever been sent** (`package_outbox.send_attempts = 0`). Send endpoint (only used when `send: true`): `https://telemetry.nousresearch.com/v1/telemetry`.

## 2. SQLite tables

| Table | Role |
|---|---|
| `counter_aggregates` | Period-batched counters. PK: `(period_start, metric_name, hermes_version, os_family, architecture, install_method, dimensions_json)`. Columns: `value`, `packaged_value` |
| `package_outbox` | Export packages: `payload_json` (v3), `period_start/end`, `send_state`, `send_attempts`, `sent_at`, `claim_token` |
| `telemetry_state` | Install identity + milestones + daily-once flags + engagement state (key/value) |
| `send_consent_windows` | Consent window bookkeeping (empty while send is off) |
| `consent_marks` | `obs` + `data` consent stamps |

Retention: exported history kept 30 days; pending deltas until exported. With `send: false`, packages accumulate indefinitely.

## 3. Payload envelope (schema v3)

Top-level: `schema_version` (`hermes.shared_metrics.v3`), `package_id` (uuid), `install_id` (random profile-scoped, stable until the telemetry dir is deleted — NOT derived from hardware/account), `period_start`, `period_end`, `generated_at`, `resource` (`architecture`, `hermes_version`, `install_method`, `os_family`), `metrics[]`.

Each metric: `{name, type: "counter", value, dimensions{}}` — all values are **bucketed or closed-enum**; raw free-text never leaves the machine (skill/plugin/command names outside the shipped catalogs report as `custom`).

## 4. Metric catalog (from schema v3 — 76 defs, 69 emissive counters)

All counters carry `{name, type, value}`; only `dimensions` differ. Bucket conventions: `*_bucket` = closed buckets (`0`, `1`, `2`, `3_to_5`, `6_to_10`, `26_to_100`, ...), `*_family`/catalog names = shipped public-catalog identifiers, `unknown` = could not be measured.

| Metric | Dimensions |
|---|---|
| `hermes.client.active` | — |
| `hermes.model_call.count` | call_role, locality, model_family, outcome, provider_family |
| `hermes.model_route.count` | model, provider, call_role, error_class, outcome, ttft_bucket |
| `hermes.model_tokens.sum` | aux_task, call_role, model, provider, token_type |
| `hermes.task_run.started` | entrypoint, execution_surface, platform |
| `hermes.task_run.finished` | — |
| `hermes.task_run.duration` | duration_bucket, execution_surface, outcome, retry_count_bucket |
| `hermes.task_cost.count` | api_calls_bucket, model, outcome, provider, tokens_bucket, tool_calls_bucket |
| `hermes.tool_call.count` | approval_outcome, latency_bucket, outcome, retry_count_bucket, tool_category |
| `hermes.tool_call.latency` | latency_bucket, retry_count_bucket, tool_category |
| `hermes.tool.usage.count` | error_class, outcome, tool_name |
| `hermes.tool_approval.count` | attribution, outcome |
| `hermes.tool_recovery.count` | model, next_outcome, next_tool, provider, tool |
| `hermes.tool_unavailable.count` | model, provider, tool_name |
| `hermes.tool_output_truncation.count` | original_size_bucket, tool, truncated |
| `hermes.tool_overhead.count` | enabled_tool_count_bucket, execution_surface, tool_schema_tokens_bucket |
| `hermes.tool_enabled_unused.count` | toolset, used |
| `hermes.file_edit.count` | match_strategy, mode, outcome, tool |
| `hermes.terminal.outcome.count` | backend, command_kind, outcome |
| `hermes.session.count` | active_duration_bucket, entrypoint, execution_surface, failed_turn_count_bucket, last_outcome, platform, turn_count_bucket, message_count_bucket, model_call_count_bucket, tool_call_count_bucket |
| `hermes.skill.lifecycle.count` | action, provenance |
| `hermes.skill.load.count` | post_patch_state, provenance, reuse_state, use_count_bucket, skill_name |
| `hermes.slash_command.count` | command, execution_surface |
| `hermes.extension.install.count` | kind, name, outcome, source |
| `hermes.install.milestone` | install_age_bucket, milestone |
| `hermes.install.snapshot` | cron_job_count_bucket, mcp_server_count_bucket, memory_provider, plugin_count_bucket, profile_count_bucket, skill_count_bucket, display_language, install_age_bucket, main_provider, messaging_platform_count_bucket, terminal_backend, behind_bucket, gpu_class, local_model_provider_used, ram_bucket, release_channel, version_age_bucket |
| `hermes.setup.completed` | provider, surface |
| `hermes.compression.count` | context_fill_bucket, outcome, trigger |
| `hermes.model_switch.count` | execution_surface, from_provider, to_provider |
| `hermes.model_switch_after.count` | model, provider, turns_before_switch_bucket |
| `hermes.fallback.count` | error_class, from_provider, to_provider |
| `hermes.memory.op.count` | op, origin, outcome, provider |
| `hermes.curator.run.count` | archived_bucket, created_bucket, merged_bucket, outcome, patched_bucket, trigger |
| `hermes.delegation.run.count` | depth, mode, outcome, subagent_count_bucket |
| `hermes.execution_backend.count` | backend, error_class, kind, outcome |
| `hermes.model_tool_quality.count` | call_role, issue, model, provider |
| `hermes.model_friction.count` | model, provider, signal |
| `hermes.model_reply_issue.count` | issue, model, provider |
| `hermes.context_peak.count` | limit_hit, model, peak_fill_bucket, provider, window_bucket |
| `hermes.startup.latency` | latency_bucket, surface |
| `hermes.engagement.day.count` | active_minutes_bucket, active_profile_count_bucket, primary_model, primary_provider, surfaces_used_count |
| `hermes.engagement.surface_day.count` | active_minutes_bucket, surface |
| `hermes.feature_adoption.count` | days_since_install_bucket, feature |
| `hermes.feature_disabled.count` | event, kind, name, surface |
| `hermes.cache_break.count` | cause, model, provider |
| `hermes.loop_guard.count` | detector, model, provider, signal |
| `hermes.wasted_tokens.count` | model, provider, reason, tokens_bucket |
| `hermes.cron.run` | delivery_kind, duration_bucket, outcome |
| `hermes.gateway.reply_latency` | first_response_bucket, platform |
| `hermes.platform.health` | error_class, event, platform |
| `hermes.platform.delivery` | failure_class, outcome, platform |
| `hermes.process.exit` | crash_class, exit_kind, process_kind |
| `hermes.update.run` | apply_mode, duration_bucket, failed_stage, from_version_age_bucket, kind, outcome |
| `hermes.update.stage` | duration_bucket, outcome, stage |
| `hermes.provider_setup.count` | event, failure_class, provider, surface |
| `hermes.desktop.feature_use` | area |
| `hermes.desktop.friction` | detail, kind |
| `hermes.desktop.onboarding` | event, step |
| `hermes.desktop.mode_use` | active_minutes_bucket, bot_count_bucket, messages_sent_bucket, mode |
| `hermes.desktop.action_use` | action, count_bucket, via |
| `hermes.desktop.dislike` | direction, setting, signal, target |
| `hermes.install.snapshot` | (same as above — emitted on first run + daily) |

## 5. Dimension vocabularies (constants to know for rendering)

- **Buckets**: `size_bucket`: `0|1|2|3_to_5|6_to_10|11_to_25|26_to_100|101_to_500|gte_501`; `duration_bucket`/`latency_bucket`/`ttft_bucket`/`install_age_bucket`/`active_duration_bucket`: closed ms/s thresholds ending `lt_*|*_to_*|gte_*`; `count_bucket`: `0|1|2|3_to_5|6_to_10|gte_11`; `long_size_bucket` for schema sizes.
- **Provider/model names**: shipped provider ids + model ids; anything user-defined → `custom`.
- **Install snapshot** renders human table: ram_bucket (`16g_to_32g`), gpu_class (`intel|nvidia|amd|...`), main_provider, terminal_backend, counts of skills/plugins/profiles/MCP/cron/messaging.

## 6. Privacy invariants (what the GUI must respect)

1. All identifiers bucketed or catalog-closed; GUI should display buckets verbatim (no raw values exist in the DB beyond these).
2. `install_id` is profile-scoped and random; GUI may display it locally (it's yours) but the plugin must never send it anywhere.
3. The plugin reads ONLY. Never write to `metrics.sqlite3`, never touch `telemetry.*` config, never call the sender.
4. DB is live-written → read with `mode=ro` + `busy_timeout`, or copy to a scratch path when locked (observed on this machine).