import test from "node:test";
import assert from "node:assert/strict";
import { costSummary } from "../../src/unified_web_parser/interfaces/static/js/cost.js";

const running = (ai) => costSummary({ status: "running", ai });

test("unknown and unpriced calls never appear as free usage", () => {
  for (const ai of [
    {},
    { calls: 1 },
    { known_cost_usd: 0, cost_complete: false, cost_pending_calls: 1 },
  ]) {
    const cost = running(ai);
    assert.equal(cost.value, "—");
    assert.equal(cost.pending, true);
  }
  assert.equal(costSummary(null).hidden, true);
});

test("known running cost grows and remains visible while later billing is pending", () => {
  const first = running({
    calls: 1,
    known_cost_usd: 0.0025,
    cost_complete: true,
  });
  const later = running({
    calls: 3,
    known_cost_usd: 0.0075,
    cost_complete: false,
    cost_pending_calls: 1,
  });
  assert.equal(first.value, "$0.00250");
  assert.equal(first.pending, false);
  assert.equal(later.value, "$0.00750");
  assert.equal(later.pending, true);
  assert.match(later.note, /Часткова/);
});

test("explicit zero cost and cache-only runs show a measured zero without cache rebilling", () => {
  const cost = running({
    calls: 0,
    cache_hits: 8,
    known_cost_usd: 0,
    cost_complete: true,
    stages: { web_search: { original_cached_cost_usd: 2 } },
  });
  assert.equal(cost.value, "$0.00");
  assert.equal(cost.pending, false);
});

test("in-flight attempts keep cost provisional even when preceding billing was complete", () => {
  const cost = running({
    known_cost_usd: 0.005,
    cost_complete: true,
    in_flight_calls: 1,
  });
  assert.equal(cost.value, "$0.00500");
  assert.equal(cost.pending, true);
});

test("stopped and completed runs retain their actual known sums", () => {
  const report = {
    ai: { known_cost_usd: 0.0075, cost_complete: false, cost_pending_calls: 1 },
  };
  const cost = costSummary({ status: "stopped", report });
  assert.equal(cost.value, "$0.00750");
  assert.equal(cost.label, "Вартість запуску");
  assert.equal(cost.note, "Сума неповна");
  const final = costSummary({
    status: "success",
    ai: { known_cost_usd: 0.009, cost_complete: true },
  });
  assert.equal(final.value, "$0.00900");
  assert.equal(final.pending, false);
});

test("legacy measured totals remain readable and malformed totals stay unknown", () => {
  assert.equal(running({ cost_usd: 0.5 }).value, "$0.50000");
  for (const value of ["0.5", false, -1, Infinity, NaN]) {
    assert.equal(running({ known_cost_usd: value }).value, "—");
  }
});

test("positive micro-costs are never rounded to a false zero", () => {
  const cost = running({ known_cost_usd: 0.0000000123, cost_complete: true });
  assert.equal(cost.value, "$0.0000000123");
  assert.notEqual(Number(cost.value.slice(1)), 0);
});
