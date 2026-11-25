// Copyright © 2026, SAS Institute Inc., Cary, NC, USA. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { runInNewContext } from "node:vm";

test("a missing job stops polling and displays the server's message", async () => {
  const view = new URL("../src/sas_mcp_server/ui/views/sas-log.html", import.meta.url);
  const script = readFileSync(view, "utf8").split('<script type="module">')[1].split("</script>")[0];
  const nodes = new Map();
  const calls = [];
  let timers = 0;
  let busy = true;
  const document = {
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, { addEventListener() {} });
      return nodes.get(id);
    },
  };
  const sas = {
    onInput() {}, onResult() {},
    busy(value) { busy = value; },
    async call(name) {
      calls.push(name);
      return { status: "not_found", job_id: "missing", message: "Job record expired." };
    },
  };
  await runInNewContext(script + '\npollJob("missing")', {
    document, sas, clearTimeout() {}, setTimeout() { timers++; },
  });
  assert.deepEqual(calls, ["get_job_status"]);
  assert.equal(nodes.get("log").textContent, "Job record expired.");
  assert.equal(timers, 0);
  assert.equal(busy, false);
});
