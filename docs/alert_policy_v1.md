# Alert Policy V1 — Experimental Offline Contract

Risk prediction and speech decisions are separate. The model returns a calibrated probability; `AlertPolicyEngine` consumes the ordered probability stream and owns state. Event truth is used only later by offline evaluation.

## State contract

- `NORMAL`: speak once when `p >= T_enter`, then enter `ACTIVE`.
- `ACTIVE`: do not repeat during continuous high risk. During the cooldown, one escalation may speak when `p >= reference + delta`; it updates the reference and cannot occur again before reset.
- Cooldown expiry alone never speaks.
- Hysteresis: two consecutive anchors with `p < T_reset` return the episode to `NORMAL`.
- Per-trip state is isolated. `AlertDecision` is a separate typed application contract from `RiskPredictionResult`.

## Development grid

- Entry thresholds: 0.30–0.75, including 0.60.
- Cooldowns: 30, 50, 60, 90 seconds.
- Escalation delta: 0.10, 0.15, 0.20.
- Reset margin: 0.10 or 0.15 below entry; two consecutive anchors.

The high-safety, balanced, and low-burden points are Pareto research candidates, not business approvals. Threshold 0.60 is also evaluated under no cooldown, simple 50-second cooldown, and revised V1 for an explicit like-for-like comparison.

## Matching and metrics

Alerts are matched chronologically to the earliest unmatched core event onset in the same trip within 60 seconds. One alert matches at most one event and one event at most one alert. Unmatched alerts are false alerts; additional alerts preceding an already matched event are duplicate diagnostics.

Primary product metrics are event coverage, false alerts per driving hour, and lead time. Alert precision, voice alerts/hour, duplicate alerts/event, and maximum alerts in a rolling ten-minute window are also reported. The eligible-anchor exposure denominator is primary for comparability; a telemetry-interval-weighted denominator is explicitly experimental.

## Safety status

No threshold, cooldown, escalation rule, driving-hour denominator, or acceptable speech budget has been validated by HINO or safety-outcome data. Policy V1 is offline only and cannot be wired to a production warning path.

