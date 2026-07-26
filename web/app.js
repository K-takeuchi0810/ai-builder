/* MAIBuilder UI — API 配線 (UI指示書 v1.0)
 *
 * 原則:
 *  - スコアリングはすべてサーバ。UI は表示のみ。UI 側で再計算しない (§1 DON'T)
 *  - API が返さない数値は推定・補完して表示しない
 *  - 設定の正本はサーバ (/api/configs)。localStorage は使わない
 *  - fetch 失敗で画面を空白や alert() にしない。直前の描画を維持しチップで状態表示 (§4)
 *  - 参加者向け文言に出さない語: 回収率 / ROI / 「列」(→「項目」) /
 *    対抗戦・勝負・煽り系 (→「成績比較」「基準との差」)。設計書 v0.3 §1・§10
 *  - 「人気(市場)」は参加者AIから除外 (判断A)。選択肢はサーバが返すので UI は
 *    ハードコードしない。1番人気との比較表示はベースラインとして正当
 */
'use strict';

const state = {
  config: null,          // { id, name, version } — 保存済みマイAI (サーバが正本)
  selectedRaceId: null,
  raceStatus: {},        // race_id → 'provisional' | 'fixed' | 'closed'
  polling: null,         // 馬体重発表の検知タイマー
  races: [],
  features: null,
  lastPredict: null,     // 印の変動判定に使う前回レスポンス
  stale: false,          // 直近の取得に失敗しているか
};

/* 設定の正本はサーバ。ここに置くのは **id だけ** (リロード後の復元用)。
 * 設定本体は毎回 GET /api/configs/{id} で取り直す (§0 DON'T: 正本を持たない)。 */
const CFG_ID_KEY = 'maib.config_id';

const IS_DEMO = new URLSearchParams(location.search).get('demo') === '1';
const TITLES = {
  races:   ['きょうのレース', '予想できるレースから選べます'],
  build:   ['マイAIをつくる', '重視する項目を選ぶだけ · 2〜3分'],
  predict: ['マイAIの予想', 'タップすると根拠がひらきます'],
  board:   ['本日の成績比較', 'マイAIと基準を並べて確認します'],
};

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const pct = (v) => (v == null ? '—' : `${Math.round(v * 100)}%`);
/* ISO 文字列 ("2026-08-01T15:02:11") → "15:02"。**クライアントの時計は使わない**。 */
const isoHM = (s) => {
  const m = /T(\d{2}):(\d{2})/.exec(String(s || ''));
  return m ? `${m[1]}:${m[2]}` : null;
};

/* ---------------------------------------------------------------- API */
async function api(path, opts) {
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch (e) { body = null; }
  if (!res.ok) {
    const err = new Error((body && body.error) || `HTTP ${res.status}`);
    err.status = res.status; err.body = body;
    throw err;
  }
  return body;
}
const getJSON = (p) => api(p);
const postJSON = (p, obj) => api(p, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(obj),
});

/* 取得失敗を「エラーダイアログ」ではなくチップで見せる (§4 グレースフルデグレード) */
function staleChip(where) {
  state.stale = true;
  const el = $(where);
  if (el) el.innerHTML = `<p class="note"><span class="chip alert">更新できていません</span>
    表示は最後に取得できた内容です。</p>`;
}
function clearStale(where) {
  state.stale = false;
  const el = $(where);
  if (el) el.innerHTML = '';
}

/* ------------------------------------------------------------ 画面遷移 */
function go(key, push = true) {
  $$('.screen').forEach((s) => s.classList.remove('active'));
  $(`#scr-${key}`).classList.add('active');
  $$('nav.tabs button').forEach((b) => b.classList.toggle('on', b.dataset.scr === key));
  $('#screenTitle').textContent = TITLES[key][0];
  $('#screenSub').textContent = TITLES[key][1];
  window.scrollTo({ top: 0 });
  // 相対 URL なので path と ?demo=1 は保持される (ハッシュだけ差し替わる)
  if (push) history.pushState({ scr: key }, '', `#${key}`);
  if (key === 'predict') renderPredictScreen();
  if (key === 'board') loadLeaderboard();
  if (key === 'races') loadRaces();
}
window.addEventListener('popstate', (e) => {
  const key = (e.state && e.state.scr) || 'races';
  go(key, false);
});

