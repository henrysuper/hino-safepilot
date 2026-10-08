"use strict";
const assert = require("node:assert/strict");
require("../demo/reminders.js");
const api = globalThis.SafePilotReminders;

// Future outcomes must never change what the model would say at the prediction time.
const timeline = {
  points: [[0, 55, 1], [1 / 6, 60, 3], [2 / 6, 65, 2]],
  alerts: [{ minute: 0, event_label: "fatigue", lead_seconds: 30 }],
  events: [{ minute: 0.5, label: "疲勞" }],
};
assert.deepEqual(api.notifications(timeline, 0).map((n) => n.category), ["general"]);
assert.equal(api.notifications(timeline, 0)[0].text, "行駛風險升高，請留意路況。");
assert.equal(api.frameNotifications(timeline, 1).length, 0);
assert.equal(api.frameNotifications(timeline, 2)[0].escalation, true);

const scenario = {
  is_reminder_scenario: true,
  points: [[0, 20, 0], [1 / 6, 55, 1], [2 / 6, 55, 3]],
  detected_events: [{ minute: 1 / 6, event_code: "pcs" }],
};
assert.equal(api.notifications(scenario, 0).length, 0);
assert.deepEqual(api.frameNotifications(scenario, 1).map((n) => n.category), ["forward"]);
assert.equal(api.frameNotifications(scenario, 1)[0].text, "請注意前方，放慢車速，保持安全車距。");
assert.equal(api.frameNotifications(scenario, 2).length, 0);
assert.deepEqual(api.notifications({ ...scenario, is_reminder_scenario: false }, 1 / 6).map((n) => n.category), ["general"]);
for (const [code, category] of [["ldws", "lane"], ["distract", "attention"], ["phone", "attention"], ["fatigue", "fatigue"]]) {
  const result = api.frameNotifications({ ...scenario, detected_events: [{ minute: 1 / 6, event_code: code }] }, 1);
  assert.equal(result[0].category, category);
  assert.equal(result[0].source, "detected_event");
}
// Braking alone and unknown codes must not create an event-specific warning.
for (const code of ["decelerate", "unknown"]) {
  assert.deepEqual(api.frameNotifications({ ...scenario, detected_events: [{ minute: 1 / 6, event_code: code }] }, 1).map((n) => n.category), ["general"]);
}
console.log("Reminder routing, no-future-outcome use, and voice arbitration checks passed.");

for (const code of ["pcs", "ldws", "distract", "fatigue", null]) {
  const scripted = api.makeScenario({ event_code: code, label: "Synthetic" });
  assert.equal(scripted.points[0][1], 25);
  assert.equal(scripted.points[1][1], 40);
  assert.equal(scripted.points[2][1], 50);
  assert.equal(api.notifications(scripted, 19 / 60).length, 0);
  const fired = api.notifications(scripted, 20 / 60);
  assert.equal(fired.length, 1);
  assert.equal(fired[0].category, code ? ({ pcs: "forward", ldws: "lane", distract: "attention", fatigue: "fatigue" })[code] : "general");
}
const below = { is_reminder_scenario: true, points: [[0, 25, 0], [1 / 6, 49, 0], [2 / 6, 50, 0]], detected_events: [{minute: 0, event_code: "pcs"}] };
assert.equal(api.notifications(below, 1 / 6).length, 0);
assert.equal(api.notifications(below, 2 / 6)[0].minute, 2 / 6);
console.log("All five scenarios stay silent below 50% and trigger at 50%.");
