/**
 * 検証専用: 投稿モーダルの複数選択と自動下書きロジックを、
 * 最小限のDOMモックの上で実際に動かして確認する。
 *
 *   node dev/tumblr_ui_check.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const fails = [];
function check(label, cond, detail = "") {
  console.log(`${cond ? "OK " : "NG "} ${label}${detail ? "  " + detail : ""}`);
  if (!cond) fails.push(label);
}

// ---- 最小のDOMモック ------------------------------------------------------
function makeEl(tag = "div") {
  const el = {
    tagName: tag,
    className: "",
    textContent: "",
    innerHTML: "",
    checked: false,
    disabled: false,
    value: "",
    style: {},
    dataset: {},
    children: [],
    classList: {
      _has: new Set(),
      add(c) { this._has.add(c); },
      remove(c) { this._has.delete(c); },
      toggle(c, on) { on ? this._has.add(c) : this._has.delete(c); },
      contains(c) { return this._has.has(c); },
    },
    appendChild(c) { this.children.push(c); c.parent = el; return c; },
    querySelector(sel) { return (this.querySelectorAll(sel) || [])[0] || null; },
    querySelectorAll(sel) {
      if (sel === ".tmb-badge") {
        return this.children.filter((c) => c.className === "tmb-badge");
      }
      return [];
    },
  };
  return el;
}

const ids = {};
function el(id, tag) {
  if (!ids[id]) ids[id] = makeEl(tag);
  return ids[id];
}

// 使う要素を先に用意する
["tmb", "tmb-imgs", "tmb-imgcount", "tmb-selall", "tmb-draft",
 "tmb-draft-auto", "tmb-go", "tmb-caption", "tmb-tags", "tmb-msg",
 "tmb-account"].forEach((id) => el(id));

// モーダル本体としきい値
el("tmb").dataset.draftThreshold = "3";

const imgsBox = el("tmb-imgs");
imgsBox.innerHTML = "";
Object.defineProperty(imgsBox, "innerHTML", {
  get() { return ""; },
  set(v) { if (v === "") this.children.length = 0; },
});

const document_ = {
  getElementById: (id) => ids[id] || null,
  querySelectorAll(sel) {
    if (sel === "#tmb-imgs .tmb-cell") return imgsBox.children;
    return [];
  },
  createElement: (tag) => makeEl(tag),
};

// ---- テスト対象の関数だけを _scripts.html から抜き出す --------------------
const html = fs.readFileSync(
  path.join(__dirname, "..", "app", "templates", "_scripts.html"), "utf8");

// 選択・自動下書き・ボタン文言まわりだけを取り出す。
// ネットワークを触る tmbLoadAccounts / tmbPost は範囲外。
const src = [
  html.slice(html.indexOf("let tmbCtx"), html.indexOf("// 投稿先の一覧を一度だけ読み込む")),
  html.slice(html.indexOf("// byUser=true はチェックボックス"), html.indexOf("function tmbClose()")),
].join("\n")
  // vm のコンテキストから状態を読み書きできるよう let → var に置換する。
  // スコープが関数外という点は同じで、挙動は変わらない。
  .replace(/^let (tmbCtx|tmbSel|tmbDraftTouched)\b/gm, "var $1");

const ctx = vm.createContext({
  document: document_, console, Array, String, parseInt, isNaN, Object, JSON,
  atob: (b) => Buffer.from(b, "base64").toString("latin1"),
  fetch: async () => ({ json: async () => ({ accounts: [] }) }),
});
vm.runInContext(src, ctx);

// ---- 画像n枚を並べた状態を作る -------------------------------------------
function setup(n) {
  imgsBox.children.length = 0;
  for (let i = 0; i < n; i++) {
    const cell = makeEl("div");
    cell.className = "tmb-cell";
    const badge = makeEl("span");
    badge.className = "tmb-badge";
    cell.appendChild(badge);
    imgsBox.appendChild(cell);
  }
  ctx.tmbSel = Array.from({ length: n }, (_, i) => i);
  ctx.tmbDraftTouched = false;
  el("tmb-draft").checked = false;
  el("tmb-draft-auto").style = {};
  el("tmb-selall").style = {};
  ctx.tmbRenderSelection();
}

const sel = () => ctx.tmbSel;
const draft = () => el("tmb-draft").checked;
const label = () => el("tmb-imgcount").textContent;

console.log("== 既定は全選択 ==");
setup(4);
check("4枚とも選択される", sel().length === 4, JSON.stringify(sel()));
check("枚数表示", label().includes("4枚中 4枚"), label());
check("4枚なので自動で下書きON", draft() === true);
check("投稿ボタンが有効", el("tmb-go").disabled === false);

console.log("\n== 個別に外せる ==");
ctx.tmbToggleOne(1);
check("1枚外れる", sel().length === 3 && !sel().includes(1), JSON.stringify(sel()));
check("3枚なのでまだ下書きON", draft() === true);
ctx.tmbToggleOne(2);
check("2枚になる", sel().length === 2, JSON.stringify(sel()));
check("しきい値未満で下書きOFFに戻る", draft() === false);

console.log("\n== 選択順が保持される ==");
setup(3);
ctx.tmbToggleAll(null);              // 全解除
check("全解除できる", sel().length === 0, JSON.stringify(sel()));
check("0枚だと投稿ボタンが無効", el("tmb-go").disabled === true);
ctx.tmbToggleOne(2);
ctx.tmbToggleOne(0);
check("選んだ順に並ぶ", JSON.stringify(sel()) === "[2,0]", JSON.stringify(sel()));
const badges = imgsBox.children.map((c) => c.querySelector(".tmb-badge").textContent);
check("バッジが選択順を示す", badges[2] === "1" && badges[0] === "2", JSON.stringify(badges));
check("未選択はバッジ空", badges[1] === "", JSON.stringify(badges));

console.log("\n== 全選択トグル ==");
setup(3);
ctx.tmbToggleAll(null);
check("全選択→全解除", sel().length === 0);
check("リンク文言が全選択に", el("tmb-selall").textContent === "全選択",
  el("tmb-selall").textContent);
ctx.tmbToggleAll(null);
check("全解除→全選択", sel().length === 3);
check("リンク文言が全解除に", el("tmb-selall").textContent === "全解除");

console.log("\n== 自動下書きのしきい値 ==");
setup(2);
check("2枚なら下書きOFF", draft() === false);
ctx.tmbToggleAll(null); ctx.tmbToggleAll(null);
setup(3);
check("3枚ちょうどで下書きON", draft() === true);
check("説明が表示される", el("tmb-draft-auto").style.display === "");

console.log("\n== 手動操作を尊重する ==");
setup(4);
check("最初は自動でON", draft() === true);
el("tmb-draft").checked = false;
ctx.tmbDraftToggled(true);           // 人が手動でOFFにした
check("説明が消える", el("tmb-draft-auto").style.display === "none");
ctx.tmbToggleOne(0);                 // 選択を変えても
check("手動OFFが維持される", draft() === false, "自動判定が上書きしてはいけない");
ctx.tmbToggleOne(0);
check("再選択しても維持", draft() === false);

console.log("\n== しきい値0なら自動化しない ==");
el("tmb").dataset.draftThreshold = "0";
setup(5);
check("5枚でも下書きOFF", draft() === false);
check("説明も出ない", el("tmb-draft-auto").style.display === "none");
el("tmb").dataset.draftThreshold = "3";

console.log("\n== 1枚のとき ==");
setup(1);
check("1枚は選択済み", sel().length === 1);
check("下書きにはならない", draft() === false);
check("枚数表示が簡潔", label() === "画像（1枚）", label());
check("全選択リンクを隠す", el("tmb-selall").style.display === "none");

console.log("\n== ボタン文言 ==");
setup(3);
check("下書き時は「下書きに保存」", el("tmb-go").textContent === "下書きに保存",
  el("tmb-go").textContent);
el("tmb-draft").checked = false;
ctx.tmbDraftToggled(true);
check("公開時は「投稿する」", el("tmb-go").textContent === "投稿する",
  el("tmb-go").textContent);

console.log("\n" + (fails.length === 0
  ? "=== 全て通過 ===" : `=== 失敗 ${fails.length}件: ${fails} ===`));
process.exit(fails.length ? 1 : 0);
