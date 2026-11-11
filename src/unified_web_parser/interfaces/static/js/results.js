import { $, element } from "./dom.js";
import { ACTIVE } from "./state.js";
import { sourceUrl, imageUrl, recordTitle, hostName, label } from "./format.js";
import {
  dataValue,
  displayColumns,
  fieldText,
  isActiveFilter,
  matchesFilters,
} from "../dataset.js";
import { createDatasetControls } from "./filters.js";

export function createDatasetView(state, { showRecord }) {
  const { resetDatasetSelection, renderDatasetControls } =
    createDatasetControls(state, { onChange: renderResults });

  function card(record) {
    const article = element("article", "record-card");
    article.dataset.recordId = record.record_id;
    const media = element("div", "record-media");
    const placeholder = element("div", "image-placeholder");
    placeholder.append(
      element("span", "", "▧"),
      element("span", "", "Без зображення"),
    );
    placeholder.setAttribute("aria-hidden", "true");
    media.append(placeholder);
    const image = imageUrl(record);
    article.classList.toggle("has-image", Boolean(image));
    if (image) {
      const img = element("img");
      img.alt = recordTitle(record, state.current?.presentation);
      img.loading = "lazy";
      img.decoding = "async";
      img.referrerPolicy = "no-referrer";
      img.addEventListener(
        "error",
        () => {
          img.hidden = true;
          placeholder.hidden = false;
          article.classList.remove("has-image");
        },
        { once: true },
      );
      img.addEventListener(
        "load",
        () => {
          placeholder.hidden = true;
        },
        { once: true },
      );
      img.src = image;
      media.append(img);
    }
    const url = sourceUrl(record);
    media.append(element("span", "record-source", hostName(url)));
    const body = element("div", "record-body");
    body.append(
      element(
        "h3",
        "record-title",
        recordTitle(record, state.current?.presentation),
      ),
    );
    const fields = element("dl", "record-fields");
    const selected = displayColumns(state.current?.presentation, state.columns);
    const entries = state.current?.presentation
      ? selected.map((field) => [
          field.key,
          dataValue(record, field.key),
          field,
        ])
      : Object.entries(record.data || {}).filter(
          ([key]) =>
            ![
              "title",
              "name",
              "url",
              "source_url",
              "img_url",
              "image",
              "description",
            ].includes(key),
        );
    for (const [key, value, field] of entries.slice(
      0,
      selected.length ? 8 : 4,
    )) {
      const row = element("div");
      const display = fieldText(value, field);
      const term = element("dt", "", field?.label || label(key));
      term.title = key;
      const definition = element("dd", "", display);
      definition.title = display;
      row.append(term, definition);
      fields.append(row);
    }
    if (!entries.length && record.data?.description) {
      const row = element("div");
      row.append(
        element("dd", "", fieldText(record.data.description).slice(0, 160)),
      );
      fields.append(row);
    }
    const footer = element("div", "record-footer");
    if (url) {
      const link = element("a", "", "Відкрити джерело ↗");
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      footer.append(link);
    } else footer.append(element("span", "", "Без посилання"));
    const details = element("button", "", "Деталі →");
    details.type = "button";
    details.addEventListener("click", () => showRecord(record));
    footer.append(details);
    body.append(fields, footer);
    article.append(media, body);
    return article;
  }

  function renderResults() {
    const records = state.current?.records || [];
    document.querySelector(".result-actions").hidden = !records.length;
    renderDatasetControls();
    const query = $("result-filter").value.trim().toLocaleLowerCase("uk-UA");
    const fields = state.current?.presentation?.fields || [];
    const filtered = records.filter(
      (record) =>
        (!query ||
          JSON.stringify(record.data || {})
            .toLocaleLowerCase("uk-UA")
            .includes(query)) &&
        matchesFilters(record, fields, state.filters),
    );
    const hasFilters =
      query || [...state.filters.values()].some(isActiveFilter);
    $("result-count").textContent = hasFilters
      ? `${filtered.length} / ${records.length}`
      : records.length;
    $("empty-state").hidden = records.length > 0;
    $("no-matches").hidden = !records.length || filtered.length > 0;
    const emptyHeading = $("empty-state").querySelector("h3");
    const emptyText = $("empty-state").querySelector("p");
    if (state.current) {
      const active = ACTIVE.has(state.current.status);
      emptyHeading.textContent = active
        ? "Шукаємо перші записи…"
        : "У цьому запуску записів немає";
      emptyText.textContent = active
        ? "Нові записи з’являтимуться тут автоматично."
        : "Перегляньте хід виконання: там є причини, помилки та досягнуті ліміти.";
    } else {
      emptyHeading.textContent = "Записів ще немає";
      emptyText.textContent = "Опишіть запит і почніть збір.";
    }
    const run = state.current;
    $("dataset-note").textContent = !run
      ? "Нові записи з’являтимуться тут під час збору."
      : run.persisted
        ? "Збережений датасет цього запуску."
        : ACTIVE.has(run.status)
          ? "Попередній перегляд наживо. Датасет зберігається наприкінці збору."
          : "Попередній перегляд. Ці записи не підтверджені як збережені.";
    // Preserve loaded images and focus when another progress snapshot has unchanged records.
    const root = $("results");
    root.hidden = state.view === "table";
    $("result-table-wrapper").hidden =
      state.view !== "table" || !filtered.length;
    const existing = new Map(
      [...root.children].map((node) => [node.dataset.recordId, node]),
    );
    const nodes = filtered.map((record) => {
      const fingerprint = JSON.stringify([
        record,
        displayColumns(state.current?.presentation, state.columns).map(
          ({ key, label, unit }) => ({ key, label, unit }),
        ),
      ]);
      const old = existing.get(record.record_id);
      if (old?.dataset.fingerprint === fingerprint) return old;
      const node = card(record);
      node.dataset.fingerprint = fingerprint;
      return node;
    });
    nodes.forEach((node, index) => {
      if (root.children[index] !== node)
        root.insertBefore(node, root.children[index] || null);
    });
    while (root.children.length > nodes.length) root.lastElementChild.remove();
    if (state.view === "table") renderTable(filtered);
  }

  function renderTable(records) {
    const fields = displayColumns(state.current?.presentation, state.columns);
    const table = $("result-table");
    const columnsFingerprint = JSON.stringify(
      fields.map(({ key, label, type, unit, format }) => ({
        key,
        label,
        type,
        unit,
        format,
      })),
    );
    const head = table.querySelector("thead");
    if (head.dataset.fingerprint !== columnsFingerprint) {
      const header = element("tr");
      for (const name of [
        "Запис",
        ...fields.map((field) => field.label || label(field.key)),
        "Дії",
      ]) {
        const cell = element("th", "", name);
        cell.scope = "col";
        header.append(cell);
      }
      head.replaceChildren(header);
      head.dataset.fingerprint = columnsFingerprint;
    }
    const body = table.querySelector("tbody");
    const existing = new Map(
      [...body.children].map((row) => [row.dataset.recordId, row]),
    );
    const rows = records.map((record) => {
      const fingerprint = JSON.stringify([
        record,
        columnsFingerprint,
        recordTitle(record, state.current?.presentation),
      ]);
      const previous = existing.get(record.record_id);
      if (previous?.dataset.fingerprint === fingerprint) return previous;
      const row = element("tr");
      row.dataset.recordId = record.record_id;
      row.dataset.fingerprint = fingerprint;
      const identity = element("th", "table-identity");
      identity.scope = "row";
      const url = sourceUrl(record);
      const title = element(
        url ? "a" : "span",
        "",
        recordTitle(record, state.current?.presentation),
      );
      title.title = recordTitle(record, state.current?.presentation);
      if (url) {
        title.href = url;
        title.target = "_blank";
        title.rel = "noopener noreferrer";
      }
      identity.append(title, element("small", "", hostName(url)));
      row.append(identity);
      for (const field of fields) {
        const cell = element(
          "td",
          ["integer", "number"].includes(field.type) ? "numeric-cell" : "",
        );
        const value = fieldText(dataValue(record, field.key), field);
        const content = element("span", "table-cell-value", value);
        content.title = value;
        cell.append(content);
        row.append(cell);
      }
      const actions = element("td");
      const details = element("button", "button button-quiet", "Деталі");
      details.type = "button";
      details.addEventListener("click", () => showRecord(record));
      actions.append(details);
      row.append(actions);
      return row;
    });
    // Keep unchanged controls attached, including when new rows arrive before them.
    rows.forEach((row, index) => {
      if (body.children[index] !== row)
        body.insertBefore(row, body.children[index] || null);
    });
    while (body.children.length > rows.length) body.lastElementChild.remove();
  }

  $("result-filter").addEventListener("input", renderResults);
  for (const view of ["cards", "table"]) {
    $(`view-${view}`).addEventListener("click", () => {
      state.view = view;
      renderResults();
    });
  }

  return { renderResults, resetDatasetSelection };
}
