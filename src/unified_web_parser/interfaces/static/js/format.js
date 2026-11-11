import { fieldText, titleValue } from "../dataset.js";

export function safeUrl(value) {
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) &&
      !url.username &&
      !url.password
      ? url.href
      : null;
  } catch {
    return null;
  }
}

export function sourceUrl(record) {
  return safeUrl(record.data?.url) || safeUrl(record.provenance?.source_url);
}

export function imageUrl(record) {
  return safeUrl(record.img_url);
}

export function recordTitle(record, presentation) {
  return fieldText(titleValue(record, presentation));
}

export function hostName(value) {
  try {
    return new URL(value).hostname.replace(/^www\./, "");
  } catch {
    return "Джерело не вказане";
  }
}

export function label(key) {
  return key.replaceAll("_", " ");
}

export function duration(snapshot) {
  const start = Date.parse(snapshot.started_at);
  const finish = snapshot.finished_at
    ? Date.parse(snapshot.finished_at)
    : Date.now();
  if (!Number.isFinite(start) || !Number.isFinite(finish)) return "—";
  const seconds = Math.max(0, Math.floor((finish - start) / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}
