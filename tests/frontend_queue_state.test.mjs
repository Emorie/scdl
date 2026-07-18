import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { JSDOM } from "jsdom";

async function loadQueueRenderer() {
  const dom = new JSDOM(`<!doctype html><div id="queue-pill"></div><div id="queue-list"></div><div id="likes-current"></div>`, {
    url: "http://localhost/",
    runScripts: "outside-only",
  });
  dom.window.CSS = { escape: (value) => String(value).replaceAll('"', "\\\"") };
  let source = await readFile(new URL("../scdl_web/static/app.js", import.meta.url), "utf8");
  source = source.replace(/wireEvents\(\);[\s\S]*$/, "window.__queueTest = { state, renderQueue };");
  dom.window.eval(source);
  return { ...dom.window.__queueTest, document: dom.window.document };
}

const item = (id, status = "Running", logs = ["first line"]) => ({
  id, status, target: `track-${id}`, job_type: "Download", url_kind: "track",
  logs, command: [], summary: {}, files: [], metadata_records: [],
});

test("expanded log panel survives live logs, status changes, and queue reordering", async () => {
  const { state, renderQueue, document } = await loadQueueRenderer();
  renderQueue({ paused: false, items: [item("stable-id"), item("other", "Pending")] });
  state.expandedLogItems.add("stable-id");
  renderQueue({ paused: false, items: [item("stable-id", "Downloading", ["first line", "live line"]), item("other", "Pending")] });
  let panel = document.querySelector('.queue-item[data-id="stable-id"] details');
  assert.equal(panel?.open, true);
  renderQueue({ paused: false, items: [item("other", "Pending"), item("stable-id", "Processing", ["first line", "live line", "processing"])] });
  panel = document.querySelector('.queue-item[data-id="stable-id"] details');
  assert.equal(panel?.open, true);
  assert.equal(state.expandedLogItems.has("stable-id"), true);
});
