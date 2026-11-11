// Shared run state is created once by the application composition root.

export const ACTIVE = new Set(["queued", "running", "cancelling"]);
export const STATUS = {
  queued: "В черзі",
  running: "Збираємо дані",
  cancelling: "Зупиняємо…",
  success: "Завершено",
  partial: "Завершено з помилками",
  failed: "Не вдалося завершити",
  cancelled: "Зупинено",
  stopped: "Зупинено",
  interrupted: "Запуск перервано",
};
export const STAGES = {
  queued: "Готуємо запуск",
  preparing: "Готуємо запуск",
  resuming: "Продовжуємо збережений збір",
  schema: "Готуємо схему даних",
  schema_generation: "Готуємо схему даних",
  schema_refinement: "Уточнюємо характеристики за вмістом сторінок",
  discovery: "Шукаємо джерела",
  crawling: "Обходимо сторінки та збираємо дані",
  extraction: "Витягуємо записи",
  routing: "Обираємо потрібні блоки сторінки",
  assessment: "Перевіряємо зібрані дані",
  replay: "Читаємо збережені сторінки",
  cancelling: "Очікуємо завершення очищення ресурсів",
  storage: "Зберігаємо датасет",
  saving: "Зберігаємо датасет",
  done: "Збір завершено",
  complete: "Збір завершено",
  finished: "Збір завершено",
};

export function createState() {
  return {
    current: null,
    history: [],
    stream: null,
    poll: null,
    token: 0,
    dialogRecord: null,
    filters: new Map(),
    columns: null,
    view: "cards",
  };
}
