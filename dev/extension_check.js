/**
 * 検証専用: 拡張の common.js を Node 上でモックした browser API に対して動かす。
 * 実ブラウザでの動作保証ではないが、収集ロジック・整形・送信の筋は確認できる。
 *
 *   node extension_check.js [サーバーURL]
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SERVER = process.argv[2] || null;
const fails = [];

function check(label, cond, detail = "") {
  console.log(`${cond ? "OK " : "NG "} ${label}${detail ? "  " + detail : ""}`);
  if (!cond) fails.push(label);
}

// ---- browser API のモック --------------------------------------------------
const now = Date.now() / 1000;

const containers = [
  { cookieStoreId: "firefox-container-1", name: "xconnecter01", colorCode: "#37adff" },
  { cookieStoreId: "firefox-container-2", name: "xconnecter02", colorCode: "#ac54b8" },
  { cookieStoreId: "firefox-container-3", name: "xconnecter03", colorCode: "#51cd00" },
];

// container-3 はログアウト状態（auth_token 無し）を再現
const jar = {
  "firefox-container-1": [
    { name: "auth_token", value: "TOKEN1", domain: ".x.com", expirationDate: now + 86400 * 60 },
    { name: "ct0", value: "CT01", domain: ".x.com", expirationDate: now + 86400 * 60 },
    { name: "guest_id", value: "g1", domain: ".x.com" },
  ],
  "firefox-container-2": [
    { name: "ct0", value: "CT02", domain: ".x.com" },
    { name: "auth_token", value: "TOKEN2", domain: ".x.com" }, // セッションcookie（期限なし）
  ],
  "firefox-container-3": [
    { name: "guest_id", value: "g3", domain: ".x.com" },
  ],
  "firefox-default": [],
};

let storage = {};
let fpiEnabled = false;   // firstPartyIsolate を有効にした場合の分岐も試す
let requestedOrigins = [];

const browser = {
  storage: {
    local: {
      async get(keys) {
        const out = {};
        for (const k of keys) if (k in storage) out[k] = storage[k];
        return out;
      },
      async set(patch) { Object.assign(storage, patch); },
    },
  },
  contextualIdentities: {
    async query() { return containers; },
  },
  cookies: {
    async getAll({ url, storeId, firstPartyDomain }) {
      // FPI 有効時は firstPartyDomain 未指定だと例外、という Firefox の挙動を模す
      if (fpiEnabled && firstPartyDomain === undefined) {
        throw new Error("firstPartyDomain required");
      }
      if (!url.startsWith("https://x.com")) return []; // twitter.com には何も無い想定
      return (jar[storeId] || []).map((c) => ({ ...c }));
    },
  },
  permissions: {
    async contains() { return false; },
    async request({ origins }) { requestedOrigins.push(...origins); return true; },
  },
  runtime: { openOptionsPage() {} },
};

// ---- common.js を読み込む --------------------------------------------------
const ctx = vm.createContext({
  browser, console, fetch: globalThis.fetch, URL, URLSearchParams, Date, Math, String, Object, JSON,
});
const src = fs.readFileSync(path.join(__dirname, "..", "extension", "common.js"), "utf8");
vm.runInContext(src, ctx);

(async () => {
  console.log("== コンテナ列挙 ==");
  const list = await ctx.listContainers();
  check("既定コンテナを先頭に含む", list[0].cookieStoreId === "firefox-default");
  check("コンテナ3件 + 既定 = 4件", list.length === 4, `${list.length}件`);

  console.log("\n== HttpOnly クッキーの収集 ==");
  const r1 = await ctx.harvestContainer("firefox-container-1");
  check("ログイン済みは ok", r1.status === "ok", JSON.stringify(r1));
  check("auth_token を取れる", r1.authToken === "TOKEN1");
  check("ct0 を取れる", r1.ct0 === "CT01");
  check("期限を取れる", typeof r1.expires === "number");

  const r3 = await ctx.harvestContainer("firefox-container-3");
  check("auth_token 欠落は login_required", r3.status === "login_required", JSON.stringify(r3));
  check("何が無いか報告する", /auth_token/.test(r3.note), r3.note);

  const r2 = await ctx.harvestContainer("firefox-container-2");
  check("セッションcookieでも ok", r2.status === "ok" && !r2.expires, JSON.stringify(r2));

  console.log("\n== firstPartyIsolate 有効時 ==");
  fpiEnabled = true;
  const r1b = await ctx.harvestContainer("firefox-container-1");
  check("FPI 有効でも取れる", r1b.status === "ok" && r1b.authToken === "TOKEN1", JSON.stringify(r1b));
  fpiEnabled = false;

  console.log("\n== 出力フォーマット ==");
  const rows = [
    { username: "xconnecter01", authToken: "TOKEN1", ct0: "CT01" },
    { username: "xconnecter02", authToken: "TOKEN2", ct0: "CT02" },
  ];
  const text = ctx.buildCookiesText(rows);
  const lines = text.split("\n").filter((l) => l.trim() && !l.startsWith("#"));
  check("2行出る", lines.length === 2, `${lines.length}行`);
  check("タブ区切り", lines.every((l) => l.includes("\t")));
  check("scraper/web のパース条件を満たす",
    lines.every((l) => l.split("\t").length === 2 && l.includes("auth_token") && l.includes("ct0")));
  check("値が正しい", lines[0] === "xconnecter01\tauth_token=TOKEN1; ct0=CT01", lines[0]);

  console.log("\n== 期限表示 ==");
  check("セッション", ctx.fmtExpiry(null) === "セッション");
  check("未来", ctx.fmtExpiry(now + 86400 * 30).startsWith("あと2"), ctx.fmtExpiry(now + 86400 * 30));
  check("過去", ctx.fmtExpiry(now - 86400) === "期限切れ");

  console.log("\n== 設定の保存と読み出し ==");
  const d = await ctx.loadSettings();
  check("既定値が入る", d.serverUrl.length > 0 && typeof d.mapping === "object");
  await ctx.saveSettings({ serverUrl: "http://example:1234", mapping: { a: "b" } });
  const s2 = await ctx.loadSettings();
  check("保存が効く", s2.serverUrl === "http://example:1234" && s2.mapping.a === "b");

  console.log("\n== host permission 要求 ==");
  const perm = await ctx.ensureHostPermission("http://epi1-ubu-1:8501");
  check("origin パターンで要求する", perm.ok && requestedOrigins.includes("http://epi1-ubu-1:8501/*"),
    JSON.stringify(requestedOrigins));
  const bad = await ctx.ensureHostPermission("not a url");
  check("不正URLを弾く", !bad.ok, bad.message);

  if (SERVER) {
    console.log("\n== 実サーバーへの送信 ==");
    const res = await ctx.pushCookies(SERVER, text, true);
    check("POST 成功", res.ok, res.message);
    check("merge されたと返る", res.payload && res.payload.merged === true, JSON.stringify(res.payload));

    const bad2 = await ctx.pushCookies(SERVER, "# コメントだけ\n", true);
    check("不正な中身は失敗として扱う", !bad2.ok, bad2.message);

    const bad3 = await ctx.pushCookies("http://127.0.0.1:59998", text, true);
    check("接続不可を握り潰さない", !bad3.ok, bad3.message);
  } else {
    console.log("\n(サーバーURL未指定のため送信テストはスキップ)");
  }

  console.log("\n" + (fails.length === 0 ? "=== 全て通過 ===" : `=== 失敗 ${fails.length}件: ${fails} ===`));
  process.exit(fails.length ? 1 : 0);
})();
