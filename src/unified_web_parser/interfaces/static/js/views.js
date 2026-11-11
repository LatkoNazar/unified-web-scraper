import { $, element } from "./dom.js";
import { ACTIVE, STATUS, STAGES } from "./state.js";
import { duration } from "./format.js";
import { fieldText } from "../dataset.js";
import { costSummary } from "./cost.js";

export function createRunViews(state, { renderResults, onSelectRun }) {
  function renderHistory() {
    const root = $("history");
    if (!state.history.length) {
      root.replaceChildren(
        element("p", "sidebar-empty", "Тут з’являться ваші пошуки."),
      );
      return;
    }
    root.replaceChildren(
      ...state.history.map((run) => {
        const status = run.status || run.summary?.status || "running";
        const query =
          run.question || run.config?.search || run.job_name || "Пошук";
        const button = element(
          "button",
          `history-item${state.current?.run_id === run.run_id ? " selected" : ""}`,
        );
        button.type = "button";
        button.title = query;
        button.setAttribute(
          "aria-pressed",
          String(state.current?.run_id === run.run_id),
        );
        const dot = element("span", `history-dot ${status}`);
        dot.setAttribute("aria-hidden", "true");
        const text = element("span");
        text.append(element("span", "history-name", query));
        const date = new Date(run.started_at);
        const time = Number.isNaN(date.getTime())
          ? ""
          : date.toLocaleString("uk-UA", {
              day: "numeric",
              month: "short",
              hour: "2-digit",
              minute: "2-digit",
            });
        text.append(
          element(
            "span",
            "history-time",
            `${time} · ${STATUS[status] || status}`,
          ),
        );
        button.append(dot, text);
        button.addEventListener("click", () => onSelectRun(run.run_id));
        return button;
      }),
    );
  }

  function renderRun() {
    const run = state.current;
    const cost = costSummary(run);
    $("cost-summary").hidden = cost.hidden;
    $("cost-summary").classList.toggle("pending", cost.pending);
    $("cost-value").textContent = cost.value;
    $("cost-note").textContent = cost.note;
    $("cost-label").textContent = cost.label || "Вартість";
    $("cancel-button").hidden = !ACTIVE.has(run?.status);
    $("resume-button").hidden = !run?.resume_available || ACTIVE.has(run?.status);
    $("update-button").hidden = !run?.update_available || ACTIVE.has(run?.status);
    const incremental = run?.incremental || run?.report?.incremental || {};
    const parentId = run?.parent_run_id || incremental.parent_run_id;
    $("incremental-note").hidden = !(run?.incremental_enabled || incremental.enabled || parentId);
    $("incremental-summary").textContent = ACTIVE.has(run?.status)
      ? "Дозбір: вже зібрані сторінки пропускаємо."
      : `Нових записів: ${run?.records?.length || 0}. Пропущено вже оброблених сторінок: ${incremental.skipped_detail_urls || 0}.`;
    $("parent-run-link").hidden = !parentId;
    if (parentId)
      $("parent-run-link").href = `/?run=${encodeURIComponent(parentId)}`;
    document.body.classList.toggle("has-run", Boolean(run));
    $("run-panel").hidden = !run;
    if (!run) {
      renderResults();
      return;
    }
    const active = ACTIVE.has(run.status);
    $("page-progress").closest(".progress-panel").hidden = !active;
    const counts = run.counters || {};
    const records = run.records || [];
    const maxPages =
      run.limits?.max_pages ||
      run.report?.pages_attempted ||
      Math.max(counts.pages_attempted || 0, 1);
    const pages = counts.pages_fetched ?? run.report?.pages_fetched ?? 0;
    const attempted =
      counts.pages_attempted ?? run.report?.pages_attempted ?? pages;
    const found = counts.records_found ?? records.length;
    $("run-status").className = `status-badge ${run.status}`;
    $("run-status").textContent = STATUS[run.status] || run.status;
    $("run-question").textContent = run.question || run.job_name || "Пошук";
    $("run-question").title = run.question || "";
    $("stat-records").textContent = found;
    $("stat-pages").replaceChildren(
      document.createTextNode(`${pages} `),
      element("small", "", `/ ${maxPages}`),
    );
    $("records-note").textContent = run.persisted
      ? `Збережено: ${run.records_persisted ?? run.report?.records_written ?? found}`
      : active
        ? "Попередній перегляд наживо"
        : "Попередній перегляд";
    $("pages-note").textContent = `Спроб завантаження: ${attempted}`;
    $("stat-time").textContent = duration(run);
    $("time-note").textContent = active
      ? "Збір триває"
      : "Загальний час запуску";
    const calls = run.ai?.calls ?? run.report?.ai?.calls;
    $("stat-ai").textContent = Number.isFinite(calls) ? calls : "—";
    $("ai-note").textContent = "Фактична кількість AI-запитів";
    $("cancel-button").hidden = !active;
    $("cancel-button").disabled = run.status === "cancelling";
    $("cancel-button").textContent =
      run.status === "cancelling" ? "Зупиняємо…" : "Зупинити";
    const stopMessage =
      run.stop_message ||
      run.report?.stop_message ||
      ({
        credits_exhausted:
          "Збір зупинено через обмеження доступних API-кредитів.",
        user_cancelled: "Збір зупинено користувачем.",
      }[run.stop_reason || run.report?.stop_reason] ??
        "");
    $("run-stop-message").textContent = stopMessage;
    $("run-stop-message").hidden = !stopMessage;
    $("stage-label").textContent = active
      ? STAGES[run.stage] || "Збір даних"
      : STATUS[run.status] || "Збір завершено";
    $("progress-label").textContent = `${attempted} / ${maxPages} спроб`;
    $("page-progress").max = maxPages;
    $("page-progress").value = Math.min(attempted, maxPages);
    $("page-progress")
      .closest(".progress-panel")
      .classList.toggle("idle", !active);
    const stageOrder = ["schema", "discovery", "crawling", "storage"];
    const stage = ["schema_generation", "schema_refinement"].includes(run.stage)
      ? "schema"
      : ["extraction", "routing", "assessment", "replay"].includes(run.stage)
        ? "crawling"
        : run.stage === "saving"
          ? "storage"
          : run.stage;
    for (const node of document.querySelectorAll("[data-stage]")) {
      const index = stageOrder.indexOf(node.dataset.stage);
      const current = stageOrder.indexOf(stage);
      node.classList.toggle("current", active && index === current);
      node.classList.toggle(
        "complete",
        run.status === "success" || current > index,
      );
    }
    $("download-button").disabled = !records.length;
    renderResults();
    renderActivity();
  }

  function renderActivity() {
    const run = state.current;
    const logs = run?.logs || [];
    $("activity-count").textContent = logs.length;
    const issues = [
      ...(run?.errors || []).map((item) => ({ ...item, kind: "error" })),
      ...(run?.warnings || []).map((item) => ({ ...item, kind: "warning" })),
    ];
    $("issues").replaceChildren(
      ...issues
        .slice(-30)
        .map((item) =>
          element(
            "div",
            `issue ${item.kind}`,
            `${item.stage ? `${item.stage}: ` : ""}${item.message || fieldText(item)}`,
          ),
        ),
    );
    const log = $("activity-log");
    const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 50;
    log.replaceChildren(
      ...logs.slice(-100).map((item) => {
        const row = element("li");
        const date = new Date(item.time || item.timestamp);
        const time = Number.isNaN(date.getTime())
          ? ""
          : date.toLocaleTimeString("uk-UA", {
              hour: "2-digit",
              minute: "2-digit",
              second: "2-digit",
            });
        row.append(
          element("time", "", time),
          element("span", "", item.message || fieldText(item)),
        );
        return row;
      }),
    );
    if (!logs.length)
      log.append(
        element("li", "log-empty", "Тут з’являться етапи та події збору."),
      );
    if (nearBottom) log.scrollTop = log.scrollHeight;
    if (issues.length && !ACTIVE.has(run?.status)) $("activity").open = true;
  }

  return { renderRun, renderHistory, renderActivity };
}
