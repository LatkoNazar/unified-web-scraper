import { $, element } from "./dom.js";
import { imageUrl, recordTitle, sourceUrl, label } from "./format.js";
import { fieldText } from "../dataset.js";

export function createRecordDialog(state) {
  function showRecord(record) {
    state.dialogRecord = record;
    $("detail-title").textContent = recordTitle(
      record,
      state.current?.presentation,
    );
    $("copy-record").textContent = "Копіювати JSON";
    const content = $("detail-content");
    content.replaceChildren();
    const image = imageUrl(record);
    if (image) {
      const img = element("img", "detail-image");
      img.alt = recordTitle(record, state.current?.presentation);
      img.referrerPolicy = "no-referrer";
      img.addEventListener(
        "error",
        () => {
          img.hidden = true;
        },
        { once: true },
      );
      img.src = image;
      content.append(img);
    }
    const fields = element("dl", "detail-table");
    for (const [key, value] of Object.entries(record.data || {})) {
      const field = state.current?.presentation?.fields?.find(
        (item) => item.key === key,
      );
      const row = element("div");
      row.append(
        element("dt", "", field?.label || label(key)),
        element("dd", "", fieldText(value, field)),
      );
      fields.append(row);
    }
    content.append(fields);
    const url = sourceUrl(record);
    if (url) {
      const link = element("a", "detail-source", url);
      link.href = url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      content.append(link);
    }
    const disclosure = element("details");
    disclosure.append(
      element("summary", "field-help", "Повний JSON і джерело даних"),
      element("pre", "detail-json", JSON.stringify(record, null, 2)),
    );
    content.append(disclosure);
    $("record-dialog").showModal();
  }

  $("close-dialog").addEventListener("click", () => $("record-dialog").close());
  $("close-dialog-bottom").addEventListener("click", () =>
    $("record-dialog").close(),
  );
  $("copy-record").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(
        JSON.stringify(state.dialogRecord, null, 2),
      );
      $("copy-record").textContent = "Скопійовано ✓";
    } catch {
      $("copy-record").textContent = "Оберіть текст у повному JSON";
    }
  });

  return { showRecord };
}
