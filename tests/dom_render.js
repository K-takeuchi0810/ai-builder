/* web/app.js を実際に走らせて #markList の HTML を吐き出すハーネス。
 *
 * なぜ必要か: 文字列テンプレートの目視・部分一致では **構造** を保証できない。
 * 実際にあった事故は `<button class="row">` の中に `<button class="term">` を
 * 置いたことで、HTML パーサが内側 button 以降を行の外へ吐き出し、
 * `.why` が `.horse` の子でなくなって「タップしても根拠が開かない」状態になった。
 * テンプレート文字列を grep しても検出できない。
 *
 * 外部ライブラリは入れない (jsdom も使わない)。Node 標準の vm と、
 * 描画に必要な最小限の DOM スタブだけで app.js を評価し、
 * 生成された HTML を標準出力に出す。構造の検査は呼び出し側 (Python の
 * html.parser) で行う — ブラウザと同じ「本物のパーサ」で入れ子を判定する。
 *
 * 使い方:
 *   node tests/dom_render.js <predict.json> [features.json]   … 予想画面
 *   node tests/dom_render.js --build <features.json>          … 作成画面
 *
 * A-2 の反省: 予想画面しか描画していなかったため、作成画面に残っていた
 * 「button の中の button」(pchip の中の ⓘ) を検出できなかった。
 * 作成画面も同じパーサに通す。
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

function makeEl(id) {
  const el = {
    id,
    innerHTML: '',
    textContent: '',
    hidden: false,
    dataset: {},
    classList: {
      _s: new Set(),
      add(...c) { c.forEach((x) => this._s.add(x)); },
      remove(...c) { c.forEach((x) => this._s.delete(x)); },
      contains(c) { return this._s.has(c); },
      toggle(c, force) {
        const want = force === undefined ? !this._s.has(c) : !!force;
        if (want) this._s.add(c); else this._s.delete(c);
        return want;
      },
    },
    addEventListener() {},
    removeEventListener() {},
    setAttribute() {},
    getAttribute() { return null; },
    scrollIntoView() {},
    remove() {},
    querySelector() { return null },
    querySelectorAll() { return [] },
    closest() { return null },
    appendChild() {},
  };
  return el;
}

const els = new Map();
function el(sel) {
  if (!els.has(sel)) els.set(sel, makeEl(sel));
  return els.get(sel);
}

const doc = {
  querySelector: (sel) => el(sel),
  querySelectorAll: () => [],
  getElementById: (id) => el('#' + id),
  addEventListener: () => {},
  createElement: () => makeEl('created'),
  documentElement: makeEl('html'),
  body: makeEl('body'),
};

const sandbox = {
  document: doc,
  window: {
    addEventListener: () => {},
    scrollTo: () => {},
    scrollY: 0,
    innerHeight: 800,
  },
  location: { search: '', hash: '#predict', href: 'http://127.0.0.1/#predict' },
  history: { pushState: () => {}, replaceState: () => {} },
  sessionStorage: { getItem: () => null, setItem: () => {}, clear: () => {} },
  localStorage: undefined,
  URLSearchParams: URLSearchParams,
  // URL ごとに応答を差し替えられるようにする (作成画面は /api/features を読む)
  fetch: async (url) => ({
    ok: true, status: 200,
    json: async () => (sandbox.__routes && sandbox.__routes[url]) || {},
  }),
  setInterval: () => 0,
  clearInterval: () => {},
  setTimeout: (fn) => { if (typeof fn === 'function') fn(); return 0; },
  console,
  JSON,
  Math,
  Number,
  String,
  Object,
  Array,
  Date,
  RegExp,
  Error,
  TypeError,
  isNaN,
  parseInt,
  parseFloat,
  Promise,
  Set,
  Map,
};
sandbox.window.document = doc;
sandbox.globalThis = sandbox;

const src = fs.readFileSync(
  path.join(__dirname, '..', 'web', 'app.js'), 'utf8');
const ctx = vm.createContext(sandbox);
vm.runInContext(src, ctx, { filename: 'app.js' });

const BUILD = process.argv[2] === '--build';
if (BUILD) {
  const features = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
  sandbox.__routes = { '/api/features': features };
  vm.runInContext('loadFeatures().then(() => { __done = true; });', ctx);
  // loadFeatures は await 1 回だけ。マイクロタスクを流してから読み出す。
  setImmediate(() => {
    process.stdout.write(JSON.stringify({
      step1groups: el('#step1groups').innerHTML,
      step2list: el('#step2list').innerHTML,
      starterBox: el('#starterBox').innerHTML,
    }));
  });
  return;
}

const predict = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const features = process.argv[3]
  ? JSON.parse(fs.readFileSync(process.argv[3], 'utf8')) : null;

// 描画に必要な状態を実データで満たす
vm.runInContext('state.config = {id:"t", name:"テストAI", version:1};', ctx);
if (features) {
  sandbox.__features = features;
  vm.runInContext(
    'state.features = __features;'
    + '(__features.glossary||[]).forEach(g => state.glossary[g.key] = g);', ctx);
}
sandbox.__p = predict;
vm.runInContext('renderPredict(__p, null);', ctx);

process.stdout.write(JSON.stringify({
  markList: el('#markList').innerHTML,
  markLegend: el('#markLegend').innerHTML,
  raceHead: el('#raceHead').innerHTML,
  predWarn: el('#predWarn').innerHTML,
  betSlip: el('#betSlip').innerHTML,
  // 買い目は組み立て中の1件 (#bsDraft) と追加済み (#bsResult) が別ノード
  betDraft: el('#bsDraft').innerHTML,
  betResult: el('#bsResult').innerHTML,
}));
