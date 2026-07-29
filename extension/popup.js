"use strict";

let state = { settings: null, rows: [], containers: [] };

const $ = (id) => document.getElementById(id);

function setMsg(text, kind) {
  const el = $("msg");
  el.textContent = text || "";
  el.className = kind || "";
}

async function refresh() {
  $("recheck").disabled = true;
  setMsg("");

  const settings = await loadSettings();
  state.settings = settings;
  $("server").textContent = settings.serverUrl || "サーバー未設定";

  const containers = await listContainers();
  state.containers = containers;

  const mapping = settings.mapping || {};
  const targets = containers.filter((c) => mapping[c.cookieStoreId]);

  if (!targets.length) {
    $("body").innerHTML =
      '<div class="empty">コンテナと垢の対応が未設定です。<br>下の「設定」から割り当ててください。</div>';
    state.rows = [];
    updateButtons();
    $("recheck").disabled = false;
    return;
  }

  const rows = [];
  for (const c of targets) {
    const r = await harvestContainer(c.cookieStoreId);
    rows.push({
      container: c.name,
      color: c.colorCode,
      username: mapping[c.cookieStoreId],
      ...r,
    });
  }
  state.rows = rows;
  render();
  updateButtons();
  $("recheck").disabled = false;
}

function render() {
  const rows = state.rows;
  const html = [
    "<table><thead><tr><th>コンテナ</th><th>垢</th><th>状態</th></tr></thead><tbody>",
  ];
  for (const r of rows) {
    const ok = r.status === "ok";
    const label = ok
      ? `<span class="ok">OK</span> <span class="note">${escapeHtml(fmtExpiry(r.expires))}</span>`
      : `<span class="ng">${r.status === "login_required" ? "要ログイン" : "エラー"}</span>` +
        (r.note ? ` <span class="note">${escapeHtml(r.note)}</span>` : "");
    html.push(
      `<tr><td><span class="dot" style="background:${escapeHtml(r.color || "#888")}"></span>` +
        `${escapeHtml(r.container)}</td>` +
        `<td class="user">${escapeHtml(r.username)}</td><td>${label}</td></tr>`
    );
  }
  html.push("</tbody></table>");
  $("body").innerHTML = html.join("");

  const okCount = rows.filter((r) => r.status === "ok").length;
  const warn = $("warn");
  const last = state.settings.lastPushed;
  if (okCount && last && last.usernames && okCount < last.usernames.length) {
    const missing = last.usernames.filter(
      (u) => !rows.some((r) => r.status === "ok" && r.username === u)
    );
    if (missing.length) {
      warn.style.display = "block";
      warn.innerHTML =
        `前回は ${last.usernames.length}垢 送信済み。今回取得できていないのは ` +
        `<b>${escapeHtml(missing.join(", "))}</b>。<br>` +
        `送信は merge なのでサーバー上の行は消えませんが、これらは古い値のままになります。`;
      return;
    }
  }
  warn.style.display = "none";
}

function updateButtons() {
  const okCount = state.rows.filter((r) => r.status === "ok").length;
  $("copy").disabled = okCount === 0;
  $("push").disabled = okCount === 0;
  $("push").textContent = okCount ? `送信 (${okCount}垢)` : "送信";
}

function okRows() {
  return state.rows.filter((r) => r.status === "ok");
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

$("recheck").addEventListener("click", refresh);

$("copy").addEventListener("click", async () => {
  const text = buildCookiesText(okRows());
  try {
    await navigator.clipboard.writeText(text);
    setMsg(`cookies.txt 形式で ${okRows().length}垢分をコピーしました`, "ok");
  } catch (e) {
    setMsg("コピー失敗: " + e.message, "ng");
  }
});

$("push").addEventListener("click", async () => {
  const rows = okRows();
  if (!rows.length) return;

  $("push").disabled = true;
  setMsg("送信中…");

  const perm = await ensureHostPermission(state.settings.serverUrl);
  if (!perm.ok) {
    setMsg(perm.message, "ng");
    $("push").disabled = false;
    return;
  }

  const res = await pushCookies(state.settings.serverUrl, buildCookiesText(rows), true);
  if (res.ok) {
    setMsg(res.message, "ok");
    // トークンは保存しない。次回の欠落検知用にユーザー名だけ残す
    await saveSettings({
      lastPushed: { usernames: rows.map((r) => r.username), at: new Date().toISOString() },
    });
    state.settings = await loadSettings();
  } else {
    setMsg(res.message, "ng");
  }
  $("push").disabled = false;
});

$("opts").addEventListener("click", (e) => {
  e.preventDefault();
  browser.runtime.openOptionsPage();
});

refresh();
