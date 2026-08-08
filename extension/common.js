"use strict";

// X のクッキーが乗りうるオリジン。x.com 優先で見る
const X_ORIGINS = ["https://x.com/", "https://twitter.com/"];

const DEFAULTS = {
  serverUrl: "",
  mapping: {},        // cookieStoreId -> username
  lastPushed: null,   // { usernames: [...], at: ISO文字列 } ※トークンは保存しない
};

async function loadSettings() {
  const got = await browser.storage.local.get(Object.keys(DEFAULTS));
  return Object.assign({}, DEFAULTS, got);
}

async function saveSettings(patch) {
  await browser.storage.local.set(patch);
}

/**
 * コンテナ一覧。既定コンテナ（コンテナ無し）も先頭に入れる。
 */
async function listContainers() {
  const out = [{ cookieStoreId: "firefox-default", name: "（コンテナなし）", colorCode: "#888" }];
  try {
    const ids = await browser.contextualIdentities.query({});
    for (const c of ids) {
      out.push({ cookieStoreId: c.cookieStoreId, name: c.name, colorCode: c.colorCode });
    }
  } catch (e) {
    // コンテナ機能が無効な環境では既定のみ
    console.warn("contextualIdentities 取得失敗:", e);
  }
  return out;
}

/**
 * 指定コンテナの cookie jar から X のクッキーを引く。
 * firstPartyIsolation が有効だと firstPartyDomain 指定が要るので両方試す。
 */
async function getXCookies(storeId) {
  const found = [];
  for (const url of X_ORIGINS) {
    let batch = null;
    try {
      batch = await browser.cookies.getAll({ url, storeId, firstPartyDomain: null });
    } catch (e) {
      try {
        batch = await browser.cookies.getAll({ url, storeId });
      } catch (e2) {
        continue;
      }
    }
    if (batch && batch.length) found.push({ url, cookies: batch });
  }
  return found;
}

/**
 * 1コンテナ分の状態を返す。
 * { status: "ok"|"login_required"|"error", authToken, ct0, expires, note }
 */
async function harvestContainer(storeId) {
  try {
    const groups = await getXCookies(storeId);
    let authToken = null, ct0 = null, expires = null;

    // x.com を優先し、無ければ twitter.com を見る
    for (const g of groups) {
      for (const c of g.cookies) {
        if (c.name === "auth_token" && c.value && !authToken) {
          authToken = c.value;
          expires = c.expirationDate || null;
        } else if (c.name === "ct0" && c.value && !ct0) {
          ct0 = c.value;
        }
      }
      if (authToken && ct0) break;
    }

    if (!authToken || !ct0) {
      const missing = [];
      if (!authToken) missing.push("auth_token");
      if (!ct0) missing.push("ct0");
      return { status: "login_required", note: missing.join(" / ") + " が無い" };
    }
    return { status: "ok", authToken, ct0, expires };
  } catch (e) {
    return { status: "error", note: String(e && e.message ? e.message : e) };
  }
}

function buildCookieStr(authToken, ct0) {
  return `auth_token=${authToken}; ct0=${ct0}`;
}

/**
 * [{username, authToken, ct0}] -> cookies.txt の中身
 * scraper.py / web.py と同じ「username<TAB>auth_token=...; ct0=...」形式。
 */
function buildCookiesText(rows) {
  const lines = ["# username\tauth_token=xxxx; ct0=yyyy"];
  for (const r of rows) {
    lines.push(`${r.username}\t${buildCookieStr(r.authToken, r.ct0)}`);
  }
  return lines.join("\n") + "\n";
}

function fmtExpiry(expires) {
  if (!expires) return "セッション";
  const days = Math.floor((expires * 1000 - Date.now()) / 86400000);
  if (days < 0) return "期限切れ";
  if (days === 0) return "本日中";
  return `あと${days}日`;
}

/**
 * /api/cookies/update に POST。merge=1 で既存行を保持させる。
 */
async function pushCookies(serverUrl, cookiesText, merge = true) {
  const base = String(serverUrl || "").replace(/\/+$/, "");
  if (!base) return { ok: false, message: "サーバーURLが未設定です" };

  const body = new URLSearchParams();
  body.set("cookies_text", cookiesText);
  if (merge) body.set("merge", "1");

  try {
    const res = await fetch(base + "/api/cookies/update", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body.toString(),
    });
    let payload = null;
    try { payload = await res.json(); } catch (e) { /* JSON でない場合 */ }

    if (!res.ok) {
      const err = payload && payload.error ? payload.error : `HTTP ${res.status}`;
      return { ok: false, message: err };
    }
    return { ok: true, message: describePush(payload), payload };
  } catch (e) {
    return { ok: false, message: `接続失敗: ${e && e.message ? e.message : e}` };
  }
}

function describePush(payload) {
  if (!payload) return "送信しました";
  const parts = [`更新 ${payload.updated ?? "?"}垢`];
  if (payload.kept) parts.push(`維持 ${payload.kept}垢`);
  parts.push(`合計 ${payload.count ?? "?"}垢`);
  return parts.join(" / ");
}

/**
 * サーバーURLへの host permission を要求する。
 * URL が可変なので optional_permissions から都度もらう。
 */
async function ensureHostPermission(serverUrl) {
  let origin;
  try {
    origin = new URL(serverUrl).origin + "/*";
  } catch (e) {
    return { ok: false, message: "サーバーURLの形式が不正です" };
  }
  try {
    const has = await browser.permissions.contains({ origins: [origin] });
    if (has) return { ok: true };
    const granted = await browser.permissions.request({ origins: [origin] });
    return granted ? { ok: true } : { ok: false, message: `${origin} へのアクセスが許可されていません` };
  } catch (e) {
    return { ok: false, message: String(e && e.message ? e.message : e) };
  }
}
