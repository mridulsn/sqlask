/* sqlask demo: replay recorded runs, run SQL live with sql.js, show the eval.
   The guard and the scorer are ports of src/sqlask/guard.py and scoring.py.
   ASCII-only source. */
(function () {
  "use strict";

  // ------------------------------------------------------------------
  // Read-only guard (port of guard.py; tested against tests/guard_cases.json)
  // ------------------------------------------------------------------
  var DEFAULT_MAX_ROWS = 200;
  var EVAL_MAX_ROWS = 100000;
  var WS = "[ \\t\\n\\f\\r]", ID_CHAR = "A-Za-z0-9_$\\u0080-\\uffff";
  function word(kw) { return "(?<![" + ID_CHAR + "])" + kw + "(?![" + ID_CHAR + "])"; }
  function asciiUpper(s) { return s.replace(/[a-z]/g, function (ch) { return ch.toUpperCase(); }); }
  function rstripWs(s) { return s.replace(/[ \t\n\f\r]+$/, ""); }
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
    var re = new RegExp("\\(|\\)|" + word("LIMIT"), "g"), m, depth = 0, last = null;
    masked = asciiUpper(masked);
    while ((m = re.exec(masked)) !== null) {
      if (m[0] === "(") depth += 1;
      else if (m[0] === ")") depth -= 1;
      else if (depth === 0) last = { start: m.index, end: m.index + m[0].length };
    }
    return last;
  }

  function checkSql(sql, maxRows) {
    if (maxRows === undefined) maxRows = DEFAULT_MAX_ROWS;
    if (sql === null || sql === undefined || !String(sql).replace(/[ \t\n\f\r]/g, "")) throw GuardError("empty query");
    sql = String(sql);
    var masked = maskSql(sql);
    var stripped = rstripWs(masked);
    while (stripped.endsWith(";")) stripped = rstripWs(stripped.slice(0, -1));
    if (stripped.indexOf(";") !== -1) throw GuardError("only one statement is allowed");
    var body = sql.slice(0, stripped.length);
    masked = stripped;

    var first = /^[ \t\n\f\r]*([A-Za-z]+)/.exec(masked);
    if (!first || ["SELECT", "WITH"].indexOf(first[1].toUpperCase()) === -1) {
      throw GuardError("only SELECT queries are allowed (must start with SELECT or WITH)");
    }
    var upper = asciiUpper(masked);
    for (var f = 0; f < FORBIDDEN.length; f++) {
      if (new RegExp(word(FORBIDDEN[f])).test(upper)) throw GuardError("forbidden keyword: " + FORBIDDEN[f]);
    }
    if (new RegExp(word("REPLACE" + WS + "+INTO")).test(upper)) throw GuardError("forbidden keyword: REPLACE");
    if ((masked.match(/\(/g) || []).length !== (masked.match(/\)/g) || []).length) {
      throw GuardError("unbalanced parentheses");
    }

    var limit = topLevelLimit(masked);
    if (limit === null) return { sql: body + "\nLIMIT " + maxRows, limitAdded: true, limitCapped: false };
    var tail = masked.slice(limit.end);
    var num = new RegExp("^" + WS + "*([0-9]+)" + WS + "*(?:$|" + word("OFFSET") + ")").exec(asciiUpper(tail));
    if (num) {
      var value = parseInt(num[1], 10);
      if (value <= maxRows) return { sql: body, limitAdded: false, limitCapped: false };
      var start = limit.end + /^[ \t\n\f\r]*/.exec(tail)[0].length;
      var end = start + num[1].length;
      return { sql: body.slice(0, start) + String(maxRows) + body.slice(end), limitAdded: false, limitCapped: true };
    }
    return { sql: "SELECT * FROM (\n" + body + "\n) LIMIT " + maxRows, limitAdded: true, limitCapped: false };
  }

  // ------------------------------------------------------------------
  // Execution accuracy (port of scoring.py)
  // ------------------------------------------------------------------
  // Python round(x, 2): round the exact binary64 value, ties to even.
  // Multiplying a double by 100 first loses information near midpoints.
  function round2(v) {
    if (!Number.isFinite(v) || v === 0 || Math.abs(v) >= 1e16) return v + 0;
    var bits = new DataView(new ArrayBuffer(8));
    bits.setFloat64(0, Math.abs(v));
    var hi = bits.getUint32(0), lo = bits.getUint32(4);
    var exponent = (hi >>> 20) & 2047;
    var mantissa = (BigInt(hi & 1048575) << 32n) | BigInt(lo);
    if (exponent) mantissa |= 1n << 52n;
    var shift = (exponent || 1) - 1023 - 52;
    var numerator = mantissa * 100n, cents;
    if (shift >= 0) cents = numerator << BigInt(shift);
    else {
      var denominator = 1n << BigInt(-shift);
      cents = numerator / denominator;
      var twiceRemainder = 2n * (numerator % denominator);
      if (twiceRemainder > denominator || (twiceRemainder === denominator && cents % 2n)) cents += 1n;
    }
    return Number((v < 0 ? "-" : "") + cents.toString() + "e-2") + 0;
  }
  function norm(v) {
    if (v === null || v === undefined) return null;
    if (typeof v === "number") return round2(v);
    if (typeof v === "boolean") return v ? 1 : 0;
    var s = String(v).trim();
    if (s !== "" && /^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(s)) return round2(Number(s));
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
  function resultsMatch(goldRows, predRows, ordered, strict, goldNcols, predNcols) {
    if (!goldRows.length && !predRows.length) {
      if (goldNcols === undefined || predNcols === undefined) return true;
      return strict ? goldNcols === predNcols : predNcols >= Math.ceil(goldNcols / 2);
    }
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
    "var dbs = {}, sqlP = null;",
    "onmessage = function (e) {",
    "  var m = e.data;",
    "  if (m.type === 'open') {",
    "    if (!sqlP) sqlP = initSqlJs({ locateFile: function (f) { return '" + SQLJS + "' + f; } });",
    "    sqlP.then(function (SQL) {",
    "      var db = new SQL.Database(new Uint8Array(m.buf));",
    "      db.exec('PRAGMA query_only = 1');",
    "      dbs[m.name] = db;",
    "      postMessage({ id: m.id, ok: true });",
    "    }, function (err) { postMessage({ id: m.id, ok: false, error: String(err && err.message || err) }); });",
    "  } else if (m.type === 'exec') {",
    "    var stmt = null;",
    "    try {",
    "      var db = dbs[m.db]; if (!db) throw new Error('database not loaded: ' + m.db);",
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

  // database files in data/; the disguised copy is fetched the first time it is needed
  var CHINOOK = "chinook.sqlite", MEDIALIB = "medialib.sqlite";
  var dbBufs = {}, dbReady = {}, worker = null, pending = {}, nextId = 1;

  function openDb(name) { return send({ type: "open", name: name, buf: dbBufs[name].slice(0) }, 30000); }

  function startWorker() {
    var url = URL.createObjectURL(new Blob([workerSrc], { type: "text/javascript" }));
    var owner = new Worker(url);
    worker = owner;
    URL.revokeObjectURL(url);
    owner.onmessage = function (e) {
      var p = pending[e.data.id];
      if (!p || p.owner !== owner || owner !== worker) return;
      delete pending[e.data.id];
      clearTimeout(p.timer);
      if (e.data.ok) p.resolve(e.data); else p.reject(new Error(e.data.error));
    };
    owner.onerror = function () { restartWorker(owner, new Error("database worker failed")); };
    // after a restart (query time limit), reopen every database already fetched
    Object.keys(dbBufs).forEach(function (name) { trackReady(name, openDb(name)); });
  }

  function trackReady(name, promise) {
    dbReady[name] = promise;
    promise.catch(function () { if (dbReady[name] === promise) delete dbReady[name]; });
    return promise;
  }

  function restartWorker(owner, error) {
    if (owner !== worker) return;
    owner.terminate();
    worker = null;
    Object.keys(pending).forEach(function (id) {
      var p = pending[id];
      if (p.owner !== owner) return;
      delete pending[id];
      clearTimeout(p.timer);
      p.reject(error);
    });
    dbReady = {};
    startWorker();
  }

  function ensureDb(name) {
    if (!dbReady[name]) {
      trackReady(name, fetch("data/" + name).then(function (r) {
        if (!r.ok) throw new Error(name + " " + r.status);
        return r.arrayBuffer();
      }).then(function (buf) { dbBufs[name] = buf; return openDb(name); }));
    }
    return dbReady[name];
  }

  function send(msg, timeoutMs) {
    return new Promise(function (resolve, reject) {
      var id = nextId++, owner = worker;
      msg.id = id;
      var timer = setTimeout(function () {
        if (!pending[id] || pending[id].owner !== owner) return;
        restartWorker(owner, new Error("query stopped after " + Math.round(timeoutMs / 1000) + " s time limit"));
      }, timeoutMs);
      pending[id] = { resolve: resolve, reject: reject, timer: timer, owner: owner };
      owner.postMessage(msg);
    });
  }

  function execSql(sql, db) {
    db = db || CHINOOK;
    return ensureDb(db).then(function () { return send({ type: "exec", db: db, sql: sql }, QUERY_TIMEOUT_MS); })
      .then(function (r) { return { columns: r.columns, rows: r.rows, ms: r.ms }; });
  }

  // guard + execute; truncated when the guard's LIMIT was hit
  function runGuarded(sql, db) {
    var g = checkSql(sql, DEFAULT_MAX_ROWS);
    return execSql(g.sql, db).then(function (res) {
      res.guard = g;
      res.truncated = (g.limitAdded || g.limitCapped) && res.rows.length >= DEFAULT_MAX_ROWS;
      return res;
    });
  }

  // Scoring needs complete answers, independent of the interactive 200-row cap.
  // One extra row proves whether the evaluation resource limit was exceeded.
  function runComparison(sql, db) {
    var g = checkSql(sql, EVAL_MAX_ROWS + 1);
    return execSql(g.sql, db).then(function (res) {
      res.guard = g;
      res.truncated = res.rows.length > EVAL_MAX_ROWS;
      res.rowLimit = EVAL_MAX_ROWS;
      res.rows = res.rows.slice(0, EVAL_MAX_ROWS);
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
    if (res.truncated) meta += " (stopped at the " + (res.rowLimit || DEFAULT_MAX_ROWS) + "-row limit)";
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
        if (token.cancelled) return resolve();
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
  var HARD_MAIN = "disguised-sonnet", HARD_COMPARE = "disguised-haiku";
  var TYPE_LABEL = { "value": "Stored value shown in the schema", "hidden-value": "Stored value not shown",
    "multi-join": "Joins across 3-5 tables", "window": "Window functions, ranking", "date-range": "Date ranges",
    "ties": "Ties", "null": "NULL handling" };
  function isHard(set) { return set.meta && set.meta.suite === "disguised"; }
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
    var m = set.meta, dbName = m.database || CHINOOK;
    $("rec-label").textContent = "Recorded run: SQL by " + m.sql_model + (m.answer_model ? ", answer by " + m.answer_model : "") +
      ", via the Claude Code CLI, " + m.recorded + (m.schema === "bare" ? ", bare schema (no sample values)" : "") +
      (isHard(set) ? ", disguised Chinook" : "");
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
        var head = el("div", { class: "attempt-head" }, [el("strong", { text: "Recorded attempt " + a.n }),
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
      var predP = run.ok && run.final_sql ? runComparison(run.model_sql || run.final_sql, dbName) : Promise.resolve(null);
      var goldP = run.gold_sql ? runComparison(run.gold_sql, dbName) : Promise.resolve(null);
      return Promise.all([predP, goldP]).then(function (rs) {
        if (token.cancelled) return;
        var pred = rs[0], gold = rs[1];
        $("r-sql").textContent = pred ? pred.guard.sql : "(no accepted query)";
        if (gold) $("r-gold-sql").textContent = gold.guard.sql;
        if (pred) renderTable(tableTarget, pred); else tableTarget.appendChild(el("p", { class: "result-meta", text: "No query ran." }));
        if (!gold) {
          verdict.appendChild(el("span", { class: "tag retry", text: "Demo prompt, not scored" }));
          verdict.appendChild(document.createTextNode(run.note || ""));
          return;
        }
        renderTable(goldTarget, gold);
        if (gold.truncated || (pred && pred.truncated)) {
          verdict.appendChild(el("span", { class: "tag retry", text: "Inconclusive" }));
          verdict.appendChild(document.createTextNode("Result exceeds the " + EVAL_MAX_ROWS + "-row comparison limit."));
          return;
        }
        var live = pred ? resultsMatch(gold.rows, pred.rows, run.ordered, false, gold.columns.length, pred.columns.length) : false;
        verdict.appendChild(el("span", { class: "tag " + (live ? "ok" : "err"), text: live ? "Matches gold" : "Does not match gold" }));
        var txt = "Checked live against the hand-written gold query just now";
        if (live !== run.correct) txt += " (the recorded score said " + (run.correct ? "correct" : "wrong") + ")";
        verdict.appendChild(document.createTextNode(txt + "."));
      }).catch(function (err) {
        if (token.cancelled) return;
        verdict.appendChild(el("span", { class: "err-text", text: "Could not run the query here: " + err.message }));
      });
    }).then(function () { if (!token.cancelled) btn.disabled = false; });
  }

  function setOrder() {
    var order = [MAIN_SET, "demos", COMPARE_SET, "ablation-bare", HARD_MAIN, HARD_COMPARE];
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
      var name = p[1].id + " " + shortModel(p[0].meta.sql_model) + (p[0].name === "demos" ? " demo" : "") +
        (isHard(p[0]) ? " (disguised)" : "");
      var b = el("button", { type: "button", text: name, title: p[1].question });
      b.addEventListener("click", function () { sel.value = p[0].name + "/" + p[1].id; replay(sel.value); });
      chips.appendChild(b);
    });
    sel.value = picks.length ? picks[0][0].name + "/" + picks[0][1].id : SETS[0].name + "/" + SETS[0].runs[0].id;
    var d1 = findRun("demos", "d1");
    if (d1) sel.value = "demos/d1";
    // deep link: #run=<set>/<id>, e.g. #run=disguised-haiku/x06
    var hm = /^#run=([\w-]+\/[\w-]+)$/.exec(location.hash || "");
    if (hm && findRun(hm[1].split("/")[0], hm[1].split("/")[1])) sel.value = hm[1];
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
    cmpT.appendChild(el("tbody", null, setOrder().filter(function (x) { return x.summary.overall.n && !isHard(x); }).map(function (x) {
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
    renderRunsTable($("runs-table"), main, cmp, false);
  }

  function renderRunsTable(target, main, cmp, showType) {
    var rt = clear(target), m = main.meta;
    var heads = [el("th", { text: "Id" }), el("th", { text: "Question" })];
    if (showType) heads.push(el("th", { text: "Type", class: "hide-sm" }));
    heads.push(el("th", { text: shortModel(m.sql_model) }));
    if (cmp) heads.push(el("th", { text: shortModel(cmp.meta.sql_model) }));
    rt.appendChild(el("thead", null, [el("tr", null, heads)]));
    function cell(r) {
      if (!r) return el("td");
      var txt = r.correct ? "ok" : "wrong";
      var td = el("td", { class: "res" }, [el("span", { class: "tag " + (r.correct ? "ok" : "err"), text: txt })]);
      if (r.n_attempts > 1) td.appendChild(el("span", { class: "muted small", text: " x" + r.n_attempts, title: r.n_attempts + " attempts" }));
      return td;
    }
    rt.appendChild(el("tbody", null, main.runs.map(function (r) {
      var cells = [el("td", { class: "id", text: r.id }), el("td", { text: r.question })];
      if (showType) cells.push(el("td", { class: "muted small hide-sm", text: TYPE_LABEL[r.type] || r.type || "" }));
      cells.push(cell(r));
      if (cmp) cells.push(cell(findRun(cmp.name, r.id)));
      var tr = el("tr", { tabindex: "0", title: "Replay " + r.id }, cells);
      function go() { $("q-select").value = main.name + "/" + r.id; replay($("q-select").value); $("replay").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth" }); }
      tr.addEventListener("click", go);
      tr.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
      return tr;
    })));
  }

  function money(x) { return "$" + x.toFixed(x < 0.1 ? 3 : 2); }

  function renderHard() {
    var a = setByName[HARD_MAIN], b = setByName[HARD_COMPARE];
    if (!a) { $("hard").hidden = true; return; }
    var sets = [a, b].filter(Boolean);
    $("hard-label").textContent = "Recorded runs, " + a.meta.recorded;

    // 1. one row per model
    var t = clear($("hard-sets"));
    var heads = [["Model", ""], ["Correct", ""], ["Strict", "hide-sm"], ["Retried", ""], ["Retry fixed it", ""], ["SQL calls", "hide-sm"], ["Cost", ""]];
    t.appendChild(el("thead", null, [el("tr", null, heads.map(function (h) { return el("th", { text: h[0], scope: "col", class: h[1] }); }))]));
    t.appendChild(el("tbody", null, sets.map(function (x) {
      var s = x.summary, o = s.overall;
      return el("tr", null, [
        el("td", { text: shortModel(x.meta.sql_model) }),
        el("td", { class: "num strong", text: o.correct + " / " + o.n }),
        el("td", { class: "num hide-sm", text: s.overall_strict.correct + " / " + o.n }),
        el("td", { class: "num", text: String(s.needed_retry) }),
        el("td", { class: "num", text: String(s.retry_then_correct) }),
        el("td", { class: "num hide-sm", text: String(s.sql_calls) }),
        el("td", { class: "num", text: money(s.total_cost_usd) })
      ]);
    })));

    // 2. by question type, models side by side
    var types = Object.keys(a.summary.by_type || {});
    var tt = clear($("hard-types"));
    tt.appendChild(el("caption", { text: "By question type" }));
    tt.appendChild(el("thead", null, [el("tr", null, [el("th", { text: "Type" })].concat(sets.map(function (x) {
      return el("th", { text: shortModel(x.meta.sql_model) });
    })))]));
    tt.appendChild(el("tbody", null, types.map(function (k) {
      return el("tr", null, [el("td", { text: TYPE_LABEL[k] || k })].concat(sets.map(function (x) {
        var v = x.summary.by_type[k];
        return el("td", { class: "num" + (v.correct < v.n ? " miss" : ""), text: v.correct + " / " + v.n });
      })));
    })));

    // 3. what the retry loop did
    var f = clear($("hard-facts"));
    sets.forEach(function (x) {
      var s = x.summary;
      var kinds = Object.keys(s.error_kinds).map(function (k) { return s.error_kinds[k] + " " + (KIND_LABEL[k] || k); });
      f.appendChild(el("p", null, [el("b", { text: shortModel(x.meta.sql_model) }), ": " + s.needed_retry + " of " + s.overall.n +
        " questions needed a retry (" + s.retry_ids.join(", ") + "), " + s.retry_then_correct + " ended correct. Failed attempts: " +
        (kinds.length ? kinds.join(", ") : "none") + ". Wrong: " + (s.failed_ids.length ? s.failed_ids.join(", ") : "none") + "."]));
    });
    f.appendChild(el("p", { class: "muted small", text: "SQL by the model named, answers by " + a.meta.answer_model +
      " (only for questions that needed a retry), through " + a.meta.provider + (a.meta.cli_version ? " " + a.meta.cli_version : "") + "." }));

    renderRunsTable($("hard-runs"), a, b, true);
  }

  // ------------------------------------------------------------------
  // Playground
  // ------------------------------------------------------------------
  function playDb() { return $("db-select").value; }

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
    runGuarded(sql, playDb()).then(function (res) { renderTable(target, res); })
      .catch(function (err) {
        var msg = /^query stopped/.test(err.message) ? "Stopped: " + err.message.replace(/^query stopped/, "the query was stopped") : "SQLite error: " + err.message;
        target.appendChild(el("p", { class: "err-text", text: msg + "." }));
      })
      .then(function () { $("run-btn").disabled = false; });
  }

  function renderSchema(db) {
    var q = "SELECT m.name, group_concat(p.name, ', ') FROM sqlite_master m JOIN pragma_table_info(m.name) p " +
      "WHERE m.type = 'table' GROUP BY m.name ORDER BY m.name";
    execSql(q, db).then(function (res) {
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
      b.addEventListener("click", function () {
        $("sql-box").value = b.getAttribute(playDb() === MEDIALIB ? "data-sql-alt" : "data-sql");
        runPlayground();
      });
    });
    var EXAMPLES = {};
    EXAMPLES[CHINOOK] = $("sql-box").value;
    EXAMPLES[MEDIALIB] = "SELECT s.style_label AS style, COUNT(*) AS items\nFROM catalog_item i JOIN style s ON s.style_id = i.style_id\nGROUP BY s.style_id\nORDER BY items DESC";
    $("db-select").addEventListener("change", function () {
      var db = playDb();
      $("sql-box").value = EXAMPLES[db];
      clear($("play-guard")); clear($("play-table"));
      clear($("schema-list")).appendChild(el("p", { class: "muted small", text: "Loading..." }));
      renderSchema(db);
    });
    $("replay-btn").addEventListener("click", function () { replay($("q-select").value); });
    $("q-select").addEventListener("change", function () { replay($("q-select").value); });

    var runsP = fetch("data/runs.json").then(function (r) { if (!r.ok) throw new Error("runs.json " + r.status); return r.json(); });
    startWorker();
    var dbP = ensureDb(CHINOOK);

    runsP.then(function (data) {
      SETS = data.sets || [];
      SETS.forEach(function (x) { setByName[x.name] = x; });
      if (!SETS.length) throw new Error("no run sets in runs.json");
      fillPicker();
      renderScores();
      renderHard();
    }).catch(function (err) { status.textContent = "Could not load the recorded runs: " + err.message; status.className = "status fail"; });

    dbP.then(function () {
      status.textContent = "Database loaded in your browser (sql.js 1.13.0, Chinook, 11 tables). The disguised copy loads when a run needs it.";
      status.className = "status ready";
      renderSchema(CHINOOK);
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
