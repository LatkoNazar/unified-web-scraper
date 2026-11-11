import { $, element, setConnection, showNotice } from "./dom.js";
import { safeUrl, duration } from "./format.js";
import { ACTIVE } from "./state.js";
import { request, saveSelection } from "./transport.js";

export function wireActions(
  state,
  { controller, views, resetDatasetSelection },
) {
  const { disconnect, applySnapshot, selectRun, refreshHistory } = controller;
  const { renderRun, renderActivity, renderHistory } = views;

  function newSearch() {
    if (ACTIVE.has(state.current?.status)) {
      $("query").focus();
      $("query").scrollIntoView({ block: "center" });
      return;
    }
    disconnect();
    ++state.token;
    state.current = null;
    saveSelection(null);
    showNotice("");
    $("result-filter").value = "";
    resetDatasetSelection();
    $("download-button").disabled = true;
    renderRun();
    renderActivity();
    renderHistory();
    setConnection("Готовий до роботи");
    $("query").focus();
  }

  $("search-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    showNotice("");
    const sources = $("sources")
      .value.split(/\n/)
      .map((line) => line.trim())
      .filter(Boolean);
    if (sources.some((url) => !safeUrl(url))) {
      showNotice(
        "Вкажіть абсолютні HTTP(S) адреси сайтів, по одній у рядку, без логіна й пароля в URL.",
      );
      return;
    }
    if (
      !$("ai-enabled").checked &&
      !sources.length &&
      Number($("max-calls").value) === 0
    ) {
      showNotice(
        "Вебпошуку потрібен ненульовий бюджет API-запитів. Для збору без нього вкажіть URL джерела.",
      );
      return;
    }
    const body = {
      query: $("query").value.trim(),
      profile: $("profile").value || null,
      sources,
      backend: $("backend").value,
      ai: $("ai-enabled").checked,
      incremental: $("incremental-enabled").checked,
      max_pages: Number($("max-pages").value),
      max_records: Number($("max-records").value),
      max_calls: Number($("max-calls").value),
    };
    $("start-button").disabled = true;
    try {
      const snapshot = await request("/runs", {
        method: "POST",
        body: JSON.stringify(body),
      });
      await selectRun(snapshot.run_id);
      await refreshHistory();
    } catch (error) {
      showNotice(error.message);
    } finally {
      $("start-button").disabled = false;
    }
  });

  $("resume-button").addEventListener("click", async () => {
    if (!state.current?.resume_available) return;
    const runId = state.current.run_id;
    $("resume-button").disabled = true;
    showNotice("");
    try {
      await request(`/runs/${encodeURIComponent(runId)}/resume`, { method: "POST" });
      await selectRun(runId);
      await refreshHistory();
    } catch (error) {
      showNotice(error.message);
    } finally {
      $("resume-button").disabled = false;
    }
  });

  $("cancel-button").addEventListener("click", async () => {
    if (!state.current) return;
    const runId = state.current.run_id;
    const token = state.token;
    $("cancel-button").disabled = true;
    try {
      applySnapshot(
        await request(`/runs/${encodeURIComponent(runId)}/cancel`, {
          method: "POST",
        }),
        token,
      );
    } catch (error) {
      if (token === state.token) {
        showNotice(error.message);
        $("cancel-button").disabled = false;
      }
    }
  });
  $("update-button").addEventListener("click", async () => {
    if (!state.current?.update_available) return;
    $("update-button").disabled = true;
    showNotice("");
    try {
      const snapshot = await request(
        `/runs/${encodeURIComponent(state.current.run_id)}/update`,
        { method: "POST" },
      );
      await selectRun(snapshot.run_id);
      await refreshHistory();
    } catch (error) {
      showNotice(error.message);
    } finally {
      $("update-button").disabled = false;
    }
  });
  $("new-search").addEventListener("click", newSearch);
  $("refresh-history").addEventListener("click", () =>
    refreshHistory().catch((error) => showNotice(error.message)),
  );

  $("download-button").addEventListener("click", () => {
    if (!state.current?.records?.length) return;
    const blob = new Blob([JSON.stringify(state.current.records, null, 2)], {
      type: "application/json;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const link = element("a");
    link.href = url;
    link.download = `unified-${state.current.run_id}.json`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  setInterval(() => {
    if (state.current && ACTIVE.has(state.current.status))
      $("stat-time").textContent = duration(state.current);
  }, 1000);
  window.addEventListener("pagehide", disconnect);
  const historyDisclosure = document.querySelector(".history-disclosure");
  const compactLayout = window.matchMedia("(max-width: 800px)");
  historyDisclosure.open = !compactLayout.matches;
  compactLayout.addEventListener("change", (event) => {
    historyDisclosure.open = !event.matches;
  });
}
