import { $, element, setConnection, showNotice } from "./dom.js";
import { ACTIVE } from "./state.js";

export async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  if (!response.ok) {
    let message = `Запит не виконано (${response.status}).`;
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (Array.isArray(body.detail))
        message = body.detail
          .map(
            (item) =>
              `${item.loc?.slice(1).join(" · ") || "Поле"}: ${item.msg}`,
          )
          .join("\n");
    } catch {
      /* The HTTP status remains useful for non-JSON errors. */
    }
    throw new Error(message);
  }
  return response.json();
}

export function saveSelection(id) {
  try {
    if (id) localStorage.setItem("unified:selected-run", id);
    else localStorage.removeItem("unified:selected-run");
  } catch {
    /* Browsing without local storage still works. */
  }
  const url = new URL(window.location.href);
  if (id) url.searchParams.set("run", id);
  else url.searchParams.delete("run");
  window.history.replaceState(null, "", url);
}

function selectedRun() {
  try {
    return localStorage.getItem("unified:selected-run");
  } catch {
    return null;
  }
}

export function createRunController(
  state,
  { renderRun, renderHistory, resetDatasetSelection },
) {
  function disconnect() {
    state.stream?.close();
    state.stream = null;
    if (state.poll) clearInterval(state.poll);
    state.poll = null;
  }

  function stopPolling() {
    if (state.poll) clearInterval(state.poll);
    state.poll = null;
  }

  function applySnapshot(snapshot, token) {
    if (token !== state.token || !snapshot || !snapshot.run_id) return;
    if (snapshot.run_id !== state.current?.run_id) return;
    const recovered =
      snapshot.stage === "interrupted" && !ACTIVE.has(snapshot.status);
    if (!recovered && snapshot.revision < (state.current.revision || 0)) return;
    if (!ACTIVE.has(state.current.status) && ACTIVE.has(snapshot.status))
      return;
    state.current = snapshot;
    renderRun();
    if (!ACTIVE.has(snapshot.status)) {
      disconnect();
      setConnection("Дані актуальні", "live");
      refreshHistory().catch(() => {});
    }
  }

  function connect(runId, token) {
    const stream = new EventSource(`/runs/${encodeURIComponent(runId)}/events`);
    state.stream = stream;
    stream.onopen = () => {
      if (token !== state.token) return;
      stopPolling();
      setConnection("Оновлюється наживо", "live");
    };
    stream.addEventListener("snapshot", (event) => {
      try {
        applySnapshot(JSON.parse(event.data), token);
      } catch {
        if (token === state.token)
          setConnection("Повторне підключення…", "retrying");
      }
    });
    stream.onerror = () => {
      if (token !== state.token || !ACTIVE.has(state.current?.status)) return;
      setConnection("Повторне підключення…", "retrying");
      // EventSource reconnects automatically; snapshots also keep the UI useful meanwhile.
      if (!state.poll)
        state.poll = setInterval(async () => {
          try {
            applySnapshot(
              await request(`/runs/${encodeURIComponent(runId)}`),
              token,
            );
          } catch {
            /* Reconnection remains visible; the run continues on the server. */
          }
        }, 3000);
    };
  }

  async function selectRun(runId) {
    disconnect();
    const token = ++state.token;
    showNotice("");
    setConnection("Завантажуємо запуск…");
    try {
      const snapshot = await request(`/runs/${encodeURIComponent(runId)}`);
      if (token !== state.token) return;
      state.current = snapshot;
      resetDatasetSelection();
      saveSelection(runId);
      $("result-filter").value = "";
      renderRun();
      renderHistory();
      if (ACTIVE.has(snapshot.status)) {
        connect(runId, token);
      } else setConnection("Дані актуальні", "live");
    } catch (error) {
      if (token !== state.token) return;
      setConnection("Не вдалося завантажити", "retrying");
      showNotice(error.message);
    }
  }

  async function refreshHistory() {
    const result = await request("/runs?limit=20");
    state.history = Array.isArray(result) ? result : [];
    renderHistory();
  }

  async function boot() {
    try {
      const profiles = await request("/jobs");
      for (const profile of profiles) {
        const option = element("option", "", profile.id);
        option.value = profile.id;
        $("profile").append(option);
      }
      await refreshHistory();
      const saved =
        new URL(window.location.href).searchParams.get("run") || selectedRun();
      const active = state.history.find((run) =>
        ACTIVE.has(run.status || run.summary?.status),
      );
      if (saved) await selectRun(saved);
      else if (active) await selectRun(active.run_id);
      else setConnection("Готовий до роботи", "live");
    } catch (error) {
      showNotice(`Не вдалося з’єднатися з API. ${error.message}`);
      setConnection("API недоступний", "retrying");
    }
  }

  return { disconnect, applySnapshot, selectRun, refreshHistory, boot };
}
