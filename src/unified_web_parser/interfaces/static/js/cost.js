import { ACTIVE } from "./state.js";

/** Provider-reported costs only: absence or incomplete billing is never free usage. */
export function costSummary(run) {
  if (!run) return { hidden: true, value: "—", note: "", pending: false };
  const usage = { ...(run.report?.ai || {}), ...(run.ai || {}) };
  const valid = (value) => Number.isFinite(value) && value >= 0;
  const legacy = valid(usage.cost_usd) ? usage.cost_usd : undefined;
  const known = valid(usage.known_cost_usd) ? usage.known_cost_usd : legacy;
  const inFlight =
    Number.isFinite(usage.in_flight_calls) && usage.in_flight_calls > 0;
  const unpriced =
    Number.isFinite(usage.cost_pending_calls) && usage.cost_pending_calls > 0;
  const complete =
    !inFlight &&
    !unpriced &&
    (usage.cost_complete === true ||
      (usage.cost_complete === undefined && legacy !== undefined));
  const active = ACTIVE.has(run.status);
  let value = "—";
  if (known > 0) {
    const amount =
      known < 0.00001
        ? new Intl.NumberFormat("en-US", {
            maximumSignificantDigits: 3,
            useGrouping: false,
          }).format(known)
        : known.toFixed(5);
    value = `$${amount}`;
  } else if (known === 0 && complete) value = "$0.00";
  let note;
  if (complete && known !== undefined) note = "За даними провайдера";
  else if (known > 0)
    note = active ? "Часткова сума · уточнюється" : "Сума неповна";
  else
    note = active ? "Очікуємо дані про вартість" : "Вартість не підтверджена";
  return {
    hidden: false,
    value,
    note,
    pending: !complete || known === undefined,
    label: active ? "Витрачено" : "Вартість запуску",
  };
}
