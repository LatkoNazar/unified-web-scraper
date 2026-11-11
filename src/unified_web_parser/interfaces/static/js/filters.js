import { $, element } from "./dom.js";
import { label } from "./format.js";
import { fieldText, isActiveFilter, valueKey } from "../dataset.js";

export function createDatasetControls(state, { onChange }) {
  function resetDatasetSelection() {
    state.filters.clear();
    state.columns = null;
    $("facet-controls").replaceChildren();
    $("column-controls").replaceChildren();
  }

  function updateFilter(key, property, value) {
    const current = state.filters.get(key) || {
      missing: "any",
      value: "",
      text: "",
      min: "",
      max: "",
    };
    state.filters.set(key, { ...current, [property]: value });
    onChange();
  }

  function filterSelect(options, selected = "") {
    const select = element("select");
    for (const [value, text] of options) {
      const option = element("option", "", text);
      option.value = value;
      select.append(option);
    }
    select.value = selected;
    return select;
  }

  function buildFacet(field) {
    const node = element("fieldset", "facet-control");
    node.dataset.field = field.key;
    node.dataset.signature = JSON.stringify([
      field.type,
      field.filter,
      field.format,
    ]);
    const legend = element("legend", "", field.label || label(field.key));
    legend.title = `${field.key} · ${field.type}`;
    node.append(legend, element("p", "facet-stats field-help"));
    const inputs = element("div", "facet-inputs");
    const filter = state.filters.get(field.key) || {};
    if (field.filter === "range") {
      for (const [property, text] of [
        ["min", "Від"],
        ["max", "До"],
      ]) {
        const wrap = element("label", "range-bound", text);
        const input = element("input");
        input.type = ["date", "date-time"].includes(field.format)
          ? "date"
          : "number";
        if (input.type === "number")
          input.step = field.type === "integer" ? "1" : "any";
        input.dataset.bound = property;
        input.setAttribute(
          "aria-label",
          `${field.label || label(field.key)}: ${text.toLowerCase()}`,
        );
        input.value = filter[property] || "";
        input.addEventListener("input", () =>
          updateFilter(field.key, property, input.value),
        );
        wrap.append(input);
        inputs.append(wrap);
      }
    } else if (["select", "boolean"].includes(field.filter)) {
      const select = filterSelect([["", "Усі значення"]]);
      select.className = "facet-values";
      select.setAttribute("aria-label", field.label || label(field.key));
      select.addEventListener("change", () =>
        updateFilter(field.key, "value", select.value),
      );
      inputs.append(select);
    } else {
      const input = element("input");
      input.type = "search";
      input.placeholder = "Містить…";
      input.value = filter.text || "";
      input.setAttribute("aria-label", field.label || label(field.key));
      input.addEventListener("input", () =>
        updateFilter(field.key, "text", input.value),
      );
      inputs.append(input);
    }
    const missing = filterSelect(
      [
        ["any", "Усі записи"],
        ["known", "Лише зі значенням"],
        ["missing", "Лише не вказані"],
      ],
      filter.missing || "any",
    );
    missing.className = "facet-missing";
    missing.setAttribute(
      "aria-label",
      `${field.label || label(field.key)}: наявність даних`,
    );
    missing.addEventListener("change", () =>
      updateFilter(field.key, "missing", missing.value),
    );
    node.append(inputs, missing);
    return node;
  }

  function renderDatasetControls() {
    const presentation = state.current?.presentation;
    const fields = presentation?.fields || [];
    $("dataset-controls").hidden = !fields.length;
    $("schema-note").textContent =
      presentation?.status === "ready"
        ? `Типізовано характеристик: ${fields.length} · Записів: ${presentation.sample_size}`
        : "Характеристики та діапазони уточнюються під час збору";
    $("view-cards").setAttribute(
      "aria-pressed",
      String(state.view === "cards"),
    );
    $("view-table").setAttribute(
      "aria-pressed",
      String(state.view === "table"),
    );
    $("active-filter-count").textContent =
      [...state.filters.values()].filter(isActiveFilter).length || "";
    const facets = $("facet-controls");
    const existing = new Map(
      [...facets.children].map((node) => [node.dataset.field, node]),
    );
    const filterable = fields.filter(
      (field) => field.filter && field.filter !== "none",
    );
    for (let field of filterable) {
      // A selected category remains an exact typed choice even if later values
      // take the field above the select cardinality limit.
      if (field.filter === "text" && state.filters.get(field.key)?.value) {
        field = { ...field, filter: "select" };
      }
      const signature = JSON.stringify([
        field.type,
        field.filter,
        field.format,
      ]);
      let node = existing.get(field.key);
      if (!node || node.dataset.signature !== signature) {
        if (node && field.filter !== "range") {
          const current = state.filters.get(field.key);
          if (current)
            state.filters.set(field.key, { ...current, min: "", max: "" });
        }
        const next = buildFacet(field);
        if (node) node.replaceWith(next);
        else facets.append(next);
        node = next;
      }
      existing.delete(field.key);
      const stats = field.stats || {};
      node.querySelector("legend").textContent =
        field.label || label(field.key);
      node.querySelector(".facet-stats").textContent =
        `${field.type}${field.unit ? ` · ${field.unit}` : ""} · Значень: ${stats.count || 0} · Не вказано: ${stats.missing || 0}` +
        (field.filter === "range"
          ? ` · Межі: ${fieldText(stats.min, field)} — ${fieldText(stats.max, field)}`
          : "");
      for (const input of node.querySelectorAll("[data-bound]")) {
        const value = stats[input.dataset.bound] ?? "";
        input.placeholder = `${value}${field.unit ? ` ${field.unit}` : ""}`;
        input.title = `Межі вибірки: ${stats.min ?? "—"} — ${stats.max ?? "—"}`;
      }
      const select = node.querySelector(".facet-values");
      if (select) {
        const options = [
          ["", "Усі значення"],
          ...(stats.values || []).map((item) => [
            valueKey(item.value),
            `${fieldText(item.value, field)} (${item.count})`,
          ]),
        ];
        const selected = state.filters.get(field.key)?.value || "";
        if (selected && !options.some(([value]) => value === selected))
          options.push([selected, "Обране значення"]);
        const fingerprint = JSON.stringify(options);
        if (select.dataset.options !== fingerprint) {
          select.replaceChildren(...filterSelect(options, selected).children);
          select.value = selected;
          select.dataset.options = fingerprint;
        }
      }
      const missingOnly = state.filters.get(field.key)?.missing === "missing";
      for (const input of node.querySelectorAll(
        ".facet-inputs input,.facet-inputs select",
      ))
        input.disabled = missingOnly;
    }
    for (const node of existing.values()) node.remove();
    const columns = $("column-controls");
    const known = new Map(
      [...columns.children].map((node) => [node.dataset.field, node]),
    );
    const selected = new Set((state.columns ?? presentation?.columns) || []);
    for (const field of fields.filter(
      (item) => !["title", "image"].includes(item.role),
    )) {
      let node = known.get(field.key);
      if (!node) {
        node = element("label", "column-choice");
        node.dataset.field = field.key;
        const checkbox = element("input");
        checkbox.type = "checkbox";
        checkbox.addEventListener("change", () => {
          state.columns = [...columns.querySelectorAll("input:checked")].map(
            (input) => input.parentElement.dataset.field,
          );
          onChange();
        });
        node.append(checkbox, element("span"));
        columns.append(node);
      }
      node.querySelector("input").checked = selected.has(field.key);
      node.querySelector("span").textContent = field.label || label(field.key);
      known.delete(field.key);
    }
    for (const node of known.values()) node.remove();
  }

  $("reset-filters").addEventListener("click", () => {
    state.filters.clear();
    $("result-filter").value = "";
    $("facet-controls").replaceChildren();
    onChange();
  });
  $("auto-columns").addEventListener("click", () => {
    state.columns = null;
    onChange();
  });

  return { resetDatasetSelection, renderDatasetControls };
}
