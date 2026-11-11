import test from "node:test";
import assert from "node:assert/strict";
import {
  dataValue,
  displayColumns,
  fieldText,
  isActiveFilter,
  matchesFilters,
  titleValue,
  valueKey,
} from "../../src/unified_web_parser/interfaces/static/dataset.js";

const fields = [
  { key: "price", type: "number", filter: "range" },
  { key: "mdm", type: "boolean", filter: "boolean" },
  { key: "category", type: "string", filter: "select" },
  { key: "published", type: "string", format: "date-time", filter: "range" },
  { key: "tags", type: "array", filter: "text" },
];
const matches = (data, filters) =>
  matchesFilters({ data }, fields, new Map(Object.entries(filters)));

test("numeric bounds combine inclusively and preserve zero", () => {
  assert(matches({ price: 0 }, { price: { min: "0", max: "0" } }));
  assert(!matches({ price: 1 }, { price: { min: "0", max: "0" } }));
  assert(!matches({ price: "0" }, { price: { min: "0" } }));
  assert(!matches({ price: null }, { price: { max: "100" } }));
  assert(matches({ price: null }, {}));
});
test("false, string false and unknown are distinct", () => {
  assert(matches({ mdm: false }, { mdm: { value: valueKey(false) } }));
  assert(!matches({ mdm: "false" }, { mdm: { value: valueKey(false) } }));
  assert(!matches({ mdm: null }, { mdm: { value: valueKey(false) } }));
  assert(matches({ mdm: null }, { mdm: { missing: "missing" } }));
  assert(matches({}, { mdm: { missing: "missing" } }));
  assert(!matches({ mdm: false }, { mdm: { missing: "missing" } }));
  assert(matches({ mdm: false }, { mdm: { missing: "known" } }));
});
test("independent filters are ANDed; missing-only overrides value constraints", () => {
  const filters = { price: { max: "100" }, category: { value: valueKey("A") } };
  assert(matches({ price: 50, category: "A" }, filters));
  assert(!matches({ price: 150, category: "A" }, filters));
  assert(!matches({ price: 50, category: "B" }, filters));
  assert(matches({}, { price: { max: "100", missing: "missing" } }));
});
test("date range includes the whole last day and rejects malformed values", () => {
  const filters = { published: { min: "2026-10-21", max: "2026-10-23" } };
  assert(matches({ published: "2026-10-23T23:59:59Z" }, filters));
  assert(!matches({ published: "2026-10-24T00:00:00Z" }, filters));
  assert(!matches({ published: "not a date" }, filters));
  assert(!matches({ published: null }, filters));
});
test("array text searches actual array contents without inventing membership facets", () => {
  assert(matches({ tags: ["Київ", "remote"] }, { tags: { text: "КИЇВ" } }));
  assert(!matches({ tags: [] }, { tags: { text: "Київ" } }));
});
test("display choices ignore unknown keys and support an intentionally empty choice", () => {
  const presentation = {
    fields: [
      { key: "title", role: "title" },
      { key: "price", role: "attribute" },
    ],
    columns: ["title", "price", "bad", "price"],
  };
  assert.deepEqual(
    displayColumns(presentation).map((field) => field.key),
    ["price"],
  );
  assert.deepEqual(displayColumns(presentation, []), []);
});
test("formatting retains unknowns and adds units only to numeric values", () => {
  assert.equal(fieldText(null), "Не вказано");
  assert.equal(fieldText(false), "Ні");
  assert.equal(fieldText(0, { unit: "GB" }), "0 GB");
  assert.equal(fieldText("0", { unit: "GB" }), "0");
  assert.equal(isActiveFilter({ min: "0" }), true);
  assert.equal(isActiveFilter({ missing: "any", min: "", max: "" }), false);
});

test("manual schema title roles remain visible even with a custom field name", () => {
  const presentation = { fields: [{ key: "headline", role: "title" }] };
  assert.equal(
    titleValue(
      { record_id: "id", data: { headline: "A vacancy" } },
      presentation,
    ),
    "A vacancy",
  );
  assert.equal(
    titleValue(
      { record_id: "id", data: { headline: null, name: "Fallback" } },
      presentation,
    ),
    "Fallback",
  );
});

test("arbitrary schema keys use only observed own properties", () => {
  for (const key of ["constructor", "__proto__", "toString"]) {
    const fields = [{ key, filter: "text" }];
    const filters = new Map([[key, { missing: "missing" }]]);
    assert.equal(dataValue({ data: {} }, key), undefined);
    assert(matchesFilters({ data: {} }, fields, filters));
    const record = { data: JSON.parse(`{"${key}":false}`) };
    assert.equal(dataValue(record, key), false);
    assert(!matchesFilters(record, fields, filters));
  }
});