/* --------------------------------------------------- 画面1: レース一覧 */
function raceChip(r) {
  if (r.finished) return '<span class="chip">発走済み · 結果待ち</span>';
  if (!r.weight_announced) return '<span class="chip wait">馬体重の発表待ち</span>';
  if (!r.ready) return '<span class="chip muted">分析できる項目がありません</span>';
  if (r.gate_pass_rate != null && r.gate_pass_rate < 1) {
    return '<span class="chip ok">予想できます</span><span class="chip muted">暫定印</span>';
  }
  return '<span class="chip ok">予想できます</span>';
}
function raceStatusOf(r) {
  if (r.finished) return 'closed';
  if (!r.weight_announced) return 'provisional';
  return 'fixed';
}

async function loadRaces() {
  let data;
  try {
    data = await getJSON('/api/races/today');
    clearStale('#racesWarn');
  } catch (err) {
    if (err.status === 409) {
      $('#raceList').innerHTML = `<div class="empty"><div class="t">きょうのレースがまだ準備できていません</div>
        <div class="d">当日のデータ作成が終わると一覧が出ます。</div></div>`;
      return;
    }
    staleChip('#racesWarn');
    return;
  }
  state.races = data.races || [];
  state.races.forEach((r) => { state.raceStatus[r.race_id] = raceStatusOf(r); });
  $('#hdrDate').textContent = formatDate(data.date);

  if (!state.races.length) {
    $('#raceList').innerHTML = `<div class="empty"><div class="t">きょうのレースはありません</div></div>`;
    return;
  }
  // 1日に複数開催があるので競馬場ごとにまとめる。混ぜると 01R が3つ並び、
  // 発走時刻も昇順にならない (実データ: 函館16:05 の次に 福島10:10)。
  const groups = [];
  state.races.forEach((r) => {
    const key = r.track_label || '';
    const last = groups[groups.length - 1];
    if (last && last.key === key) last.races.push(r);
    else groups.push({ key, races: [r] });
  });
  $('#raceList').innerHTML = groups.map((g) => `
    ${g.key ? `<div class="track-head">${esc(g.key)}</div>` : ''}
    ${g.races.map(raceRow).join('')}`).join('');
  $$('#raceList .race-item').forEach((el) => {
    el.addEventListener('click', () => selectRace(el.dataset.race));
  });
}
function raceRow(r) {
  // 日本語ラベルはサーバ (labels.py) の値をそのまま出す。UI に対応表を持たない。
  const meta = [`${r.n_horses}頭`, r.condition_label].filter(Boolean).join(' · ');
  const sel = r.race_id === state.selectedRaceId ? ' on' : '';
  return `<button class="race-item${sel}" data-race="${esc(r.race_id)}">
      <div class="race-time"><div class="t num">${esc(r.start_time || '--:--')}</div>
        <div class="r">${esc(Number(r.race_num))}R</div></div>
      <div class="race-name">
        <div class="n">${esc(r.race_name || '')}</div>
        <div class="meta"><span>${esc(meta)}</span>${raceChip(r)}</div>
      </div>
      <div class="go">›</div>
    </button>`;
}
function formatDate(d) {
  if (!d || d.length !== 8) return '—';
  const dt = new Date(`${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}T00:00:00`);
  const w = '日月火水木金土'[dt.getDay()];
  return `${Number(d.slice(4, 6))}/${Number(d.slice(6, 8))}(${w})`;
}

function selectRace(raceId) {
  state.selectedRaceId = raceId;
  state.lastPredict = null;
  go('predict');
}

/* ------------------------------------------------ 画面2: マイAIをつくる */
const sel = { step1: new Set(), step2: new Map() };  // step2: metric → {matches:Set, lookbacks:Set, on:bool}

