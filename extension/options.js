"use strict";

const $ = (id) => document.getElementById(id);
let containers = [];

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

async function init() {
  const settings = await loadSettings();
  $("serverUrl").value = settings.serverUrl || "";

  containers = await listContainers();
  const mapping = settings.mapping || {};

  $("rows").innerHTML = containers.map((c) => `
    <tr>
      <td><span class="dot" style="background:${escapeHtml(c.colorCode || "#888")}"></span>${escapeHtml(c.name)}</td>
      <td><input type="text" data-store="${escapeHtml(c.cookieStoreId)}"
                 value="${escapeHtml(mapping[c.cookieStoreId] || "")}"
                 placeholder="xconnecter01"></td>
    </tr>`).join("");
}

$("autofill").addEventListener("click", () => {
  for (const input of document.querySelectorAll("#rows input")) {
    if (input.value.trim()) continue;
    const c = containers.find((x) => x.cookieStoreId === input.dataset.store);
    // 「（コンテナなし）」は自動では埋めない
    if (c && c.cookieStoreId !== "firefox-default") input.value = c.name.trim();
  }
});

$("save").addEventListener("click", async () => {
  const msg = $("msg");
  const serverUrl = $("serverUrl").value.trim();

  const mapping = {};
  const seen = {};
  for (const input of document.querySelectorAll("#rows input")) {
    const username = input.value.trim().replace(/^@/, "");
    if (!username) continue;
    if (seen[username]) {
      msg.textContent = `ユーザー名が重複しています: ${username}`;
      msg.className = "ng";
      return;
    }
    seen[username] = true;
    mapping[input.dataset.store] = username;
  }

  if (serverUrl) {
    try {
      new URL(serverUrl);
    } catch (e) {
      msg.textContent = "サーバーURLの形式が不正です";
      msg.className = "ng";
      return;
    }
    const perm = await ensureHostPermission(serverUrl);
    if (!perm.ok) {
      msg.textContent = perm.message + "（保存はされます）";
      msg.className = "ng";
    }
  }

  await saveSettings({ serverUrl, mapping });
  if (msg.className !== "ng") {
    msg.textContent = `保存しました（対象 ${Object.keys(mapping).length}垢）`;
    msg.className = "ok";
  }
});

init();
