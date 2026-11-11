// Composition root: each view/controller receives the same application state.
import { createState } from "./js/state.js";
import { createRecordDialog } from "./js/dialog.js";
import { createDatasetView } from "./js/results.js";
import { createRunViews } from "./js/views.js";
import { createRunController } from "./js/transport.js";
import { wireActions } from "./js/actions.js";

const state = createState();
const dialog = createRecordDialog(state);
const dataset = createDatasetView(state, { showRecord: dialog.showRecord });
let controller;
const views = createRunViews(state, {
  renderResults: dataset.renderResults,
  onSelectRun: (runId) => controller.selectRun(runId),
});
controller = createRunController(state, {
  renderRun: views.renderRun,
  renderHistory: views.renderHistory,
  resetDatasetSelection: dataset.resetDatasetSelection,
});
wireActions(state, {
  controller,
  views,
  resetDatasetSelection: dataset.resetDatasetSelection,
});
controller.boot();
