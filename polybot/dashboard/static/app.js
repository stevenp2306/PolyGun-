"use strict";
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const usd = (v) => v == null ? "–" : (v < 0 ? "-$" : "$") + Math.abs(v).toFixed(2);
const pct = (v) => v == null ? "–" : (v * 100).toFixed(2) + "%";
const num = (v, d = 3) => v == null ? "–" : Number(v).toFixed(d);
const cls = (v) => v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "";
const t = (ts) => ts ? new Date(ts * 1000).toLocaleString() : "–";
const ago = (ts) => ts ? Math.round(Date.now() / 1000 - ts) + "s ago" : "never";

async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: "same-origin", ...opts });
  if (!r.ok) throw new Error(path + " -> " + r.status + " " + (await r.text()));
  return r.json();
}
async function post(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Polybot-Action": "1" },
    body: JSON.stringify(body || {}) });
}
function table(cols, rows) {
  if (!rows.length) return "<p class='note'>Nothing to show.</p>";
  return "<div class='tablewrap'><table><thead><tr>" + cols.map((c) => `<th>${esc(c[0])}</th>`).join("") +
    "</tr></thead><tbody>" + rows.map((r) => "<tr>" + cols.map((c) => `<td>${c[1](r)}</td>`).join("") + "</tr>").join("") +
    "</tbody></table></div>";
}

document.querySelectorAll(".tab").forEach((a) => a.addEventListener("click", (e) => {
  e.preventDefault();
  document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
  document.querySelectorAll(".panel").forEach((x) => x.classList.remove("active"));
  a.classList.add("active");
  $(a.getAttribute("href")).classList.add("active");
  refresh();
}));

