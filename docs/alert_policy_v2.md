# Alert Policy V2 — Human-Centered Offline Contract

## Product contract

The frozen XGBoost scorer detects risk. Alert Policy v2 decides how to intervene. Label/event truth is never an engine input and is used only by offline evaluation.

- **L0 SILENT:** no active interruption.
- **L1 ADVISORY:** visual/UI state only. A persistent state is permitted; this is not a new alert on every telemetry row and does not repeatedly vibrate.
- **L2 WARNING:** short voice plus visual after elapsed-time persistence. It is subject to hysteresis, same-episode de-duplication, cooldown, DMS/idling gates, and the rolling advisory voice budget.
- **L3 CRITICAL:** very high score, configured rapid risk increase, or an explicit direct-critical input. It may bypass the L2 quota once, but never speaks repeatedly merely because critical state persists.

`any_intervention_recall` counts target episodes with L1/L2/L3. `voice_coverage` counts target episodes with emitted L2/L3 voice. High intervention coverage is desirable; high voice coverage is not an optimization objective.

## State and audit rules

The state key is exactly `(enabled_code, journey_code)`. `journeyCode` alone is unsafe because it can collide across vehicles. `enabledCode` identifies a vehicle, not a driver. One journey may be treated analytically as corresponding to one driver, but this is not verified identity or personalization.

The engine retains raw risk and applies a timestamp-aware EMA using elapsed seconds. L2 requires a configurable duration above its candidate band. Each level has an enter threshold and a lower exit threshold. An optional score slope in probability units per second can request L3; it is not causal evidence. Out-of-order timestamps within a composite trip are rejected.

Every decision records the composite identity, timestamp, raw/smoothed risk, slope, previous/requested/emitted level, modality, policy episode, idling/DMS/direct-critical flags, transition and intervention state, remaining L2 budget, suppression reason, policy version, and `is_mock`.

## Voice arbitration

The L2 budget is two emitted advisory voices in the preceding rolling 60 minutes per composite trip. New episodes inside cooldown are visual-only. One continuing episode emits at most one L2 voice. A later genuine L3 escalation can speak once despite a full L2 budget; repeated L3 state is still de-duplicated.

Organizer-defined idling means completely stationary while the engine remains on; low-speed movement is not idling. Offline replay uses exact anchor `vehicle_status == 2`, not speed inference. Predictive voice is normally suppressed during idling while visual state remains. Explicit direct-critical semantics are not blindly silenced.

DMS is an optional capability. When a live DMS is active, already issuing voice, and semantically overlaps the SafePilot warning, SafePilot retains visual state and records `suppression_reason="suppressed_by_dms"`. The competition offline data does not provide simultaneous DMS state, so this contract is tested only with records explicitly marked `is_mock=true`; no empirical DMS-effect claim is allowed.

## Episode evaluation

A target-risk episode is one continuous positive `label_any_core_risk_60s` anchor region within one composite trip. A zero anchor, trip change, or a gap beyond the configured continuity bound closes it. Trusted organizer onsets are linked when available so lead time and `timely_coverage_3s` use real timestamp semantics. Mock decisions/events are excluded.

Metrics include intervention recall; voice coverage; timely coverage; advisory, critical, total, and label-unmatched voice per active driving hour; voice precision; p25/median/p75 lead time; rolling 60-minute P95/maximum burden; transitions/hour; level distribution; idling statistics; and suppression counts. `offline_false_voice_per_driving_hour` means voice unmatched to Label V0 semantics, not an accident false-alarm rate. Phase 3C-2 `event_coverage` and related names remain unchanged in the backward-comparison block.

Phase 3C-4 audits lead time separately for first any intervention, first L1, first emitted voice, and first L2/L3 voice. The legacy p25/median/p75 keys still mean first any intervention. Voice/event lead uses the next explaining onset within the configured Label V0 horizon rather than an earlier event inside a merged positive region. Voice burden counts a genuine L2-to-L3 escalation as a distinct transition; the earlier policy-episode-collapsed count remains under an explicit backward-comparison key.

## Development and selection

All parameters in `config/alert_policy_v2.json` are policy candidates or provisional engineering constraints, not regulatory standards. Search uses late-September development data only and the frozen XGBoost bundle. November was previously opened and cannot be called fresh.

A candidate must pass every available required constraint before ranking. Feasible candidates are ordered lexicographically by timely coverage, intervention recall, lower offline false voice/hour, lower advisory voice/hour, higher median lead time, then lower chatter. Unavailable label-dependent metrics do not receive fabricated pass/fail results. If no policy is feasible, there is no winner and no target is relaxed.

The same selected parameters are reported overall, by anonymized vehicle, by the existing development/holdout vehicle assignment, and by Type 300/500/700. Type is a robustness slice only for policy optimization; subgroup thresholds and driver personalization are forbidden in this phase.

### Provisional Phase 3C-3 result

The reproducible 256-candidate late-September run returned `NO_FEASIBLE_POLICY`. No winner or deployable threshold is selected. The closest diagnostic configuration used L1/L2/L3 enter thresholds 0.20/0.60/0.80, exit thresholds 0.10/0.50/0.70, five-second EMA half-life, 20-second L2 persistence, 300-second L2 cooldown, and a 0.04-per-second L3 slope threshold. It passed intervention recall (0.918), three-second timely coverage (0.874), rolling advisory P95/maximum (1/2), median lead time (41 seconds), and one-L2-voice-per-episode. It failed advisory voice/hour (1.489 > 1.0), offline false voice/hour (0.454 > 0.25), and voice precision (0.700 < 0.80). This is a Pareto/violation reference only, not a selected policy.

Phase 3C-4 corrected total/critical voice counting: 63 actual level-specific voice transitions replace the old episode-collapsed count of 60. Corrected total/critical burden is 1.590/0.101 per driving hour; label-unmatched burden is 0.479/hour and precision is 0.698. Advisory voice remains 1.489/hour. These corrections do not create a feasible policy.

## Safety status

Policy v2 is provisional offline research. It does not authorize live voice, autonomous control, disciplinary use, regulatory claims, driver identity claims, or accident-reduction claims. Fresh-holdout validation, human-factors testing, live DMS integration evidence, and production session/exposure semantics remain pending.
