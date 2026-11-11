// Pure presentation helpers shared by cards, the table and generated filters.
export function fieldText(value, field = {}) {
  if (value === null || value === undefined) return "Не вказано";
  let text;
  if (typeof value === "boolean") text = value ? "Так" : "Ні";
  else if (typeof value === "object") text = JSON.stringify(value);
  else text = String(value);
  return field.unit && typeof value === "number"
    ? `${text} ${field.unit}`
    : text;
}

export function valueKey(value) {
  return JSON.stringify(value);
}

export function dataValue(record, key) {
  const data = record.data || {};
  return Object.hasOwn(data, key) ? data[key] : undefined;
}

export function titleValue(record, presentation) {
  const titles = (presentation?.fields || []).filter(
    (field) => field.role === "title",
  );
  for (const field of titles) {
    const value = dataValue(record, field.key);
    if (value !== null && value !== undefined && String(value).trim())
      return value;
  }
  return (
    dataValue(record, "title") ??
    dataValue(record, "name") ??
    record.record_id ??
    "Запис"
  );
}

export function displayColumns(presentation, selected = null) {
  const fields = presentation?.fields || [];
  const wanted = selected ?? presentation?.columns ?? [];
  const byKey = new Map(fields.map((field) => [field.key, field]));
  return [...new Set(wanted)]
    .map((key) => byKey.get(key))
    .filter((field) => field && !["title", "image"].includes(field.role));
}

export function isActiveFilter(filter) {
  return Boolean(
    filter &&
    ((filter.missing && filter.missing !== "any") ||
      filter.value ||
      filter.text ||
      (filter.min !== "" && filter.min !== undefined) ||
      (filter.max !== "" && filter.max !== undefined)),
  );
}

function boundValue(value, field, upper = false) {
  if (field.format === "date" || field.format === "date-time") {
    const time = Date.parse(value);
    return upper && /^\d{4}-\d{2}-\d{2}$/.test(value)
      ? time + 86400000 - 1
      : time;
  }
  return typeof value === "number" ? value : Number(value);
}

export function matchesFilters(record, fields, filters) {
  return fields.every((field) => {
    const filter = filters.get(field.key);
    if (!isActiveFilter(filter)) return true;
    const value = dataValue(record, field.key);
    const missing = value === null || value === undefined;
    if (filter.missing === "missing") return missing;
    if (filter.missing === "known" && missing) return false;
    const constrained = Boolean(
      filter.value ||
      filter.text ||
      (filter.min !== "" && filter.min !== undefined) ||
      (filter.max !== "" && filter.max !== undefined),
    );
    if (!constrained) return true;
    if (missing) return false;
    if (filter.value && valueKey(value) !== filter.value) return false;
    if (
      filter.text &&
      !fieldText(value)
        .toLocaleLowerCase("uk-UA")
        .includes(filter.text.toLocaleLowerCase("uk-UA"))
    )
      return false;
    if (field.filter === "range") {
      const temporal = ["date", "date-time"].includes(field.format);
      if (!temporal && (typeof value !== "number" || !Number.isFinite(value)))
        return false;
      const actual = boundValue(value, field);
      if (!Number.isFinite(actual)) return false;
      for (const [key, upper] of [
        ["min", false],
        ["max", true],
      ]) {
        if (filter[key] === "" || filter[key] === undefined) continue;
        const boundary = boundValue(filter[key], field, upper);
        if (!Number.isFinite(boundary)) return false;
        if (upper ? actual > boundary : actual < boundary) return false;
      }
    }
    return true;
  });
}
