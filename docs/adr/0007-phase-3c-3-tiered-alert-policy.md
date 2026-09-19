# ADR 0007: Phase 3C-3 Human-Centered Tiered Alert Policy

- Status: Accepted as Provisional Offline Experiment
- Date: 2026-09-05

## Context

Phase 3C-2 established a useful XGBoost ranking result but also a product conflict: lowering warning frequency substantially reduced Risk Coverage. The model score, risk intervention, and spoken warning were coupled too tightly. November was already viewed in Phases 3C-1/3C-2 and is not a fresh holdout. The fleet contains only about 20 vehicles, so unseen-vehicle model generalization is diagnosed but not addressed here.

The organizer FAQ dated 2026-09-02 confirms that `enabledCode` is a vehicle identifier, not a driver identity; `journeyCode` can collide across vehicles; `(enabledCode, journeyCode)` is the journey key; actual driver identity/rotation is unavailable; idling is complete stationary state with the engine on; Type 300/500/700 denotes the HINO series; competition `fenceId` is empty; and fatigue/distraction DMS can issue Chinese voice and lamp warnings.

## Decision

1. Preserve the frozen XGBoost model and Label V0. Policy v2 consumes its raw score and cannot alter either.
2. Separate any intervention (L1/L2/L3) from voice (L2/L3). Voice coverage is reported, not maximized.
3. Use deterministic L0 silent, L1 visual advisory, L2 persistent warning, and L3 critical escalation states. Thresholds are searched on development data rather than treated as calibrated probabilities or fixed at arbitrary values.
4. Isolate all EMA, persistence, hysteresis, episode, cooldown, and rolling-budget state by `(enabledCode, journeyCode)`.
5. Gate predictive voice during organizer-defined idling while retaining visual intervention. A direct critical semantic can remain audible. DMS arbitration suppresses overlapping SafePilot voice as `suppressed_by_dms` while retaining visual state.
6. Treat DMS availability as a capability flag. Because simultaneous DMS state is absent offline, its arbitration evidence is synthetic and every such fixture is tagged `is_mock=true`; mock rows are excluded from empirical optimization and HINO metrics.
7. Build target-risk episodes as continuous positive Label V0 anchor regions within one composite trip. Retain Phase 3C-2 event coverage unchanged for backward comparison and add explicit episode-level metrics.
8. Apply provisional engineering constraints before lexicographic ranking. If no candidate passes every available required constraint, select no winner and publish the Pareto/violation table without relaxing targets.
9. Search late September only. November remains a known-holdout comparative benchmark; genuinely fresh holdout validation is pending.
10. Apply one global policy to vehicle, existing seen/unseen assignment, and Type slices. Do not introduce per-vehicle, per-Type, driver-personalized, or unseen-vehicle model tuning.

## Consequences

Policy v2 is deterministic, auditable, model-independent, and capable of high visual intervention coverage without requiring equivalent voice coverage. L2 voice is capped at two per rolling 60 minutes per composite trip; L3 may bypass that advisory quota once per episode but remains de-duplicated. Risk slope is only an escalation signal and has no causal interpretation.

All empirical conclusions remain provisional. Offline false voice is label-relative rather than a real-world accident false-alarm rate. No verified driver identity exists, DMS overlap is not empirically measurable, and no fresh holdout remains.

The 256-candidate late-September search found no feasible policy, so Phase 3C-3 selects no winner. The closest non-feasible diagnostic configuration achieved 0.918 intervention recall and 0.874 timely coverage, but failed three required constraints: 1.489 advisory voices/driving hour, 0.454 offline false voices/driving hour, and 0.700 voice precision. Its rolling advisory P95/maximum were 1/2 and median lead time was 41 seconds. These figures do not justify deployment or target relaxation.

Phase 3C-4 subsequently audited the evaluator. The 41-second value is first-any-intervention lead, not voice lead. Total/critical burden now counts a genuine L3 escalation following L2 as a distinct level transition. The regenerated 256-candidate report remains `NO_FEASIBLE_POLICY`; ADR 0008 records corrected definitions and reference metrics.