let cfgLoaded = false;
async function refresh() {
  try {
    const o = await api("/api/overview");
    $("#modeBadge").textContent = o.mode + (o.simulated ? " (simulated)" : " (REAL MONEY)");
    $("#modeBadge").className = "badge " + o.mode;
    $("#state").textContent = o.state; $("#state").className = "pill " + o.state;
    const banner = $("#banner");
    if (o.kill && o.kill.active) { banner.textContent = "KILL SWITCH ACTIVE - no new orders. Positions are still held and monitored."; banner.className = "banner"; }
    else if (Object.keys(o.locks || {}).length) { banner.textContent = "RISK LOCK: " + Object.keys(o.locks).join(", ") + " - new entries stopped until you reset it."; banner.className = "banner"; }
    else if (o.halt) { banner.textContent = "HALTED: " + o.halt.reason; banner.className = "banner"; }
    else if (o.paused) { banner.textContent = "New entries paused."; banner.className = "banner warn"; }
    else banner.className = "banner hidden";
    const cards = [
      ["Account value (conservative)", usd(o.equity)], ["Cash", usd(o.cash)], ["Available cash", usd(o.available_cash)],
      ["Total exposure", usd(o.exposure)], ["Realized P&L today", `<span class="${cls(o.realized_today)}">${usd(o.realized_today)}</span>`],
      ["P&L today (incl. unrealized)", `<span class="${cls(o.daily_pnl)}">${usd(o.daily_pnl)}</span>`],
      ["Unrealized (at bids, after fees)", `<span class="${cls(o.unrealized_conservative)}">${usd(o.unrealized_conservative)}</span>`],
      ["Total return", pct(o.total_return)], ["Drawdown from peak", pct(o.drawdown_pct)],
      ["Max drawdown", usd(o.max_drawdown_usd)], ["Data feed", `${esc(o.feed.status)}<br><span class='note'>${ago(o.feed.rest_last_success)}</span>`],
      ["Last loop", ago(o.last_tick)], ["Markets eligible / seen", `${o.discovery.eligible} / ${o.discovery.markets_seen}`],
      ["Risk limits triggered", Object.keys(o.locks || {}).length ? `<span class='bad'>${esc(Object.keys(o.locks).join(", "))}</span>` : "<span class='ok'>none</span>"],
    ];
    $("#cards").innerHTML = cards.map(([k, v]) => `<div class="card"><div class="k">${esc(k)}</div><div class="v">${v}</div></div>`).join("");
    $("#gate").innerHTML = o.can_open ? "<li class='ok'>Entry gate open: trades are allowed only when every strategy and risk rule passes.</li>"
      : (o.gate_reasons.concat(o.live_gate && !o.live_gate.allowed ? o.live_gate.failures : [])).map((r) => `<li class='warn'>${esc(r)}</li>`).join("");
    if (o.last_tick_error) $("#gate").innerHTML += `<li class='bad'>last loop error: ${esc(o.last_tick_error)}</li>`;
    $("#feeds").innerHTML = table([["Feed", (r) => esc(r[0])], ["Status", (r) => r[1]]], [
      ["WebSocket", o.feed.ws_connected ? `<span class='ok'>connected</span> (last msg ${ago(o.feed.ws_last_message)}, reconnects ${o.feed.ws_reconnects})` : "<span class='warn'>not connected (REST fallback)</span>"],
      ["REST order books", ago(o.feed.rest_last_success)], ["Clock skew", o.feed.clock_skew_s == null ? "not checked" : num(o.feed.clock_skew_s, 2) + "s"],
      ["Last feed error", esc(o.feed.last_error || "none")],
    ]) + table([["Provider", (p) => esc(p.name)], ["Enabled", (p) => p.enabled ? "<span class='ok'>yes</span>" : `<span class='bad'>no</span> – ${esc(p.disabled_reason)}`],
      ["Last refresh", (p) => ago(p.last_refresh)], ["Events", (p) => p.events_cached], ["Quota left", (p) => esc(p.quota_remaining ?? "–")],
      ["Error", (p) => esc(p.last_error || "")]], o.providers);
    $("#liveGate").innerHTML = o.live_gate ? "<h3>Live gate</h3>" + (o.live_gate.allowed ? "<p class='ok'>open</p>" : "<ul>" + o.live_gate.failures.map((f) => `<li class='bad'>${esc(f)}</li>`).join("") + "</ul>") : "";
    $("#external").innerHTML = (o.external_orders.length || o.external_positions.length) ?
      "<h3>External (not managed by the bot – never touched)</h3>" + table([["Order", (r) => esc(r.id)], ["Side", (r) => esc(r.side)], ["Price", (r) => num(r.price)], ["Size", (r) => num(r.size, 2)]], o.external_orders) +
      table([["Asset", (r) => esc(r.asset)], ["Size", (r) => num(r.size, 2)]], o.external_positions) : "";
    if (o.kill_result && o.kill && o.kill.active) {
      $("#gate").innerHTML += "<li>Kill switch cancel results: " + o.kill_result.results.map((r) => `${esc((r.exchange_order_id || r.order_id).slice(0, 12))}: ${r.confirmed ? "<span class='ok'>confirmed</span>" : "<span class='bad'>NOT confirmed</span>"} (${esc(r.message)})`).join("; ") + "</li>";
    }
    const active = document.querySelector(".panel.active").id;
    if (active === "positions") {
      const ps = await api("/api/positions");
      $("#posTable").innerHTML = table([
        ["", (p) => `<span class="tag ${p.simulated ? "SIM" : "REAL"}">${p.simulated ? "SIM" : "REAL"}</span>`],
        ["Event / outcome", (p) => `${esc(p.event)}<br><b>${esc(p.outcome)}</b>`], ["Shares", (p) => num(p.shares, 2)],
        ["Avg entry (incl. fees)", (p) => num(p.avg_entry, 4)], ["Best bid", (p) => num(p.best_bid)],
        ["Exit value now", (p) => usd(p.exit_value) + (p.exit_shares_absorbable + 1e-6 < p.shares ? `<br><span class='bad'>only ${num(p.exit_shares_absorbable, 2)} sh absorbable</span>` : "")],
        ["Cost basis", (p) => usd(p.cost_basis)], ["P&L after costs", (p) => `<span class="${cls(p.pnl_after_costs)}">${usd(p.pnl_after_costs)} (${pct(p.pnl_pct)})</span>`],
        ["Model prob", (p) => num(p.fair_prob)], ["Edge vs bid", (p) => num(p.edge_vs_bid)],
        ["Why entered / exit plan", (p) => `<details><summary>details</summary><p>${esc(p.entry_reason)}</p><p><b>Exit plan:</b> ${esc(p.exit_plan)}</p><p><b>Now:</b> ${esc((p.exit_check.action || ""))} – ${esc((p.exit_check.reasons || []).join("; "))}</p><pre>${esc(JSON.stringify(p.exit_check.numbers || {}, null, 1))}</pre></details>`],
      ], ps);
    } else if (active === "orders") {
      const os = await api("/api/orders");
      $("#ordTable").innerHTML = table([
        ["", (r) => `<span class="tag ${r.simulated ? "SIM" : "REAL"}">${r.simulated ? "SIM" : "REAL"}</span>`],
        ["Created", (r) => t(r.created_at)], ["Order ID", (r) => esc((r.exchange_order_id || "").slice(0, 18))],
        ["Market", (r) => esc(r.meta.event_title || r.market_id) + "<br>" + esc(r.meta.outcome_label || "")],
        ["Side", (r) => esc(r.side)], ["Type", (r) => esc(r.order_type)], ["Purpose", (r) => esc(r.purpose)],
        ["Limit", (r) => num(r.limit_price)], ["Size", (r) => num(r.size, 2)], ["Filled", (r) => num(r.filled_size, 2)],
        ["Avg fill", (r) => num(r.avg_fill_price, 4)], ["Fees", (r) => num(r.fees, 4)], ["Status", (r) => esc(r.status)],
        ["Updated", (r) => t(r.updated_at)], ["Error", (r) => esc(r.error || "")],
      ], os);
    } else if (active === "opportunities") {
      const op = await api("/api/opportunities");
      let rows = op.candidates;
      if ($("#onlyFair").checked) rows = rows.filter((c) => c.numbers.fair_prob != null);
      rows.sort((a, b) => (b.approved - a.approved) || ((b.numbers.edge_at_best_ask ?? -9) - (a.numbers.edge_at_best_ask ?? -9)));
      $("#oppTable").innerHTML = table([
        ["Decision", (c) => c.approved ? "<b class='ok'>TRADE</b>" : "<span class='note'>skip</span>"],
        ["Event / outcome", (c) => `${esc(c.event)}<br><b>${esc(c.outcome)}</b><br><span class='note'>starts ${t(c.game_start_time)}</span>`],
        ["Est. prob (source)", (c) => c.numbers.fair_prob == null ? "–" : `${num(c.numbers.fair_prob)} ± ${num(c.numbers.uncertainty)}<br><span class='note'>${esc(c.numbers.fair_source)} (${c.numbers.n_sources} books)</span>`],
        ["Bid / ask", (c) => `${num(c.numbers.best_bid)} / ${num(c.numbers.best_ask)}`],
        ["Ask depth (sh)", (c) => num(c.numbers.ask_depth_shares, 0)],
        ["Fees+slippage /sh", (c) => c.numbers.all_in_per_share == null ? num(c.numbers.fee_per_share_at_ask, 4) + " fee" : `fee ${num(c.numbers.fees / c.numbers.shares, 4)}, slip ${num(c.numbers.slippage, 4)}`],
        ["Uncertainty buffer", (c) => num(c.numbers.uncertainty_buffer)],
        ["Edge after costs", (c) => `<span class="${cls(c.numbers.edge ?? c.numbers.edge_at_best_ask)}">${num(c.numbers.edge ?? c.numbers.edge_at_best_ask)}</span> vs req ${num(c.numbers.required_edge)}`],
        ["EV after costs", (c) => c.numbers.ev_total == null ? "–" : usd(c.numbers.ev_total) + ` (${pct(c.numbers.ev_per_dollar)}/$)`],
        ["Reason", (c) => esc(c.reasons.join(" | "))],
      ], rows);
    } else if (active === "performance") {
      const p = await api("/api/performance?period=" + $("#period").value);
      const blk = (x, title) => `<h3>${title}</h3><p class="note">${esc(x.note)}</p>` + table([["Metric", (r) => esc(r[0])], ["Value", (r) => r[1]]], [
        ["Closed trades", x.n_trades], ["Independent events", x.n_events], ["Net realized P&L", usd(x.net_pnl)],
        ["Win rate", pct(x.win_rate)], ["Average win", usd(x.avg_win)], ["Average loss", usd(x.avg_loss)],
        ["Profit factor", esc(x.profit_factor ?? "–")], ["Fees paid", usd(x.fees_paid)], ["Max drawdown", usd(x.max_drawdown_usd)],
        ["Total return", pct(x.total_return)]]) +
        "<h4>By strategy</h4>" + table([["Strategy", (r) => esc(r[0])], ["Trades", (r) => r[1].n_trades], ["Net", (r) => usd(r[1].net_pnl)], ["Win rate", (r) => pct(r[1].win_rate)]], Object.entries(x.by_strategy)) +
        "<h4>By sport / league</h4>" + table([["Group", (r) => esc(r[0])], ["Trades", (r) => r[1].n_trades], ["Net", (r) => usd(r[1].net_pnl)], ["Win rate", (r) => pct(r[1].win_rate)]], Object.entries(x.by_sport));
      $("#perf").innerHTML = blk(p.paper, "PAPER (simulated)") + blk(p.live, "LIVE (real money)");
    } else if (active === "controls" && !cfgLoaded) {
      const c = await api("/api/config");
      cfgLoaded = true;
      $("#cfgKey").innerHTML = c.editable.map((k) => `<option>${esc(k)}</option>`).join("");
      $("#cfg").innerHTML = `<p class='note'>Risk-config hash: ${esc(c.risk_hash)} · Alert channels: ${esc(c.alert_channels.join(", "))}</p>` +
        table([["Setting", (r) => esc(r[0])], ["Value", (r) => esc(r[1])]], Object.entries(c.settings));
      const card = c.strategy_card;
      $("#card").innerHTML = `<p><b>${esc(card.name)}</b> – validated: <b class="${card.validated ? "ok" : "bad"}">${card.validated ? "yes" : "NO"}</b></p>` +
        ["information_used", "fair_value_method", "why_edge_might_exist", "how_tested", "expected_costs"].map((k) => `<p><b>${esc(k.replaceAll("_", " "))}:</b> ${esc(card[k])}</p>`).join("") +
        ["assumptions", "disabled_when", "key_risks"].map((k) => `<p><b>${esc(k.replaceAll("_", " "))}:</b></p><ul>${card[k].map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`).join("");
    } else if (active === "alerts") {
      const a = await api("/api/alerts");
      $("#alertList").innerHTML = "<h3>Alerts</h3>" + table([["Time", (r) => t(r.ts)], ["Level", (r) => `<span class="${r.level === "info" ? "" : "bad"}">${esc(r.level)}</span>`], ["Kind", (r) => esc(r.kind)], ["Message", (r) => esc(r.message)]], a.alerts) +
        "<h3>Audit log</h3>" + table([["Time", (r) => t(r.ts)], ["Actor", (r) => esc(r.actor)], ["Action", (r) => esc(r.action)], ["Details", (r) => `<pre>${esc(JSON.stringify(r.details))}</pre>`]], a.audit);
    }
  } catch (e) {
    $("#banner").textContent = "Dashboard cannot reach the bot: " + e.message; $("#banner").className = "banner";
  }
}

