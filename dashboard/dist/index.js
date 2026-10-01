/* Hermes Telemetry Dashboard — HER-30 GUI bundle (v0.2.0)
 * Renders: overview KPIs, metrics drilldown, timeline, install snapshot,
 * package preview (copy button), consent panel. Auto-polls every 30s.
 * SDK-only: no React import, no build step.
 */
(function () {
  "use strict";
  if (!window.__HERMES_PLUGINS__ || !window.__HERMES_PLUGIN_SDK__) return;

  var SDK = window.__HERMES_PLUGIN_SDK__;
  var React = SDK.React;
  var hooks = SDK.hooks;
  var h = React.createElement;
  var API = "/api/plugins/hermes-telemetry-dashboard";

  /* ---------------- helpers ---------------- */
  function num(n) {
    return n == null ? "—" : Number(n).toLocaleString();
  }

  function fmtTime(iso) {
    if (!iso) return "—";
    try { return new Date(iso).toLocaleString(); } catch (e) { return iso; }
  }

  /* Bucket labels: "16g_to_32g" -> "16–32 GB", "gte_5s" -> "≥ 5s",
     "lt_7d" -> "< 7d", "100ms_to_250ms" -> "100–250 ms" */
  function humanBucket(v) {
    if (v == null || v === "") return "—";
    var s = String(v);
    if (s.indexOf("g_to_") !== -1) s = s.replace(/g_to_/g, "–").replace(/g$/i, "") + " GB";
    if (s.indexOf("to_") !== -1 && s.indexOf("–") === -1) s = s.replace(/to_/g, "–");
    if (s.indexOf("lt_") === 0) return "< " + s.slice(3);
    if (s.indexOf("gte_") === 0) return "≥ " + s.slice(4);
    if (s.indexOf("gt_") === 0) return "> " + s.slice(3);
    if (s.indexOf("_to_") !== -1) s = s.replace(/_/g, " ");
    if (s === "unknown") return "unknown";
    return s;
  }

  function humanKey(k) {
    return String(k).replace(/_/g, " ");
  }

  function sendBadge(t) {
    if (!t) return h("span", { className: "htd-badge htd-badge-muted" }, "config unavailable");
    if (t.send === false)
      return h("span", { className: "htd-badge htd-badge-good" }, "send OFF · local-only");
    if (t.send === true)
      return h("span", { className: "htd-badge htd-badge-danger" }, "send ON");
    return h("span", { className: "htd-badge htd-badge-warn" }, "send: " + (t.send == null ? "unknown" : t.send));
  }

  function Table(props) {
    var headers = props.headers, rows = props.rows, empty = props.empty || "no data";
    var thead = h("thead", null, h("tr", null,
      headers.map(function (hd) { return h("th", { key: hd }, hd); })));
    var body;
    if (!rows || rows.length === 0) {
      body = h("tbody", null, h("tr", null,
        h("td", { colSpan: headers.length, className: "htd-muted" }, empty)));
    } else {
      body = h("tbody", null, rows.map(function (row, i) {
        return h("tr", { key: i },
          row.map(function (cell, j) {
            return h("td", { key: j, className: typeof cell === "string" ? undefined : "htd-mono" }, cell);
          }));
      }));
    }
    return h("table", { className: "htd-table" }, thead, body);
  }

  function Panel(props) {
    return h("div", { className: "htd-panel" },
      h("div", { className: "htd-panel-title" }, props.title),
      props.children);
  }

  /* ---------------- main component ---------------- */
  function TelemetryPage() {
    var _v = hooks.useState("overview"), view = _v[0], setView = _v[1];
    var _s = hooks.useState(null), summary = _s[0], setSummary = _s[1];
    var _m = hooks.useState(null), metrics = _m[0], setMetrics = _m[1];
    var _d = hooks.useState(null), drill = _d[0], setDrill = _d[1];
    var _t = hooks.useState(null), timeline = _t[0], setTimeline = _t[1];
    var _i = hooks.useState(null), install = _i[0], setInstall = _i[1];
    var _p = hooks.useState(null), pkg = _p[0], setPkg = _p[1];
    var _e = hooks.useState(null), error = _e[0], setError = _e[1];
    var _c = hooks.useState(false), copied = _c[0], setCopied = _c[1];

    function refresh() {
      SDK.fetchJSON(API + "/summary").then(setSummary).catch(function (e) {
        setError("summary: " + (e && e.message ? e.message : e));
      });
      SDK.fetchJSON(API + "/metrics").then(setMetrics).catch(function (e) {
        setError("metrics: " + (e && e.message ? e.message : e));
      });
      SDK.fetchJSON(API + "/timeline").then(setTimeline).catch(function (e) {
        setError("timeline: " + (e && e.message ? e.message : e));
      });
      SDK.fetchJSON(API + "/install").then(setInstall).catch(function (e) {
        setError("install: " + (e && e.message ? e.message : e));
      });
      SDK.fetchJSON(API + "/package/next").then(setPkg).catch(function (e) {
        setError("package: " + (e && e.message ? e.message : e));
      });
    }

    hooks.useEffect(function () {
      refresh();
      var t = setInterval(refresh, 30000);
      return function () { clearInterval(t); };
    }, []);

    function openMetric(name) {
      SDK.fetchJSON(API + "/metrics/" + encodeURIComponent(name)).then(setDrill).catch(function (e) {
        setError("drilldown: " + (e && e.message ? e.message : e));
      });
    }

    function copyPayload() {
      if (!pkg || !pkg.package || !pkg.package.payload) return;
      var text = JSON.stringify(pkg.package.payload, null, 2);
      navigator.clipboard.writeText(text).then(function () {
        setCopied(true);
        setTimeout(function () { setCopied(false); }, 2000);
      }).catch(function () {
        setCopied("copy failed — select manually");
      });
    }

    /* ----- overview ----- */
    function renderOverview() {
      var t = summary ? summary.telemetry : null;
      var kpis = [
        { label: "metric families", value: summary ? num(summary.metric_families_total) : "…" },
        { label: "total events", value: summary ? num(summary.events_total) : "…" },
        { label: "packages queued", value: summary ? num(summary.packages_queued) + " / " + num(summary.packages_total) : "…" },
        { label: "aggregate rows", value: summary && summary.periods && summary.periods.length ? num(summary.periods[0].aggregate_rows) : "…" }
      ];
      return h("div", null,
        h("div", { className: "htd-kpi-grid" },
          kpis.map(function (k) {
            return h("div", { key: k.label, className: "htd-kpi" },
              h("span", { className: "htd-kpi-label" }, k.label),
              h("span", { className: "htd-kpi-value" }, k.value));
          })),
        renderConsent());
    }

    /* ----- consent panel (informational only, no toggle) ----- */
    function renderConsent() {
      var t = summary ? summary.telemetry : null;
      var consent = summary ? summary.consent : null;
      var rows = [
        ["shared_metrics.enabled", t ? String(t.enabled) : "…"],
        ["shared_metrics.send", t ? String(t.send) : "…"],
        ["open consent windows", consent ? String(consent.open_windows) : "…"],
        ["consent marks", consent && consent.marks && consent.marks.length
          ? consent.marks.map(function (m) { return m.name + " @" + fmtTime(m.stamp); }).join(" · ")
          : "none"]
      ];
      return h(Panel, { title: "Consent / send state" },
        h("div", { className: "htd-row" },
          sendBadge(t),
          h("span", { className: "htd-muted", style: { marginLeft: "8px" } },
            "read-only view — sending stays OFF")),
        h(Table, {
          headers: ["key", "value"],
          rows: rows.map(function (r) { return [r[0], r[1]]; })
        }));
    }

    /* ----- metrics list + drilldown ----- */
    function renderMetrics() {
      if (drill) {
        var dimBlocks = Object.keys(drill.dimensions || {}).map(function (k) {
          var buckets = Object.keys(drill.dimensions[k]).map(function (val) {
            return h("span", { key: val, className: "htd-pill" },
              humanBucket(val) + " → " + num(drill.dimensions[k][val]));
          });
          return h("div", { key: k, className: "htd-dim-block" },
            h("div", { className: "htd-dim-name" }, humanKey(k)),
            h("div", { className: "htd-pill-row" }, buckets));
        });
        return h("div", null,
          h("button", { className: "htd-btn htd-btn-back", onClick: function () { setDrill(null); } },
            "← all metrics"),
          h(Panel, { title: drill.name },
            h("div", { className: "htd-row" },
              h("span", { className: "htd-mono" }, "total: " + num(drill.total)),
              h("span", { className: "htd-mono", style: { marginLeft: "16px" } }, "packaged: " + num(drill.packaged_total))),
            h(Table, {
              headers: ["period", "value", "packaged"],
              rows: (drill.periods || []).map(function (p) {
                return [p.period, num(p.value), num(p.packaged_value)];
              })
            })),
          h(Panel, { title: "dimensions" }, dimBlocks.length ? dimBlocks : h("div", { className: "htd-muted" }, "no dimensions")));
      }
      var list = metrics ? (metrics.metrics || []) : [];
      return h(Panel, { title: "metric families (" + num(list.length) + ")" },
        h("div", { className: "htd-muted", style: { marginBottom: "8px" } },
          "click a row for per-period breakdown + dimension buckets"),
        h("table", { className: "htd-table htd-clickable" },
          h("thead", null, h("tr", null,
            ["metric family", "rows", "total", "packaged", "last period"].map(function (hd) {
              return h("th", { key: hd }, hd);
            }))),
          h("tbody", null, list.map(function (m) {
            return h("tr", { key: m.metric_name, onClick: function () { openMetric(m.metric_name); } },
              h("td", { className: "htd-mono" }, m.metric_name),
              h("td", { className: "htd-mono" }, num(m.rows)),
              h("td", { className: "htd-mono" }, num(m.total)),
              h("td", { className: "htd-mono" }, num(m.packaged)),
              h("td", { className: "htd-mono" }, m.last_period));
          }))));
    }

    /* ----- timeline ----- */
    function renderTimeline() {
      var tl = timeline || {};
      var ms = (tl.milestones || []).map(function (m) {
        return h("div", { key: m.name, className: "htd-tl-item" },
          h("span", { className: "htd-tl-stamp htd-mono" }, fmtTime(m.stamp)),
          h("span", { className: "htd-tl-name" }, humanKey(m.name)));
      });
      var feats = (tl.features || []).map(function (f) {
        return h("div", { key: f.name, className: "htd-tl-item" },
          h("span", { className: "htd-tl-stamp htd-mono" }, fmtTime(f.stamp)),
          h("span", { className: "htd-tl-name" }, humanKey(f.name)));
      });
      return h("div", null,
        h(Panel, { title: "milestones" },
          ms.length ? ms : h("div", { className: "htd-muted" }, "no milestones recorded")),
        h(Panel, { title: "feature flags" },
          feats.length ? feats : h("div", { className: "htd-muted" }, "no features recorded")),
        h("div", { className: "htd-muted" },
          "install: " + (tl.install_id || "—") + " · schema v" + (tl.schema_version || "?") +
          " · snapshot " + fmtTime(tl.install_snapshot_recorded_at)));
    }

    /* ----- install snapshot ----- */
    function renderInstall() {
      var inst = install || {};
      var res = inst.resource || {};
      var buckets = inst.buckets || {};
      var resRows = [
        ["hermes version", res.hermes_version || "—"],
        ["os", res.os_family || "—"],
        ["architecture", res.architecture || "—"],
        ["install method", res.install_method || "—"],
        ["install id", inst.install_id || "—"],
        ["recorded at", fmtTime(inst.recorded_at)]
      ];
      var bucketRows = Object.keys(buckets).map(function (k) {
        return [humanKey(k), humanBucket(buckets[k])];
      });
      return h("div", null,
        h(Panel, { title: "install resource" },
          h(Table, { headers: ["key", "value"], rows: resRows })),
        h(Panel, { title: "install buckets (" + bucketRows.length + ")" },
          h(Table, { headers: ["bucket", "value"], rows: bucketRows, empty: "no snapshot recorded" })));
    }

    /* ----- package preview ----- */
    function renderPackage() {
      if (!pkg) return h(Panel, { title: "package preview" }, "…");
      if (!pkg.package) {
        return h(Panel, { title: "package preview" },
          h("div", { className: "htd-muted" }, "no queued package — nothing would be sent"),
          h("div", { className: "htd-muted" }, "outbox: " + num(pkg.packages_total) + " total, " + num(pkg.packages_queued) + " queued"));
      }
      var p = pkg.package;
      var meta = [
        ["package id", p.package_id || "—"],
        ["period", (p.period_start || "—") + " → " + (p.period_end || "—")],
        ["created", fmtTime(p.created_at)],
        ["send state", p.send_state || "never sent"],
        ["send attempts", String(p.send_attempts == null ? 0 : p.send_attempts)],
        ["last error", p.last_error || "none"]
      ];
      return h("div", null,
        h(Panel, { title: "next package — exactly what would be sent to Nous" },
          h("div", { className: "htd-row" },
            sendBadge(pkg.telemetry),
            h("button", { className: "htd-btn", onClick: copyPayload },
              copied === true ? "✓ copied" : (copied ? copied : "copy payload")),
            h("span", { className: "htd-muted", style: { marginLeft: "8px" } },
              (p.payload && p.payload.metrics ? p.payload.metrics.length : 0) + " metrics · schema " +
              ((p.payload && p.payload.schema_version) || "?"))),
          h(Table, { headers: ["key", "value"], rows: meta }),
          h("pre", { className: "htd-pre" },
            JSON.stringify(p.payload, null, 2))));
    }

    /* ----- nav + shell ----- */
    var tabs = [
      ["overview", "Overview"], ["metrics", "Metrics"],
      ["timeline", "Timeline"], ["install", "Install"], ["package", "Package"]
    ];
    return h("div", { className: "htd-root" },
      h("h2", null, "Hermes Telemetry"),
      error ? h("div", { className: "htd-error" }, "⚠ " + error) : null,
      h("nav", { className: "htd-nav" },
        tabs.map(function (t) {
          return h("button", {
            key: t[0],
            className: "htd-tab" + (view === t[0] ? " htd-tab-active" : ""),
            onClick: function () { setView(t[0]); setError(null); }
          }, t[1]);
        })),
      view === "overview" ? renderOverview() : null,
      view === "metrics" ? renderMetrics() : null,
      view === "timeline" ? renderTimeline() : null,
      view === "install" ? renderInstall() : null,
      view === "package" ? renderPackage() : null,
      h("div", { className: "htd-footer htd-muted" },
        "auto-refreshes every 30s · hard-refresh (Ctrl+Shift+R) after changes"));
  }

  window.__HERMES_PLUGINS__.register("hermes-telemetry-dashboard", TelemetryPage);
})();