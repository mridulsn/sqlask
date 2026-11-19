/* Offline integration harness: real replay/worker code, fake DOM and scheduling. */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const app = require("../web/app.js");
const cases = require("./scoring_cases.json");

class Element {
  constructor() { this.children = []; this.text = ""; this.disabled = false; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(x => x.textContent).join(""); }
  get firstChild() { return this.children[0]; }
  appendChild(child) { this.children.push(child); return child; }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
  setAttribute() {}
}
function harness() {
  const nodes = {}, timers = new Map(), workers = [];
  let timerId = 0;
  class Worker {
    constructor() { this.messages = []; this.terminated = false; workers.push(this); }
    postMessage(m) { this.messages.push(m); }
    terminate() { this.terminated = true; }
    reply(m) { this.onmessage({data: m}); }
  }
  const context = {
    document: {getElementById: id => nodes[id] ||= new Element(),
      createElement: () => new Element(), createTextNode: text => {
        const node = new Element(); node.textContent = text; return node;
      }},
    window: {matchMedia: () => ({matches: true})},
    Blob: class {}, URL: {createObjectURL: () => "blob:test", revokeObjectURL() {}}, Worker,
    setTimeout: (fn, ms) => { timers.set(++timerId, {fn, ms}); return timerId; },
    clearTimeout: id => timers.delete(id),
  };
  let source = fs.readFileSync("web/app.js", "utf8");
  source = source.slice(0, source.indexOf('  if (document.readyState === "loading")')) + `
    sleep = function () { return Promise.resolve(); };
    globalThis.testApi = {replay: replay, send: send, startWorker: startWorker,
      setExec: function (fn) { execSql = fn; },
      setRuns: function (runs) { setByName.test = {name: "test", meta: {}, runs: runs}; }};
  })();`;
  vm.runInNewContext(source, context);
  return {api: context.testApi, nodes, timers, workers};
}
async function flush() { for (let i = 0; i < 100; i++) await Promise.resolve(); }
function run(id, attempts = 1) {
  return {id, question: id + " question", answer: id + " answer", ok: true,
    final_sql: "SELECT '" + id + "'", gold_sql: "SELECT '" + id + "'", correct: true,
    attempts: Array.from({length: attempts}, (_, i) => ({n: i + 1, sql: "SELECT 1", row_count: 1}))};
}
function result(value) { return {columns: ["value"], rows: [[value]], ms: 1}; }

async function main(finding) {
  if (finding === "f01") {
    for (const c of cases.empty) assert.equal(app.resultsMatch([], [], false, c.strict, c.gold, c.pred), c.match);
  } else if (finding === "f03" || finding === "f03_cap") {
    const h = harness();
    const record = run("A");
    record.model_sql = "SELECT TrackId FROM Track";
    record.final_sql = record.model_sql + " LIMIT 200";
    record.gold_sql = "SELECT TrackId FROM Track LIMIT 200";
    h.api.setRuns([record]);
    h.api.setExec(sql => {
      const limit = Number(/LIMIT (\d+)\s*$/.exec(sql)[1]);
      const available = finding === "f03_cap" ? 100001 : 201;
      return Promise.resolve({columns: ["TrackId"],
        rows: Array.from({length: Math.min(limit, available)}, (_, i) => [i + 1])});
    });
    h.api.replay("test/A"); await flush();
    assert.match(h.nodes["r-verdict"].textContent,
      finding === "f03_cap" ? /Inconclusive/ : /Does not match gold/);
    assert.match(h.nodes["r-sql"].textContent, /LIMIT 100001/);
  } else if (finding === "f04") {
    for (const c of cases.numbers) assert.equal(app.norm(c.value), c.expected, String(c.value));
    assert.equal(app.resultsMatch([[1.125]], [[1.12]], false, true), true);
  } else if (finding.startsWith("f05")) {
    const h = harness(), pending = [];
    h.api.setRuns([run("A"), run("B")]);
    h.api.setExec(() => new Promise((resolve, reject) => pending.push({resolve, reject})));
    h.api.replay("test/A"); await flush();
    h.api.replay("test/B"); await flush();
    assert.equal(pending.length, 4);
    if (finding !== "f05_button") {
      pending[2].resolve(result("B")); pending[3].resolve(result("B")); await flush();
    }
    if (finding === "f05_error") pending[0].reject(new Error("stale error"));
    else pending[0].resolve(result("A"));
    pending[1].resolve(result("A")); await flush();
    if (finding === "f05_button") {
      assert.equal(h.nodes["replay-btn"].disabled, true);
      pending[2].resolve(result("B")); pending[3].resolve(result("B")); await flush();
    }
    assert.equal(h.nodes["r-question"].textContent, "B question");
    assert.equal(h.nodes["r-answer"].textContent, "B answer");
    assert.ok(h.nodes["r-table"].textContent.includes("B"));
    assert.ok(!h.nodes["r-verdict"].textContent.includes("stale error"));
    assert.equal(h.nodes["replay-btn"].disabled, false);
  } else if (finding === "f06") {
    const h = harness(); h.api.startWorker();
    let a, b;
    const pa = h.api.send({type: "exec"}, 8000).catch(e => { a = e; });
    const pb = h.api.send({type: "exec"}, 8000).catch(e => { b = e; });
    const oldTimers = [...h.timers.values()];
    oldTimers[0].fn(); await flush();
    assert.ok(a && b, "all requests on the timed-out worker must reject");
    assert.equal(h.workers.length, 2);
    const pc = h.api.send({type: "exec"}, 8000);
    oldTimers[1].fn(); await flush(); // even an already queued callback is harmless
    assert.equal(h.workers.length, 2);
    assert.equal(h.workers[1].terminated, false);
    const id = h.workers[1].messages[0].id;
    h.workers[0].reply({id, ok: true, rows: [["stale"]]});
    h.workers[1].reply({id, ok: true, rows: [["fresh"]]});
    assert.deepEqual((await pc).rows, [["fresh"]]);
    await Promise.all([pa, pb]);
    assert.equal(h.timers.size, 0);
  } else if (finding === "f10") {
    for (const name of ["\u00e9delete", "delete\u00e9", "\u00e9limit", "limit\u00e9", "\u017finsert", "\ud83d\ude00drop"]) {
      const sql = "SELECT 1 AS " + name;
      assert.equal(app.checkSql(sql).sql, sql + "\nLIMIT 200");
    }
  } else if (finding === "f11") {
    const h = harness(), calls = [];
    h.api.setRuns([run("A", 3)]);
    h.api.setExec(sql => { calls.push(sql); return Promise.resolve(result("A")); });
    h.api.replay("test/A"); await flush();
    assert.equal(calls.length, 2, "only final and gold execute live");
    assert.match(h.nodes["r-attempts"].textContent, /Recorded attempt/);
    const html = fs.readFileSync("web/index.html", "utf8");
    assert.ok(!html.includes("Everything after the model writes its SQL"));
    assert.match(html, /Earlier attempts and their statuses are recorded playback/);
    assert.match(html, /final.*gold[\s\S]*live in your browser/);
  } else throw new Error("unknown finding: " + finding);
}
main(process.argv[2]).catch(error => { console.error(error); process.exitCode = 1; });
