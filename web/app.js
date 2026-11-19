/* sqlask demo: replay recorded runs, run SQL live with sql.js, show the eval.
   The guard and the scorer are ports of src/sqlask/guard.py and scoring.py.
   ASCII-only source. */
(function () {
  "use strict";

  // ------------------------------------------------------------------
  // Read-only guard (port of guard.py; tested against tests/guard_cases.json)
  // ------------------------------------------------------------------
  var DEFAULT_MAX_ROWS = 200;
  var FORBIDDEN = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "ATTACH",
    "DETACH", "PRAGMA", "VACUUM", "REINDEX", "ANALYZE", "BEGIN", "COMMIT",
    "ROLLBACK", "SAVEPOINT", "RELEASE", "TRUNCATE", "GRANT", "LOAD_EXTENSION"];

  function GuardError(message) {
    var e = new Error(message);
    e.name = "GuardError";
    return e;
  }

  function maskSql(sql) {
    var out = [];
    var i = 0, n = sql.length;
    while (i < n) {
      var ch = sql[i];
      if (ch === "'" || ch === '"' || ch === "`" || ch === "[") {
        var close = ch === "[" ? "]" : ch;
        var j = i + 1;
        while (true) {
          if (j >= n) throw GuardError("unterminated quoted string or identifier");
          if (sql[j] === close) {
            if (close !== "]" && j + 1 < n && sql[j + 1] === close) { j += 2; continue; }
            break;
          }
          j += 1;
        }
        out.push(ch + " ".repeat(j - i - 1) + close);
        i = j + 1;
      } else if (ch === "-" && sql.substr(i, 2) === "--") {
        var k = sql.indexOf("\n", i);
        k = k === -1 ? n : k;
        out.push(" ".repeat(k - i));
        i = k;
      } else if (ch === "/" && sql.substr(i, 2) === "/*") {
        var c = sql.indexOf("*/", i + 2);
        if (c === -1) throw GuardError("unterminated /* comment");
        out.push(" ".repeat(c + 2 - i));
        i = c + 2;
      } else {
        out.push(ch);
        i += 1;
      }
    }
    return out.join("");
  }

  function topLevelLimit(masked) {
    var re = /\(|\)|\bLIMIT\b/gi, m, depth = 0, last = null;
    while ((m = re.exec(masked)) !== null) {
      if (m[0] === "(") depth += 1;
      else if (m[0] === ")") depth -= 1;
      else if (depth === 0) last = { start: m.index, end: m.index + m[0].length };
    }
    return last;
  }

  function checkSql(sql, maxRows) {
    if (maxRows === undefined) maxRows = DEFAULT_MAX_ROWS;
    if (sql === null || sql === undefined || !String(sql).trim()) throw GuardError("empty query");
    sql = String(sql);
    var masked = maskSql(sql);
    var stripped = masked.replace(/\s+$/, "");
    while (stripped.endsWith(";")) stripped = stripped.slice(0, -1).replace(/\s+$/, "");
    if (stripped.indexOf(";") !== -1) throw GuardError("only one statement is allowed");
    var body = sql.slice(0, stripped.length);
    masked = stripped;

    var first = /^\s*([A-Za-z]+)/.exec(masked);
    if (!first || ["SELECT", "WITH"].indexOf(first[1].toUpperCase()) === -1) {
      throw GuardError("only SELECT queries are allowed (must start with SELECT or WITH)");
    }
    var upper = masked.toUpperCase();
    for (var f = 0; f < FORBIDDEN.length; f++) {
      if (new RegExp("\\b" + FORBIDDEN[f] + "\\b").test(upper)) throw GuardError("forbidden keyword: " + FORBIDDEN[f]);
    }
    if (/\bREPLACE\s+INTO\b/.test(upper)) throw GuardError("forbidden keyword: REPLACE");
    if ((masked.match(/\(/g) || []).length !== (masked.match(/\)/g) || []).length) {
      throw GuardError("unbalanced parentheses");
    }

    var limit = topLevelLimit(masked);
    if (limit === null) return { sql: body + "\nLIMIT " + maxRows, limitAdded: true, limitCapped: false };
    var tail = masked.slice(limit.end);
    var num = /^\s*(\d+)\s*(?:$|OFFSET\b)/i.exec(tail);
    if (num) {
      var value = parseInt(num[1], 10);
      if (value <= maxRows) return { sql: body, limitAdded: false, limitCapped: false };
      var start = limit.end + /^\s*/.exec(tail)[0].length;
      var end = start + num[1].length;
      return { sql: body.slice(0, start) + String(maxRows) + body.slice(end), limitAdded: false, limitCapped: true };
    }
    return { sql: "SELECT * FROM (\n" + body + "\n) LIMIT " + maxRows, limitAdded: true, limitCapped: false };
  }

  // ------------------------------------------------------------------
  // Execution accuracy (port of scoring.py)
  // ------------------------------------------------------------------
  function norm(v) {
    if (v === null || v === undefined) return null;
    if (typeof v === "number") return Math.round(v * 100) / 100 + 0;
    if (typeof v === "boolean") return v ? 1 : 0;
    var s = String(v).trim();
    if (s !== "" && /^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(s)) return Math.round(parseFloat(s) * 100) / 100 + 0;
    return s;
  }
  function key(v) { return v === null ? "\u0000null" : (typeof v) + ":" + String(v); }
  function rowKey(r) { return r.map(key).join("\u0001"); }
  function bag(list) {
    var m = new Map();
    list.forEach(function (x) { m.set(x, (m.get(x) || 0) + 1); });
    return m;
  }
  function bagEq(a, b) {
    if (a.size !== b.size) return false;
    var ok = true;
    a.forEach(function (v, k) { if (b.get(k) !== v) ok = false; });
    return ok;
  }
  function rowsEqual(g, p, ordered) {
    if (g.length !== p.length) return false;
    var gk = g.map(rowKey), pk = p.map(rowKey);
    if (ordered) return gk.every(function (x, i) { return x === pk[i]; });
    return bagEq(bag(gk), bag(pk));
  }
  function goldViews(rows) {
    if (!rows.length) return [rows];
    var n = rows[0].length, pairs = [];
    for (var i = 0; i < n - 1; i++) {
      if (rows.every(function (r) { return typeof r[i] === "string" && typeof r[i + 1] === "string"; })) pairs.push(i);
    }
    var subsets = [];
    (function rec(start, acc) {
      subsets.push(acc);
      pairs.forEach(function (p) { if (p >= start) rec(p + 2, acc.concat([p])); });
    })(0, []);
    return subsets.map(function (chosen) {
      return rows.map(function (r) {
        var out = [], j = 0;
        while (j < n) {
          if (chosen.indexOf(j) !== -1) { out.push(r[j] + " " + r[j + 1]); j += 2; }
          else { out.push(r[j]); j += 1; }
        }
        return out;
      });
    });
  }
  function matchView(g, p, ordered, strict) {
    if (g.length !== p.length) return false;
    if (!g.length) return true;
    var gc = g[0].length, pc = p[0].length;
    if (gc > pc || (strict && gc !== pc)) return false;
    var gBags = [], pBags = [], c;
    for (c = 0; c < gc; c++) gBags.push(bag(g.map(function (r) { return key(r[c]); })));
    for (c = 0; c < pc; c++) pBags.push(bag(p.map(function (r) { return key(r[c]); })));
    var cands = gBags.map(function (gb) {
      var out = [];
      pBags.forEach(function (pb, j) { if (bagEq(gb, pb)) out.push(j); });
      return out;
    });
    if (cands.some(function (x) { return !x.length; })) return false;
    var tried = 0, found = false;
    (function rec(idx, used) {
      if (found || tried > 5000) return;
      if (idx === gc) {
        tried += 1;
        var proj = p.map(function (r) { return used.map(function (j) { return r[j]; }); });
        if (rowsEqual(g, proj, ordered)) found = true;
        return;
      }
      cands[idx].forEach(function (j) { if (used.indexOf(j) === -1) rec(idx + 1, used.concat([j])); });
    })(0, []);
    return found;
  }
  function resultsMatch(goldRows, predRows, ordered, strict) {
    var g = goldRows.map(function (r) { return r.map(norm); });
    var p = predRows.map(function (r) { return r.map(norm); });
    var views = strict ? [g] : goldViews(g);
    return views.some(function (v) { return matchView(v, p, !!ordered, !!strict); });
  }

  var api = { checkSql: checkSql, maskSql: maskSql, resultsMatch: resultsMatch, norm: norm };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof document === "undefined") return;

  // ------------------------------------------------------------------
  // sql.js in a Web Worker, so a runaway query can be stopped
  // ------------------------------------------------------------------
  var SQLJS = "https://cdnjs.cloudflare.com/ajax/libs/sql.js/1.13.0/";
  var QUERY_TIMEOUT_MS = 8000;
  var workerSrc = [
    "importScripts('" + SQLJS + "sql-wasm.js');",
    "var db = null;",
    "onmessage = function (e) {",
    "  var m = e.data;",
    "  if (m.type === 'open') {",
    "    initSqlJs({ locateFile: function (f) { return '" + SQLJS + "' + f; } }).then(function (SQL) {",
    "      db = new SQL.Database(new Uint8Array(m.buf));",
    "      db.exec('PRAGMA query_only = 1');",
    "      postMessage({ id: m.id, ok: true });",
    "    }, function (err) { postMessage({ id: m.id, ok: false, error: String(err && err.message || err) }); });",
    "  } else if (m.type === 'exec') {",
    "    var stmt = null;",
    "    try {",
    "      var t0 = Date.now(); stmt = db.prepare(m.sql);",
    "      var cols = stmt.getColumnNames(), rows = [];",
    "      while (stmt.step()) rows.push(stmt.get());",
    "      postMessage({ id: m.id, ok: true, columns: cols, rows: rows, ms: Date.now() - t0 });",
    "    }",
    "    catch (err) { postMessage({ id: m.id, ok: false, error: String(err && err.message || err) }); }",
    "    finally { if (stmt) stmt.free(); }",
    "  }",
    "};"
  ].join("\n");

  var dbBytes = null, worker = null, pending = {}, nextId = 1, readyPromise = null;

  function startWorker() {
    var url = URL.createObjectURL(new Blob([workerSrc], { type: "text/javascript" }));
    worker = new Worker(url);
    worker.onmessage = function (e) {
      var p = pending[e.data.id];
      if (!p) return;
      delete pending[e.data.id];
      clearTimeout(p.timer);
      if (e.data.ok) p.resolve(e.data); else p.reject(new Error(e.data.error));
    };
    readyPromise = send({ type: "open", buf: dbBytes.slice(0) }, 30000);
    return readyPromise;
  }

  function send(msg, timeoutMs) {
    return new Promise(function (resolve, reject) {
      var id = nextId++;
      msg.id = id;
      var timer = setTimeout(function () {
        delete pending[id];
        worker.terminate();
        reject(new Error("query stopped after " + Math.round(timeoutMs / 1000) + " s time limit"));
        startWorker();
      }, timeoutMs);
      pending[id] = { resolve: resolve, reject: reject, timer: timer };
      worker.postMessage(msg);
    });
  }

  function execSql(sql) {
    return readyPromise.then(function () { return send({ type: "exec", sql: sql }, QUERY_TIMEOUT_MS); })
      .then(function (r) { return { columns: r.columns, rows: r.rows, ms: r.ms }; });
  }

  // guard + execute; truncated when the guard's LIMIT was hit
  function runGuarded(sql) {
    var g = checkSql(sql, DEFAULT_MAX_ROWS);
    return execSql(g.sql).then(function (res) {
      res.guard = g;
      res.truncated = (g.limitAdded || g.limitCapped) && res.rows.length >= DEFAULT_MAX_ROWS;
      return res;
    });
  }

  // ------------------------------------------------------------------
  // DOM helpers
  // ------------------------------------------------------------------
  function $(id) { return document.getElementById(id); }
  function el(tag, attrs, children) {
    var n = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === "text") n.textContent = attrs[k];
      else if (k === "class") n.className = attrs[k];
      else n.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { if (c) n.appendChild(typeof c === "string" ? document.createTextNode(c) : c); });
    return n;
  }
  function clear(n) { while (n.firstChild) n.removeChild(n.firstChild); return n; }
  function fmt(v) {
    if (v === null || v === undefined) return "NULL";
    if (typeof v === "number" && !Number.isInteger(v)) return String(Math.round(v * 10000) / 10000);
    return String(v);
  }
  function pct(x) { return (x * 100).toFixed(1) + "%"; }

  function renderTable(target, res, note) {
    clear(target);
    if (!res.columns.length) {
      target.appendChild(el("p", { class: "result-meta", text: "The query returned no columns." }));
      return;
    }
    var numeric = res.columns.map(function (c, i) {
      return res.rows.length > 0 && res.rows.every(function (r) { return r[i] === null || typeof r[i] === "number"; });
    });
    var thead = el("thead", null, [el("tr", null, res.columns.map(function (c, i) { return el("th", { text: c, scope: "col", class: numeric[i] ? "num" : "" }); }))]);
    var shown = res.rows.slice(0, 25);
    var tbody = el("tbody", null, shown.map(function (r) {
      return el("tr", null, r.map(function (v) {
        return el("td", { class: typeof v === "number" ? "num" : "", text: fmt(v) });
      }));
    }));
    target.appendChild(el("table", { class: "result" }, [thead, tbody]));
    var meta = res.rows.length + (res.rows.length === 1 ? " row" : " rows");
    if (res.truncated) meta += " (stopped at the " + DEFAULT_MAX_ROWS + "-row limit)";
    if (res.rows.length > shown.length) meta += ", first " + shown.length + " shown";
    if (res.ms !== undefined) meta += ", " + res.ms + " ms in your browser";
    if (note) meta += ". " + note;
    target.appendChild(el("p", { class: "result-meta", text: meta }));
  }

  var reduceMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, reduceMotion ? 0 : ms); }); }

  function typeInto(node, text, token) {
    if (reduceMotion) { node.textContent = text; return Promise.resolve(); }
    var step = Math.max(2, Math.ceil(text.length / 60));
    var i = 0;
    return new Promise(function (resolve) {
      (function tick() {
        if (token.cancelled) { node.textContent = text; return resolve(); }
        i = Math.min(text.length, i + step);
        node.textContent = text.slice(0, i);
        if (i >= text.length) return resolve();
        setTimeout(tick, 16);
      })();
    });
  }

  // ------------------------------------------------------------------
  // Recorded runs
  // ------------------------------------------------------------------
  var SETS = [], setByName = {}, replayToken = { cancelled: false };
  var MAIN_SET = "sonnet", COMPARE_SET = "haiku";
  var KIND_LABEL = { sql_error: "SQL error", empty: "suspicious empty result", guard: "blocked by guard",
    parse: "no SQL in reply", timeout: "timed out", provider: "model call failed" };

  function findRun(setName, id) {
    var set = setByName[setName];
    if (!set) return null;
    for (var i = 0; i < set.runs.length; i++) if (set.runs[i].id === id) return set.runs[i];
    return null;
  }
  function shortModel(m) { return (m || "").replace("claude-", "").replace(/-(\d)-(\d)$/, " $1.$2"); }

  function attemptTag(a, isLast) {
    if (!a.error_kind) return el("span", { class: "tag ok", text: "ran, " + a.row_count + (a.row_count === 1 ? " row" : " rows") });
    var label = KIND_LABEL[a.error_kind] || a.error_kind;
    return el("span", { class: "tag " + (a.error_kind === "empty" ? "retry" : "err"), text: label + (isLast ? "" : ", sent back") });
  }

  function replay(value) {
    var parts = String(value).split("/"), set = setByName[parts[0]], run = findRun(parts[0], parts[1]);
    if (!run) return;
    replayToken.cancelled = true;
    var token = { cancelled: false };
    replayToken = token;
    var m = set.meta;
    $("rec-label").textContent = "Recorded run: SQL by " + m.sql_model + (m.answer_model ? ", answer by " + m.answer_model : "") +
      ", via the Claude Code CLI, " + m.recorded + (m.schema === "bare" ? ", bare schema (no sample values)" : "");
    $("replay-out").hidden = false;
    $("r-question").textContent = run.question;
    var list = clear($("r-attempts"));
    $("r-final").hidden = true;
    var btn = $("replay-btn");
    btn.disabled = true;

    var chain = Promise.resolve();
    run.attempts.forEach(function (a, idx) {
      chain = chain.then(function () {
        if (token.cancelled) return;
        var sqlNode = el("pre", { class: "sql" });
        var head = el("div", { class: "attempt-head" }, [el("strong", { text: "Attempt " + a.n }),
          el("span", { text: (a.model || "") + (a.latency_ms ? ", " + (a.latency_ms / 1000).toFixed(1) + " s" : "") })]);
        var li = el("li", { class: "attempt" }, [head, sqlNode]);
        list.appendChild(li);
        return sleep(250).then(function () { return typeInto(sqlNode, a.sql || "(no SQL in the reply)", token); })
          .then(function () { return sleep(250); })
          .then(function () {
            if (token.cancelled) return;
            var last = idx === run.attempts.length - 1;
            head.appendChild(attemptTag(a, last));
            if (a.error && a.error_kind && !last) {
              li.appendChild(el("p", { class: "feedback" }, [el("b", { text: "Sent back to the model: " }), a.error]));
            } else if (a.error && a.error_kind) {
              li.appendChild(el("p", { class: "feedback" }, [el("b", { text: "Last attempt: " }), a.error]));
            }
            return sleep(500);
          });
      });
    });
    chain.then(function () {
      if (token.cancelled) return;
      $("r-final").hidden = false;
      $("r-answer").textContent = run.answer || "(The answer step was skipped in this run set to save model calls; it only ran for questions that needed a retry.)";
      $("r-sql").textContent = run.final_sql || "(no query ran)";
      var tableTarget = clear($("r-table")), goldTarget = clear($("r-gold-table")), verdict = clear($("r-verdict"));
      var goldBox = $("r-gold");
      goldBox.hidden = !run.gold_sql;
      $("r-gold-sql").textContent = run.gold_sql || "";
      var predP = run.final_sql ? runGuarded(run.final_sql) : Promise.resolve(null);
      var goldP = run.gold_sql ? runGuarded(run.gold_sql) : Promise.resolve(null);
      return Promise.all([predP, goldP]).then(function (rs) {
        var pred = rs[0], gold = rs[1];
        if (pred) renderTable(tableTarget, pred); else tableTarget.appendChild(el("p", { class: "result-meta", text: "No query ran." }));
        if (!gold) {
          verdict.appendChild(el("span", { class: "tag retry", text: "Demo prompt, not scored" }));
          verdict.appendChild(document.createTextNode(run.note || ""));
          return;
        }
        renderTable(goldTarget, gold);
        var live = pred ? resultsMatch(gold.rows, pred.rows, run.ordered, false) : false;
        verdict.appendChild(el("span", { class: "tag " + (live ? "ok" : "err"), text: live ? "Matches gold" : "Does not match gold" }));
        var txt = "Checked live against the hand-written gold query just now";
        if (live !== run.correct) txt += " (the recorded score said " + (run.correct ? "correct" : "wrong") + ")";
        verdict.appendChild(document.createTextNode(txt + "."));
      }).catch(function (err) {
        verdict.appendChild(el("span", { class: "err-text", text: "Could not run the query here: " + err.message }));
      });
    }).then(function () { btn.disabled = false; });
  }

  function setOrder() {
    var order = [MAIN_SET, "demos", COMPARE_SET, "ablation-bare"];
    return SETS.slice().sort(function (a, b) {
      var ia = order.indexOf(a.name), ib = order.indexOf(b.name);
      return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
    });
  }

  function fillPicker() {
    var sel = $("q-select");
    setOrder().forEach(function (set) {
      var group = el("optgroup", { label: set.label });
      set.runs.forEach(function (r) {
        var flags = [];
        if (r.n_attempts > 1) flags.push(r.n_attempts + " attempts");
        if (r.correct === false) flags.push("wrong");
        group.appendChild(el("option", { value: set.name + "/" + r.id,
          text: r.id + "  " + r.question + (flags.length ? "  [" + flags.join(", ") + "]" : "") }));
      });
      sel.appendChild(group);
    });
    var chips = $("quick-picks");
    var picks = [];
    setOrder().forEach(function (set) {
      set.runs.forEach(function (r) { if (r.n_attempts > 1 || r.correct === false) picks.push([set, r]); });
    });
    if (picks.length) chips.appendChild(el("span", { class: "muted", text: "Runs that needed a retry:" }));
    picks.forEach(function (p) {
      var name = p[1].id + " " + shortModel(p[0].meta.sql_model) + (p[0].name === "demos" ? " demo" : "");
      var b = el("button", { type: "button", text: name, title: p[1].question });
      b.addEventListener("click", function () { sel.value = p[0].name + "/" + p[1].id; replay(sel.value); });
      chips.appendChild(b);
    });
    sel.value = picks.length ? picks[0][0].name + "/" + picks[0][1].id : SETS[0].name + "/" + SETS[0].runs[0].id;
    var d1 = findRun("demos", "d1");
    if (d1) sel.value = "demos/d1";
  }

  function renderScores() {
    var main = setByName[MAIN_SET] || SETS[0], cmp = setByName[COMPARE_SET];
    var s = main.summary, m = main.meta;
    $("score-label").textContent = "Recorded runs, " + m.recorded;

    // 1. run sets side by side
    var cmpT = clear($("sets-table"));
    var heads = [["Model", ""], ["Schema", ""], ["Questions", "hide-sm"], ["Correct", ""], ["Strict", "hide-sm"], ["Retried", ""], ["Cost", ""]];
    cmpT.appendChild(el("thead", null, [el("tr", null, heads.map(function (h) {
      return el("th", { text: h[0], scope: "col", class: h[1] });
    }))]));
    cmpT.appendChild(el("tbody", null, setOrder().filter(function (x) { return x.summary.overall.n; }).map(function (x) {
      var o = x.summary.overall;
      return el("tr", null, [
        el("td", { text: shortModel(x.meta.sql_model) }),
        el("td", { text: x.meta.schema === "bare" ? "bare" : "full" }),
        el("td", { class: "num hide-sm", text: String(o.n) }),
        el("td", { class: "num strong", text: o.correct + " / " + o.n }),
        el("td", { class: "num hide-sm", text: x.summary.overall_strict.correct + " / " + o.n }),
        el("td", { class: "num", text: String(x.summary.needed_retry) }),
        el("td", { class: "num", text: "$" + x.summary.total_cost_usd.toFixed(x.summary.total_cost_usd < 0.1 ? 3 : 2) })
      ]);
    })));

    // 2. main set by difficulty
    var rows = [["Overall", s.overall], ["Easy", s.by_difficulty.easy], ["Medium", s.by_difficulty.medium], ["Hard", s.by_difficulty.hard]];
    var t = clear($("score-table"));
    t.appendChild(el("caption", { text: shortModel(m.sql_model) + ", by difficulty" }));
    t.appendChild(el("thead", null, [el("tr", null, [el("th", { text: "Set" }), el("th", { text: "Correct" }), el("th", { text: "Accuracy" }), el("th", { text: "" })])]));
    t.appendChild(el("tbody", null, rows.map(function (r) {
      var v = r[1];
      var bar = el("div", { class: "bar" }, [el("i", { style: "width:" + (v.accuracy * 100).toFixed(1) + "%" })]);
      return el("tr", null, [el("td", { text: r[0] }), el("td", { text: v.correct + " / " + v.n }),
        el("td", { class: "big", text: pct(v.accuracy) }), el("td", null, [bar])]);
    })));
    var f = clear($("score-facts"));
    f.appendChild(el("p", null, [el("b", { text: String(s.needed_retry) }), " of " + s.overall.n + " questions needed more than one attempt; ",
      el("b", { text: String(s.retry_then_correct) }), " of those ended with a correct result."]));
    var kinds = Object.keys(s.error_kinds).map(function (k) { return s.error_kinds[k] + " " + (KIND_LABEL[k] || k); });
    f.appendChild(el("p", { text: "Failed attempts by kind: " + (kinds.length ? kinds.join(", ") : "none") + "." }));
    f.appendChild(el("p", { class: "muted small", text: "SQL by " + m.sql_model + ", answers by " + m.answer_model + ", through " + m.provider +
      (m.cli_version ? " " + m.cli_version : "") + ". " + s.sql_calls + " SQL calls. Cost is what the CLI reported for the run." }));

    // 3. every question, main vs compare set
    var rt = clear($("runs-table"));
    var heads = [el("th", { text: "Id" }), el("th", { text: "Question" }), el("th", { text: shortModel(m.sql_model) })];
    if (cmp) heads.push(el("th", { text: shortModel(cmp.meta.sql_model) }));
    rt.appendChild(el("thead", null, [el("tr", null, heads)]));
    function cell(setName, r) {
      if (!r) return el("td");
      var txt = r.correct ? "ok" : "wrong";
      var td = el("td", { class: "res" }, [el("span", { class: "tag " + (r.correct ? "ok" : "err"), text: txt })]);
      if (r.n_attempts > 1) td.appendChild(el("span", { class: "muted small", text: " x" + r.n_attempts, title: r.n_attempts + " attempts" }));
      return td;
    }
    rt.appendChild(el("tbody", null, main.runs.map(function (r) {
      var cells = [el("td", { class: "id", text: r.id }), el("td", { text: r.question }), cell(main.name, r)];
      if (cmp) cells.push(cell(cmp.name, findRun(cmp.name, r.id)));
      var tr = el("tr", { tabindex: "0", title: "Replay " + r.id }, cells);
      function go() { $("q-select").value = main.name + "/" + r.id; replay($("q-select").value); $("replay").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth" }); }
      tr.addEventListener("click", go);
      tr.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
      return tr;
    })));
  }

  // ------------------------------------------------------------------
  // Playground
  // ------------------------------------------------------------------
  function runPlayground() {
    var sql = $("sql-box").value;
    var note = clear($("play-guard")), target = clear($("play-table"));
    var g;
    try { g = checkSql(sql, DEFAULT_MAX_ROWS); }
    catch (err) {
      note.appendChild(el("p", { class: "err-text", text: "Blocked by the read-only guard: " + err.message + "." }));
      return;
    }
    if (g.limitAdded || g.limitCapped) {
      note.appendChild(el("p", { text: g.limitCapped ? "The guard lowered the LIMIT to 200. It will run:" : "The guard added a row limit. It will run:" }));
      note.appendChild(el("pre", { class: "sql", text: g.sql }));
    } else {
      note.appendChild(el("p", { class: "ok-text", text: "Passed the guard unchanged." }));
    }
    $("run-btn").disabled = true;
    runGuarded(sql).then(function (res) { renderTable(target, res); })
      .catch(function (err) {
        var msg = /^query stopped/.test(err.message) ? "Stopped: " + err.message.replace(/^query stopped/, "the query was stopped") : "SQLite error: " + err.message;
        target.appendChild(el("p", { class: "err-text", text: msg + "." }));
      })
      .then(function () { $("run-btn").disabled = false; });
  }

  function renderSchema() {
    var q = "SELECT m.name, group_concat(p.name, ', ') FROM sqlite_master m JOIN pragma_table_info(m.name) p " +
      "WHERE m.type = 'table' GROUP BY m.name ORDER BY m.name";
    execSql(q).then(function (res) {
      var box = clear($("schema-list"));
      res.rows.forEach(function (r) {
        box.appendChild(el("p", { class: "schema-table" }, [el("b", { text: r[0] }), " ", el("span", { text: r[1] })]));
      });
    }).catch(function () { /* schema list is optional */ });
  }

  // ------------------------------------------------------------------
  // Boot
  // ------------------------------------------------------------------
  function boot() {
    var status = $("db-status");
    $("run-btn").addEventListener("click", runPlayground);
    $("sql-box").addEventListener("keydown", function (e) {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); runPlayground(); }
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-sql]"), function (b) {
      b.addEventListener("click", function () { $("sql-box").value = b.getAttribute("data-sql"); runPlayground(); });
    });
    $("replay-btn").addEventListener("click", function () { replay($("q-select").value); });
    $("q-select").addEventListener("change", function () { replay($("q-select").value); });

    var runsP = fetch("data/runs.json").then(function (r) { if (!r.ok) throw new Error("runs.json " + r.status); return r.json(); });
    var dbP = fetch("data/chinook.sqlite").then(function (r) { if (!r.ok) throw new Error("chinook.sqlite " + r.status); return r.arrayBuffer(); });

    runsP.then(function (data) {
      SETS = data.sets || [];
      SETS.forEach(function (x) { setByName[x.name] = x; });
      if (!SETS.length) throw new Error("no run sets in runs.json");
      fillPicker();
      renderScores();
    }).catch(function (err) { status.textContent = "Could not load the recorded runs: " + err.message; status.className = "status fail"; });

    dbP.then(function (buf) {
      dbBytes = buf;
      return startWorker();
    }).then(function () {
      status.textContent = "Database loaded in your browser (sql.js 1.13.0, Chinook, 11 tables).";
      status.className = "status ready";
      renderSchema();
      return runsP;
    }).then(function () {
      if (SETS.length) replay($("q-select").value);
    }).catch(function (err) {
      status.textContent = "Could not start the in-browser database: " + err.message;
      status.className = "status fail";
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
