"use strict";
const assert = require("node:assert/strict");
require("../demo/playback.js");
const api = globalThis.SafePilotPlayback;
const points = [[0, 25], [10 / 60, 40], [30 / 60, 50]];
let seconds = 0;
for (let expected = 1; expected <= 9; expected += 1) {
  seconds = api.advance(seconds, 1000, 1, 30);
  assert.equal(seconds, expected);
  assert.equal(api.indexAt(points, seconds), 0);
}
assert.equal(api.indexAt(points, 10), 1);
assert.equal(api.indexAt(points, 29), 1); // A gap must not reveal the next prediction early.
assert.equal(api.indexAt(points, 30), 2);
assert.equal(api.advance(12, 1000, 1, 30), 13); // Resume preserves second-level progress.
assert.equal(api.advance(0, 1000, 30, 30), 30);
assert.equal(api.advance(29, 1000, 10, 30), 30);
assert.equal(api.advance(5, -1000, 1, 30), 5);
const fractional = [[4.03, 25], [4.17, 40]];
assert.equal(api.indexAt(fractional, 250), 0);
assert.equal(api.indexAt(fractional, 250.2), 1);
assert.equal(api.bounds(fractional).start, 241.8);
console.log("Second-level timing, speed, resume, end clamping and irregular anchors passed.");
