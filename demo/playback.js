"use strict";

(function (root) {
  function bounds(points) {
    return { start: points[0][0] * 60, end: points.at(-1)[0] * 60 };
  }

  function advance(seconds, elapsedMs, speed, end) {
    return Math.min(end, seconds + Math.max(0, elapsedMs) / 1000 * speed);
  }

  function indexAt(points, seconds) {
    let left = 0, right = points.length - 1;
    while (left < right) {
      const middle = Math.ceil((left + right) / 2);
      if (points[middle][0] * 60 <= seconds + 1e-8) left = middle;
      else right = middle - 1;
    }
    return left;
  }

  root.SafePilotPlayback = Object.freeze({ bounds, advance, indexAt });
})(globalThis);