async function loadFeatures() {
  try {
    state.features = await getJSON('/api/features');
  } catch (err) {
    staleChip('#racesWarn');
    return;
  }
  const f = state.features;
  $('#step1chips').innerHTML = f.step1.map((s) =>
    `<button class="pchip" data-key="${esc(s.key)}">${esc(s.label)}</button>`).join('');
  $$('#step1chips .pchip').forEach((el) => el.addEventListener('click', () => {
    const k = el.dataset.key;
    if (sel.step1.has(k)) { sel.step1.delete(k); el.classList.remove('on'); }
    else { sel.step1.add(k); el.classList.add('on'); }
    refreshSaveCta();
  }));

  $('#step2list').innerHTML = f.step2_metrics.map((m, i) => {
    const matches = f.step2_matches.map((x, j) =>
      `<button class="cchip" data-kind="match" data-metric="${esc(m.metric)}" data-idx="${j}">${esc(x.label)}</button>`).join('');
    const lbs = f.step2_lookbacks.map((x, j) =>
      `<button class="cchip" data-kind="lb" data-metric="${esc(m.metric)}" data-idx="${j}">${esc(x.label)}</button>`).join('');
    return `<div class="item" data-metric="${esc(m.metric)}">
      <div class="head">
        <label class="toggle">
          <input type="checkbox" data-metric="${esc(m.metric)}" aria-label="${esc(m.label)}を使う">
          <span class="nm">${esc(m.label)}</span>
        </label>
        <span class="st">使わない</span>
        <button class="disc" aria-label="${esc(m.label)}の詳細を開く" aria-expanded="false">▾</button>
      </div>
      <div class="body">
        <div class="mini-label">どの条件のレースで</div>
        <div class="cellrow">${matches}</div>
        <div class="mini-label">どこまでさかのぼる</div>
        <div class="cellrow">${lbs}</div>
      </div>
    </div>`;
  }).join('');

  // §3-1: 項目トグルとアコーディオン開閉は独立した操作
  $$('#step2list .disc').forEach((btn) => btn.addEventListener('click', () => {
    const item = btn.closest('.item');
    const open = item.classList.toggle('open');
    btn.setAttribute('aria-expanded', String(open));
    btn.textContent = open ? '▴' : '▾';
  }));
  $$('#step2list input[type=checkbox]').forEach((cb) => cb.addEventListener('change', () => {
    const metric = cb.dataset.metric;
    const item = cb.closest('.item');
    if (cb.checked) {
      // §3-2: ON 時のデフォルトは「すべて × 直近3走」
      const s = { matches: new Set([idxOf(state.features.step2_matches, (x) => isAll(x.value))]),
                  lookbacks: new Set([idxOf(state.features.step2_lookbacks, (x) => x.value === 3)]) };
      sel.step2.set(metric, s);
      item.classList.add('on', 'open');
      item.querySelector('.disc').textContent = '▴';
      item.querySelector('.disc').setAttribute('aria-expanded', 'true');
    } else {
      sel.step2.delete(metric);
      item.classList.remove('on');
    }
    syncCells(metric);
    refreshSaveCta();
  }));
  $$('#step2list .cchip').forEach((chip) => chip.addEventListener('click', () => {
    const metric = chip.dataset.metric;
    const s = sel.step2.get(metric);
    if (!s) {   // 項目トグルOFFのままセルを押したらONにする (迷子防止)
      const cb = $(`#step2list input[data-metric="${cssEsc(metric)}"]`);
      cb.checked = true; cb.dispatchEvent(new Event('change'));
      return;
    }
    const set = chip.dataset.kind === 'match' ? s.matches : s.lookbacks;
    const idx = Number(chip.dataset.idx);
    if (set.has(idx)) {
      if (set.size === 1) return;   // §3-2: 条件≥1 かつ 期間≥1 を必須
      set.delete(idx);
    } else set.add(idx);
    syncCells(metric);
    refreshSaveCta();
  }));
  refreshSaveCta();
}
const idxOf = (arr, pred) => { const i = arr.findIndex(pred); return i < 0 ? 0 : i; };
const isAll = (v) => v == null || (Array.isArray(v) && v.length === 0);
const cssEsc = (s) => String(s).replace(/["\\]/g, '\\$&');

/* §3-3: サマリーは選択状態と常に同期する */
function syncCells(metric) {
  const item = $(`#step2list .item[data-metric="${cssEsc(metric)}"]`);
  const s = sel.step2.get(metric);
  item.querySelectorAll('.cchip').forEach((c) => {
    const set = !s ? null : (c.dataset.kind === 'match' ? s.matches : s.lookbacks);
    c.classList.toggle('on', !!set && set.has(Number(c.dataset.idx)));
  });
  const st = item.querySelector('.st');
  if (!s) { st.textContent = '使わない'; return; }
  st.textContent = `${summary(state.features.step2_matches, s.matches)} × ${summary(state.features.step2_lookbacks, s.lookbacks)}`;
}
function summary(all, set) {
  const idx = Array.from(set).sort((a, b) => a - b);
  if (!idx.length) return '未選択';
  const first = all[idx[0]].label;
  return idx.length === 1 ? first : `${first} ほか${idx.length - 1}`;
}

/* §3 DO: 何も選ばれていなければ保存CTAを無効化し、理由を出す */
function refreshSaveCta() {
  const n = sel.step1.size + sel.step2.size;
  const btn = $('#saveBtn');
  btn.disabled = n === 0;
  $('#saveReason').textContent = n === 0
    ? '項目を1つ以上選んでください'
    : `${n}項目を選択中`;
}

function buildConfig() {
  const step2 = [];
  sel.step2.forEach((s, metric) => {
    s.matches.forEach((mi) => s.lookbacks.forEach((li) => {
      step2.push({
        metric,
        match: state.features.step2_matches[mi].value || [],
        lookback: state.features.step2_lookbacks[li].value,
      });
    }));
  });
  return { name: $('#aiName').value.trim() || '参加者AI',
           step1: Array.from(sel.step1), step2 };
}

async function saveConfig() {
  const btn = $('#saveBtn');
  btn.disabled = true;
  $('#saveReason').textContent = '保存しています…';
  try {
    const saved = await postJSON('/api/configs', { config: buildConfig() });
    state.config = { id: saved.id, name: saved.name, version: saved.version };
    try { sessionStorage.setItem(CFG_ID_KEY, saved.id); } catch (e) { /* 非対応環境は無視 */ }
    $('#saveReason').textContent = `保存しました(${esc(saved.name)} v${saved.version})`;
    await loadBacktest();
    if (state.selectedRaceId) go('predict'); else go('races');
  } catch (err) {
    $('#saveReason').textContent = '保存できませんでした。もう一度お試しください。';
    btn.disabled = false;
  }
}

/* バックテスト結果 (的中率系のみ。回収率は表示しない) */
async function loadBacktest() {
  const box = $('#btResult');
  let bt;
  try {
    bt = await postJSON('/api/backtest', { config: buildConfig() });
  } catch (err) {
    box.classList.add('hidden');
    return;
  }
  const noRaces = (bt.warnings || []).some((w) => w.code === 'no_races_in_period');
  const you = bt.your_ai || {}, base = bt.baseline_favorite || {};
  if (noRaces || !you.races) {
    box.classList.remove('hidden');
    box.innerHTML = `<div class="card"><div class="bt-cell">
      <div class="k">これまでの成績</div>
      <div class="b">該当期間にレースがありません</div></div></div>`;
    return;
  }
  box.classList.remove('hidden');
  box.innerHTML = `<div class="section-label">このマイAIのこれまでの成績</div>
    <div class="card"><div class="bt-grid">
      ${btCell('◎が1着だった割合', pct(you.hit_rate_win), `1番人気AI ${pct(base.hit_rate_win)}`)}
      ${btCell('◎が3着以内', pct(you.hit_rate_show), `1番人気AI ${pct(base.hit_rate_show)}`)}
      ${btCell('印の中に勝ち馬', pct(you.hit_rate_in_marks), `1番人気AI ${pct(base.hit_rate_in_marks)}`)}
      ${btCell('対象レース数', `${you.races}`, `${esc(bt.period[0])}以降`)}
    </div></div>
    <p class="note" style="margin-top:8px">${esc(bt.note || '')}</p>`;
}
const btCell = (k, v, b) => `<div class="bt-cell"><div class="k">${esc(k)}</div>
  <div class="v num">${esc(v)}</div><div class="b">${esc(b)}</div></div>`;

/* ------------------------------------------------------- 画面3: 予想 */
function renderPredictScreen() {
  const noAi = !state.config;
  const noRace = !state.selectedRaceId;
  $('#predEmpty').classList.toggle('hidden', !noAi);
  $('#predNoRace').classList.toggle('hidden', noAi || !noRace);
  $('#predBody').classList.toggle('hidden', noAi || noRace);
  if (!noAi && !noRace) loadPredict();
  stopPolling();
  if (!noAi && !noRace) startPolling();
}

async function loadPredict() {
  let p;
  try {
    p = await postJSON('/api/predict',
      { race_id: state.selectedRaceId, config: buildConfig() });
    clearStale('#predWarn');
  } catch (err) {
    staleChip('#predWarn');
    return;
  }
  const prev = state.lastPredict;
  state.lastPredict = p;
  renderPredict(p, prev);
}

function renderPredict(p, prev) {
  const conf = p.confidence || {};
  // 分母は **参加者が選んだ項目数**。API の columns は重み0の列を含まないので、
  // それを分母にすると「使えなかった項目」が見えなくなる。
  const used = p.n_columns_used != null
    ? p.n_columns_used : (p.columns || []).filter((c) => c.decision === 'used').length;
  const total = p.n_columns_selected != null ? p.n_columns_selected : used;

  $('#raceHead').innerHTML = `
    <div class="top">
      <span class="place">${esc(p.race_num || '')}R</span>
      <h2>${esc(p.race_name || '')}</h2>
      <span class="off num">発走 ${esc(p.start_time || '--:--')}</span>
    </div>
    <div class="bottom">
      <button class="badge-conf" id="confBadge">${esc(conf.label || '—')}</button>
      <span class="head-chip">分析に使えた項目 ${used}/${total}</span>
      ${p.weight_announced ? '' : '<span class="head-chip">暫定印(馬体重の発表前)</span>'}
      ${p.odds_trusted === false ? '<span class="head-chip">オッズが古い可能性</span>' : ''}
      <span class="ai">予想: <b>${esc(state.config ? state.config.name : '')}</b></span>
    </div>`;
  $('#confBadge').addEventListener('click', () => $('#legend').classList.add('show'));

  // §4: warnings は印リストの上に出す。重み0/全列落ちなら印は出さない
  const warns = p.warnings || [];
  $('#predWarn').innerHTML = warns.map((w) => `<div class="warn-card">
      <div class="m">${esc(w.message)}</div>
      ${w.hint ? `<div class="h">${esc(w.hint)}</div>` : ''}
      ${warnColumns(w)}</div>`).join('');
  // 印が意味を持たない警告が1つでもあれば印を出さない。ここは **列挙で塞ぐ**
  // (新しい警告コードが増えたときに黙って印を出してしまわないよう、
  //  「印を出しても良い警告」の側を列挙する)
  const HARMLESS = ['low_sample_columns', 'excluded_columns_dropped'];
  const blocked = warns.some((w) => !HARMLESS.includes(w.code));
  if (blocked) { $('#markList').innerHTML = ''; return; }

  const marks = p.marks || [];
  const maxAbs = Math.max(...marks.map((m) => Math.abs(m.score || 0)), 1e-9);
  const prevMark = {};
  if (prev) (prev.marks || []).forEach((m) => { prevMark[m.horse_num] = m.mark; });

  $('#markList').innerHTML = marks.map((m) => {
    const isHon = m.mark === '◎';
    const upset = isHon && m.popularity != null && m.popularity !== 1;
    const w = Math.max(2, Math.round((Math.abs(m.score || 0) / maxAbs) * 100));
    const promoted = prev && prevMark[m.horse_num] && prevMark[m.horse_num] !== m.mark
      && rankOf(m.mark) < rankOf(prevMark[m.horse_num]);
    return `<div class="horse${isHon ? ' hon' : ''}${promoted ? ' flash' : ''}" data-num="${esc(m.horse_num)}">
      <button class="row">
        <span class="mark${isHon ? ' hon' : ''}${m.mark ? '' : ' none'}">${esc(m.mark || '–')}</span>
        <span class="waku w${wakuColor(m.horse_num, marks.length)}">${esc(String(Number(m.horse_num)))}</span>
        <div class="who">
          <div class="name">${esc(m.horse_name || '')}
            ${upset ? '<span class="badge-upset">1番人気ではない</span>' : ''}</div>
          <div class="sub">${popLabel(m)}${oddsLabel(m, p)}${coverChip(m)}</div>
        </div>
        <div class="scorebar"><div class="bar"><i style="width:${w}%"></i></div></div>
      </button>
      ${whyBlock(m)}
    </div>`;
  }).join('');

  $$('#markList .row').forEach((row) => row.addEventListener('click', () => {
    row.parentElement.classList.toggle('open');
  }));
}
/* 警告に項目の内訳が付いている場合 (学習サンプルが薄い項目など) は名前を出す。
 * 「何件か」ではなく「どの項目か」が分からないと参加者は判断できない。 */
function warnColumns(w) {
  if (!w.columns || !w.columns.length) return '';
  const rows = w.columns.map((c) => `<div>${esc(c.label)}${
    c.train_races == null ? '' : ` — 過去 ${Number(c.train_races)}レースで学習`}</div>`).join('');
  const more = w.n_columns > w.columns.length
    ? `<div>ほか ${w.n_columns - w.columns.length}件</div>` : '';
  return `<div class="warn-cols">${rows}${more}</div>`;
}

const rankOf = (mk) => ['◎', '○', '▲', '△', '×'].indexOf(mk);
const wakuColor = (num, n) => {
  const k = Number(num) || 1;
  return Math.min(8, Math.max(1, n <= 8 ? k : Math.ceil(k / Math.ceil(n / 8))));
};
const popLabel = (m) => (m.popularity == null ? '' : `<span>${Number(m.popularity)}番人気</span>`);
/* §4: オッズには必ず取得時刻を添える。時刻はサーバの odds_fetched_at のみを使い、
 * 取れないときは時刻を出さず「配信オッズ」と書く (クライアント時計で代用しない)。 */
function oddsLabel(m, p) {
  if (m.odds == null) return '';
  const hm = isoHM(p && p.odds_as_of);
  // 「オッズが古い可能性」はレース単位の性質なので、行ごとに繰り返さずヘッダに1回出す
  const when = hm ? `${hm}時点` : '配信オッズ';
  return `<span class="num">単勝 ${m.odds.toFixed(1)}(${esc(when)})</span>`;
}
/* ヘッダの「分析に使えた項目 9/12」は **レース単位** (選んだ項目のうち使えた数)。
 * こちらは **馬単位** (使えた項目のうちこの馬に値があった数)。同じ言葉だと分母が
 * 違うのに同義に見えるので、馬側は「この馬のデータ」と言い分ける。 */
function coverChip(m) {
  const c = m.coverage || {};
  if (c.n_used == null || c.n_with_value == null) return '';
  if (c.n_with_value >= c.n_used) return '';
  return `<span class="chip muted">この馬のデータ ${c.n_with_value}/${c.n_used}</span>`;
}

/* 学習サンプルが薄い項目は、寄与の行にその事実を添える。
 * 15,000レースで学習した項目と 25レースの項目を同じ見た目で並べない。 */
const thinNote = (c) => (c.low_sample
  ? `<small>この項目は過去 ${Number(c.train_races)}レース分の学習です</small>` : '');

function whyBlock(m) {
  const cs = (m.contributions || []).filter((c) => c.available || c.contribution !== 0);
  const na = (m.contributions || []).filter((c) => !c.available);
  const maxAbs = Math.max(...cs.map((c) => Math.abs(c.contribution || 0)), 1e-9);
  // 寄与の絶対値は項目によって桁が違う (人気 1.8 に対し集計項目は 0.01 台) ので、
  // 生の数値は出さず **その馬の押し上げ量に占める割合** で示す。桁に依存せず、
  // 「どの項目がこの印を決めたのか」という参加者の問いに直接答える。
  const total = cs.reduce((s, c) => s + Math.abs(c.contribution || 0), 0);
  const rows = cs.map((c) => {
    const v = c.contribution || 0;
    const cls = v >= 0 ? 'plus' : 'minus';
    const w = Math.max(2, Math.round((Math.abs(v) / maxAbs) * 100));
    const share = total > 0 ? Math.round((Math.abs(v) / total) * 100) : null;
    return `<div class="lbl">${esc(c.label)}${thinNote(c)}</div>
      <div class="cbar"><i class="${cls}" style="width:${w}%"></i></div>
      <div class="v ${cls} num">${share == null ? '—' : `${v >= 0 ? '+' : '−'}${share}%`}</div>`;
  }).join('');
  const naRows = na.map((c) => `<div class="lbl">${esc(c.label)}${thinNote(c)}</div>
      <div class="cbar"></div><div class="v na">データなし</div>`).join('');
  const cov = (m.coverage || {});
  const short = cov.n_with_value != null && cov.n_used != null && cov.n_with_value < cov.n_used;
  // 行のチップと同じ内容を再掲しない。ここでは「なぜ確かさが下がるか」の理由文だけ出す。
  // スコアの絶対値 (重み付き z の総和) は参加者に意味を持たないので出さない (§7)。
  // 設計書 §5: 無印も全頭表示し、「なぜ無印か」を一文添える
  const title = m.mark
    ? `なぜこの馬が ${esc(m.mark)} か`
    : `なぜ無印か — 選んだ項目での評価が ${Number(m.rank)}番目で、印は上位5頭まで`;
  return `<div class="why">
    <div class="why-title">${title}</div>
    <div class="contrib">${rows}${naRows}</div>
    <div class="cover-why">数値は、この馬の評価を動かした量のうち各項目が占める割合です。
      ${short ? '「データなし」の項目は評価に加えていません。その分だけ評価の確かさは下がります。'
        : ''}</div>
    ${m.n_past_runs != null
      ? `<div class="score-line">この馬の過去 ${m.n_past_runs}走を参照しています</div>` : ''}
  </div>`;
}

/* --------------------------------- 馬体重発表のライブ更新 (§6・ポーリング) */
function startPolling() {
  state.polling = setInterval(async () => {
    if (!state.selectedRaceId) return;
    let data;
    try { data = await getJSON('/api/races/today'); } catch (e) { return; }
    const r = (data.races || []).find((x) => x.race_id === state.selectedRaceId);
    if (!r) return;
    const before = state.races.find((x) => x.race_id === state.selectedRaceId) || {};
    state.races = data.races;
    if (!before.weight_announced && r.weight_announced) {
      const prev = state.lastPredict;
      await loadPredict();
      announceChanges(prev, state.lastPredict);
    }
  }, 30000);
}
function stopPolling() {
  if (state.polling) { clearInterval(state.polling); state.polling = null; }
}
function announceChanges(prev, now) {
  if (!prev || !now) return;
  const before = {}; (prev.marks || []).forEach((m) => { before[m.horse_num] = m.mark; });
  const promoted = (now.marks || []).find((m) =>
    before[m.horse_num] && before[m.horse_num] !== m.mark
    && rankOf(m.mark) < rankOf(before[m.horse_num]));
  if (!promoted) { toast('馬体重が発表されました。印は変わりませんでした。'); return; }
  toast(`馬体重が発表されました。<b>${Number(promoted.horse_num)}番が ${esc(before[promoted.horse_num])} → ${esc(promoted.mark)} に昇格</b>しました。`);
}
function toast(html) {
  const t = $('#toast');
  t.innerHTML = html;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 5200);
}

/* ------------------------------------------ 画面4: 本日の成績比較 (§7) */
async function loadLeaderboard() {
  let d;
  try {
    d = await getJSON('/api/leaderboard');
    clearStale('#boardWarn');
  } catch (err) {
    if (err.status === 409) {
      $('#boardList').innerHTML = `<div class="empty"><div class="t">まだ集計できていません</div>
        <div class="d">レースが確定すると成績が出ます。</div></div>`;
      return;
    }
    staleChip('#boardWarn');
    return;
  }
  $('#boardSub').textContent = `◎的中数で並べています · ${d.n_races_finished}レース終了時点`;
  $('#boardRule').textContent = d.ranking_rule || '';
  const entries = d.entries || [];
  if (!entries.length) {
    $('#boardList').innerHTML = `<div class="empty"><div class="t">参加者のマイAIがまだありません</div></div>`;
    return;
  }
  $('#boardList').innerHTML = entries.map((e) => {
    const top = e.rank === 1;
    const stat = e.is_baseline
      ? 'いつも1番人気を◎にするAI'
      : `複勝率 ${pct(e.show_rate)} · 人気を出し抜いた的中 <b>${e.upset_hits}回</b>`;
    return `<div class="brow${top ? ' top' : ''}${e.is_baseline ? ' baseline' : ''}">
      <div class="rank">${e.is_baseline ? '—' : esc(String(e.rank))}</div>
      <div class="who"><div class="aname">${esc(e.name)}${e.is_baseline ? '(基準)' : ''}</div>
        <div class="astat">${stat}</div></div>
      <div class="hits"><div class="n num">${e.win_hits}</div><div class="l">◎的中</div></div>
    </div>`;
  }).join('');
}

/* ------------------------------------------------------------- 起動 */
function init() {
  $$('nav.tabs button').forEach((b) => b.addEventListener('click', () => go(b.dataset.scr)));
  $$('[data-go]').forEach((b) => b.addEventListener('click', () => go(b.dataset.go)));
  $('#saveBtn').addEventListener('click', saveConfig);
  $('#legendClose').addEventListener('click', () => $('#legend').classList.remove('show'));
  $('#legend').addEventListener('click', (e) => {
    if (e.target.id === 'legend') $('#legend').classList.remove('show');
  });

  // §6: デモボタンは ?demo=1 のときだけ DOM に存在させる
  if (IS_DEMO) {
    $('#demoStrip').classList.remove('hidden');
    $('#demoBtn').addEventListener('click', simulateWeight);
  } else {
    const strip = $('#demoStrip');
    if (strip) strip.remove();
  }

  history.replaceState({ scr: 'races' }, '', location.hash || '#races');
  loadRaces();
  loadFeatures().then(restoreConfig).then(() => {
    const key = (location.hash || '#races').slice(1);
    if (TITLES[key] && key !== 'races') go(key, false);
  });
}

/* リロード後の復元: sessionStorage の id からサーバの設定を取り直して選択状態に戻す。
 * 設定本体をクライアントに保存しないので、サーバ側で消えた設定は復元されない (正しい)。 */
async function restoreConfig() {
  let id = null;
  try { id = sessionStorage.getItem(CFG_ID_KEY); } catch (e) { id = null; }
  if (!id || !state.features) return;
  let got;
  try { got = await getJSON(`/api/configs/${encodeURIComponent(id)}`); } catch (e) { return; }
  if (!got || !got.config) return;
  state.config = { id: got.id, name: got.name, version: got.version };
  applyConfigToForm(got.config, got.name);
}

/* サーバの設定 → STEP1/STEP2 の選択状態。セルは value から index を引き直す。 */
function applyConfigToForm(cfg, name) {
  sel.step1 = new Set(cfg.step1 || []);
  sel.step2 = new Map();
  $$('#step1chips .pchip').forEach((el) =>
    el.classList.toggle('on', sel.step1.has(el.dataset.key)));

  (cfg.step2 || []).forEach((cell) => {
    const mi = state.features.step2_matches.findIndex(
      (x) => JSON.stringify(x.value || []) === JSON.stringify(cell.match || []));
    const li = state.features.step2_lookbacks.findIndex((x) => x.value === cell.lookback);
    if (mi < 0 || li < 0) return;
    const s = sel.step2.get(cell.metric)
      || { matches: new Set(), lookbacks: new Set() };
    s.matches.add(mi); s.lookbacks.add(li);
    sel.step2.set(cell.metric, s);
  });

  $$('#step2list .item').forEach((item) => {
    const on = sel.step2.has(item.dataset.metric);
    item.classList.toggle('on', on);
    item.querySelector('input[type=checkbox]').checked = on;
    syncCells(item.dataset.metric);
  });
  if (name) $('#aiName').value = name;
  refreshSaveCta();
}

/* デモ: サーバに触らず「発表された場合の見え方」を再現する。リロードでリセット。 */
let demoDone = false;
async function simulateWeight() {
  if (demoDone || !state.lastPredict) return;
  demoDone = true;
  const prev = JSON.parse(JSON.stringify(state.lastPredict));
  const marks = state.lastPredict.marks || [];
  if (marks.length >= 2) {
    const a = marks[0], b = marks[1];
    [a.mark, b.mark] = [b.mark, a.mark];
    marks.sort((x, y) => rankOf(x.mark) - rankOf(y.mark));
  }
  renderPredict(state.lastPredict, prev);
  announceChanges(prev, state.lastPredict);
  const btn = $('#demoBtn');
  btn.textContent = '✓ 印が更新されました(実際は自動で更新・通知されます)';
  btn.disabled = true;
}

document.addEventListener('DOMContentLoaded', init);