async function act(path, body, confirmText) {
  if (confirmText && !confirm(confirmText)) return;
  try { const r = await post(path, body); alert(r.message || JSON.stringify(r)); } catch (e) { alert(e.message); }
  refresh();
}
$("#btnPause").onclick = () => act("/api/control/pause");
$("#btnResume").onclick = () => act("/api/control/resume");
$("#btnKill").onclick = () => act("/api/control/kill", { reason: "dashboard" },
  "KILL SWITCH: stop all new orders and try to cancel every open order placed by the bot. Positions stay open. Continue?");
$("#btnReset").onclick = () => { const c = prompt("Resetting a risk lock re-allows trading after a loss limit. Type RESET to confirm:"); if (c) act("/api/control/reset-risk-lock", { confirm: c }); };
$("#btnClearKill").onclick = () => { const c = prompt("Type CLEAR KILL to confirm:"); if (c) act("/api/control/clear-kill", { confirm: c }); };
$("#btnClearHalt").onclick = () => { const c = prompt("Only clear a halt after you checked the cause. Type CLEAR HALT:"); if (c) act("/api/control/clear-halt", { confirm: c }); };
$("#btnCfg").onclick = () => act("/api/config", { key: $("#cfgKey").value, value: $("#cfgVal").value, password: $("#cfgPw").value });
$("#period").onchange = refresh; $("#onlyFair").onchange = refresh;
refresh(); setInterval(refresh, 5000);
