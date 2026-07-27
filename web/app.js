/* MAIBuilder UI — API 配線
 *
 * 原則:
 *  - スコアリングはすべてサーバ。UI は表示のみ。UI 側で再計算しない
 *  - API が返さない数値は推定・補完して表示しない
 *  - 設定の正本はサーバ (/api/configs)。localStorage は使わない
 *  - fetch 失敗で画面を空白や alert() にしない。直前の描画を維持しチップで状態表示
 *  - **説明文を UI に書かない**。用語辞書は labels.py が正本で /api/features 経由
 *  - 参加者向け文言に出さない語: 回収率 / ROI / 「列」(→「項目」) /
 *    対抗戦・勝負・煽り系 (→「成績比較」「基準との差」)
 *  - 「人気(市場)」は参加者AIから除外。選択肢はサーバが返すので UI はハードコードしない
 *  - 競馬初心者が読んで意味が通ること。既定は簡潔、説明はタップで開く
 */
'use strict';

const state = {
  config: null,          // { id, name, version } — 保存済みマイAI (サーバが正本)
  selectedRaceId: null,
  polling: null,         // 馬体重発表の検知タイマー
  races: [],
  features: null,
  glossary: {},          // term key → {term, desc}
  lastPredict: null,     // 印の変動判定に使う前回レスポンス
  stale: false,          // 直近の取得に失敗しているか
};

/* 設定の正本はサーバ。ここに置くのは **id だけ** (リロード後の復元用)。
 * 設定本体は毎回 GET /api/configs/{id} で取り直す。 */
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
/* "20250701" → "2025年7月1日"。日付整形はここだけ (表記ゆれを作らない)。 */
function ymd(s) {
  const m = /^(\d{4})(\d{2})(\d{2})$/.exec(String(s || ''));
  return m ? `${Number(m[1])}年${Number(m[2])}月${Number(m[3])}日` : String(s || '');
}

/* ---------------------------------------------------------------- API */
async function api(path, opts) {
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch (e) { body = null; }
  if (!res.ok) {
    const err = new Error((body && body.error) || `HTTP ${res.status}`);
    err.status = res.status; err.body = body;
    // 起動後にサーバ側コードが変わると、無い経路が 404 になって理由が分からない。
    // 失敗のたびに動作モードを確かめ、古いプロセスなら画面に出す。
    if (path !== '/api/version') maybeWarnStale();
    throw err;
  }
  return body;
}

let staleWarned = false;
async function maybeWarnStale() {
  if (staleWarned) return;
  let v;
  try { v = await getJSON('/api/version'); } catch (e) { return; }
  if (!v || !v.stale) return;
  staleWarned = true;
  const el = $('#racesWarn') || $('#predWarn');
  if (el) el.innerHTML = `<div class="warn-card"><div class="m">${esc(v.message)}</div></div>`;
  toast(v.message);
}
const getJSON = (p) => api(p);
const postJSON = (p, obj) => api(p, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(obj),
});

/* 取得失敗を「エラーダイアログ」ではなくチップで見せる (グレースフルデグレード) */
/* 何が更新できていないのか・いつの表示なのかを添える。
 * 「更新できていません」だけでは、参加者は何を疑えばいいのか分からない。 */
function staleChip(where, what, asOf) {
  state.stale = true;
  const el = $(where);
  if (!el) return;
  const subject = what || '情報';
  const when = asOf ? `(表示は${esc(asOf)}時点)` : '(表示は最後に取得できた内容)';
  el.innerHTML = `<p class="note"><span class="chip alert">${esc(subject)}`
    + `を更新できていません</span> ${when}</p>`;
}
function clearStale(where) {
  state.stale = false;
  const el = $(where);
  if (el) el.innerHTML = '';
}

/* ------------------------------------------------ 用語のワンタップ説明 */
/* 説明文はサーバ (labels.py) が正本。UI は表示するだけ。 */
function term(key, text) {
  const g = state.glossary[key];
  if (!g) return esc(text == null ? '' : text);
  return `<button class="term" data-term="${esc(key)}"
    aria-label="${esc(g.term)}の説明">${esc(text == null ? g.term : text)}</button>`;
}
function openSheet(title, bodyHtml) {
  $('#sheetTitle').textContent = title;
  $('#sheetBody').innerHTML = bodyHtml;
  $('#sheet').classList.add('show');
}
function openTermSheet(key) {
  const g = state.glossary[key];
  if (!g) return;
  openSheet(g.term + (g.reading ? `(${g.reading})` : ''), `<p>${esc(g.desc)}</p>`);
}
function openMarkSheet() {
  const rows = (state.features && state.features.mark_legend || []).map((m) =>
    `<div class="sheet-row"><b>${esc(m.term)}</b><span>${esc(m.desc)}</span></div>`).join('');
  openSheet('印の意味', rows +
    '<p class="sheet-foot">印はマイAIの評価順です。上位5頭に付きます。</p>');
}

/* ------------------------------------------------------------ 画面遷移 */
function go(key, push = true) {
  $$('.screen').forEach((s) => s.classList.remove('active'));
  $(`#scr-${key}`).classList.add('active');
  $$('nav.tabs button').forEach((b) => b.classList.toggle('on', b.dataset.scr === key));
  $('#screenTitle').textContent = TITLES[key][0];
  $('#screenSub').textContent = TITLES[key][1];
  $('#miniHead').hidden = true;
  window.scrollTo({ top: 0 });
  // 選択レースをURLに載せる。リロード・共有・戻るで同じレースに戻れる。
  // 相対 URL なので path と ?demo=1 は保持される (ハッシュだけ差し替わる)。
  const hash = (key === 'predict' && state.selectedRaceId)
    ? `#predict/${encodeURIComponent(state.selectedRaceId)}` : `#${key}`;
  if (push) history.pushState({ scr: key, raceId: state.selectedRaceId }, '', hash);
  if (key === 'predict') renderPredictScreen();
  if (key === 'board') loadLeaderboard();
  if (key === 'races') loadRaces();
}
/* "#predict/2025070501010201" → {key:'predict', raceId:'2025...'} */
function parseHash() {
  const raw = (location.hash || '#races').slice(1);
  const [key, rest] = raw.split('/');
  return { key: TITLES[key] ? key : 'races',
           raceId: rest ? decodeURIComponent(rest) : null };
}
window.addEventListener('popstate', (e) => {
  const p = parseHash();
  const key = (e.state && e.state.scr) || p.key;
  const raceId = (e.state && e.state.raceId) || p.raceId;
  if (raceId && raceId !== state.selectedRaceId) {
    state.selectedRaceId = raceId;
    state.lastPredict = null;
  }
  go(key, false);
});

/* --------------------------------------------------- 画面1: レース一覧 */
function raceChip(r) {
  if (r.finished) return '<span class="chip">終了</span>';
  // 「暫定印」= 馬体重の発表前という意味。項目のカバレッジ不足は別のことなので
  // 同じ言葉を使わない (終了レースに「暫定印」が付いて見えたのはこの混同が原因)。
  if (!r.weight_announced) return '<span class="chip wait">馬体重の発表待ち</span>';
  if (!r.ready) return '<span class="chip muted">分析できる項目がありません</span>';
  // 使えない項目のチップは行に出さない。全レースに同じ内容が並ぶと情報にならない
  // ので、共通なら一覧上部に1回だけ出す (gateNotice)。
  return '<span class="chip ok">予想できます</span>';
}

/* 使えない項目が全レース共通なら、一覧上部に1回だけ具体名で知らせる。
 * レースごとに違う場合は行数が多いので件数だけを出す。 */
function gateNotice(races) {
  const upcoming = races.filter((r) => !r.finished && (r.n_gate_missing || 0) > 0);
  if (!upcoming.length) return '';
  const sets = upcoming.map((r) => (r.gate_missing_columns || []).slice().sort().join('|'));
  const common = sets.every((x) => x === sets[0]);
  const groups = groupMissing(upcoming[0].gate_missing_columns || []);
  if (common && groups.length) {
    return `<div class="gate-note">${esc(groups.join('・'))}は、`
      + `きょうのレースでは使えません (印は残りの項目で付けています)</div>`;
  }
  return `<div class="gate-note">${upcoming.length}レースで一部の項目が使えません`
    + `(各レースの予想画面に内訳が出ます)</div>`;
}
/* 列IDを参加者向けの括りにまとめる (「賞金系」「コーナー系」)。
 * 列IDそのものは出さない (開発者語彙)。 */
function groupMissing(ids) {
  const names = new Set();
  ids.forEach((id) => {
    if (id.includes('prize')) names.add('獲得本賞金');
    else if (id.includes('corner') || id.includes('gain')) names.add('コーナー通過順位');
  });
  return [...names];
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
    staleChip('#racesWarn', 'レース一覧');
    return;
  }
  state.races = data.races || [];
  $('#hdrDate').textContent = formatDate(data.date);

  if (!state.races.length) {
    $('#raceList').innerHTML = `<div class="empty"><div class="t">きょうのレースはありません</div></div>`;
    return;
  }
  // 1日に複数開催があるので競馬場ごとにまとめる。混ぜると 1R が3つ並び、
  // 発走時刻も昇順にならない (実データ: 函館16:05 の次に 福島10:10)。
  // 終了したレースは末尾の別グループへ分ける (印は発走前のレースのもの)。
  // 終了レースも会場別のまとまりを保つ。過去日を開くと全レースが終了済みなので、
  // ひとつの「終了」グループにまとめると会場の構造が消えてしまう。
  const groups = [];
  const push = (r, done) => {
    const key = (done ? '終了 · ' : '') + (r.track_label || '');
    const last = groups[groups.length - 1];
    if (last && last.key === key) last.races.push(r);
    else groups.push({ key, races: [r], done });
  };
  state.races.filter((r) => !r.finished).forEach((r) => push(r, false));
  state.races.filter((r) => r.finished).forEach((r) => push(r, true));

  // 会場チップ (タップで該当会場へ) — 3,200px のリストを素通りさせない
  const venues = [];
  groups.forEach((g, i) => {
    if (!g.key || g.done) return;
    venues.push({ id: `grp${i}`, label: g.key });
  });
  $('#raceList').innerHTML = gateNotice(state.races)
    + (venues.length > 1 ? `<div class="venue-chips">${venues.map((v) =>
      `<button class="vchip" data-jump="${v.id}">${esc(v.label)}</button>`
    ).join('')}</div>` : '')
    + groups.map((g, i) => `
    <div class="race-group${g.done ? ' done' : ''}" id="grp${i}">
      ${g.key ? `<div class="track-head">${esc(g.key)}</div>` : ''}
      <div class="card">${g.races.map(raceRow).join('')}</div>
    </div>`).join('');
  $$('#raceList .race-item').forEach((el) => {
    el.addEventListener('click', () => selectRace(el.dataset.race));
  });
  $$('#raceList .vchip').forEach((el) => el.addEventListener('click', () => {
    const t = document.getElementById(el.dataset.jump);
    if (t) t.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }));
  scrollToNextRace();
}

/* 一覧を開いたら「いま見るべきレース」を視界に入れる (0スクロールで到達)。
 * 発走時刻はサーバの値、現在時刻は端末の時計 — 表示の並びを決めるだけで、
 * 予想や集計には一切使わない。全レース終了の日は先頭のまま動かさない。 */
function scrollToNextRace() {
  const now = new Date();
  const hm = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
  const next = state.races.find((r) => !r.finished && (r.start_time || '') >= hm)
    || state.races.find((r) => !r.finished);
  if (!next) return;
  const el = document.querySelector(`#raceList .race-item[data-race="${cssEsc(next.race_id)}"]`);
  if (el) el.scrollIntoView({ behavior: 'auto', block: 'center' });
}
/* 1行目 = 特別戦名があればそれ、なければクラス。2行目 = 施行条件 (常設)。
 * 表示名スロットに条件を焼き込むと、実名を持つ特別戦で名前が条件を上書きして
 * 芝/ダート・距離が画面から消える。**条件行は名称の有無に関わらず必ず出す。** */
function raceTitleOf(r) {
  return r.race_title || r.race_class || `${Number(r.race_num)}R`;
}
function raceCondOf(r) {
  const surface = r.surface_label || '';
  const dist = r.distance ? `${r.distance}m` : '';
  return [surface + dist, r.n_horses ? `${r.n_horses}頭` : '', r.condition_label]
    .filter(Boolean).join(' · ');
}
function raceRow(r) {
  // 日本語ラベルはサーバ (labels.py) の値をそのまま出す。UI に対応表を持たない。
  const sel = r.race_id === state.selectedRaceId ? ' on' : '';
  const cls = (r.race_title && r.race_class) ? `<span class="rcls">${esc(r.race_class)}</span>` : '';
  return `<button class="race-item${sel}" data-race="${esc(r.race_id)}">
      <div class="race-time"><div class="t num">${esc(r.start_time || '--:--')}</div>
        <div class="r">${esc(Number(r.race_num))}R</div></div>
      <div class="race-name">
        <div class="n">${esc(raceTitleOf(r))}${cls}</div>
        <div class="cond">${esc(raceCondOf(r))}</div>
        <div class="meta">${raceChip(r)}</div>
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
const sel = { step1: new Set(), step2: new Map() };  // step2: metric → {matches,lookbacks}

async function loadFeatures() {
  try {
    state.features = await getJSON('/api/features');
  } catch (err) {
    staleChip('#racesWarn', '選べる項目');
    return;
  }
  const f = state.features;
  (f.glossary || []).forEach((g) => { state.glossary[g.key] = g; });
  // 順位規則は集計結果に依存しない事実なので、選択肢と同じ経路で受け取り
  // ここで入れる。board が空でも「—」のままにならない。
  if (f.ranking_rule) $('#boardRule').textContent = f.ranking_rule;

  // 「まよったら」— 初心者が空白画面で止まらないための入口
  const sp = f.starter_preset;
  $('#starterBox').innerHTML = sp ? `<div class="starter">
      <div class="s-title">${esc(sp.label)}</div>
      <div class="s-desc">${esc(sp.desc)}</div>
      <button class="s-btn" id="starterBtn">この3項目で始める</button>
    </div>` : '';
  if (sp) $('#starterBtn').addEventListener('click', () => applyStarter(sp));

  // STEP1 はグループ見出しで分節する (28項目のフラット羅列をやめる)
  const byGroup = new Map();
  f.step1.forEach((s) => {
    if (!byGroup.has(s.group)) byGroup.set(s.group, []);
    byGroup.get(s.group).push(s);
  });
  $('#step1groups').innerHTML = (f.step1_groups || [])
    .filter((g) => byGroup.has(g.key))
    .map((g) => `<div class="g-block">
      <div class="g-head">${esc(g.label)}</div>
      <div class="g-desc">${esc(g.desc)}</div>
      <div class="param-chips">${byGroup.get(g.key).map(step1Chip).join('')}</div>
    </div>`).join('');
  $$('#step1groups .pchip').forEach((el) => el.addEventListener('click', (ev) => {
    if (ev.target.closest('.term')) return;      // 説明タップは選択に影響させない
    const k = el.dataset.key;
    if (sel.step1.has(k)) { sel.step1.delete(k); el.classList.remove('on'); }
    else { sel.step1.add(k); el.classList.add('on'); }
    refreshSaveCta();
  }));

  $('#step2list').innerHTML = f.step2_metrics.map((m) => {
    const matches = f.step2_matches.map((x, j) =>
      `<button class="cchip" data-kind="match" data-metric="${esc(m.metric)}" data-idx="${j}">${esc(x.label)}</button>`).join('');
    const lbs = f.step2_lookbacks.map((x, j) =>
      `<button class="cchip" data-kind="lb" data-metric="${esc(m.metric)}" data-idx="${j}">${esc(x.label)}</button>`).join('');
    return `<div class="item" data-metric="${esc(m.metric)}">
      <div class="head">
        <label class="toggle">
          <input type="checkbox" data-metric="${esc(m.metric)}" aria-label="${esc(m.label)}を使う">
          <span class="nm">${esc(m.label)}${thinChip(m)}</span>
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

  // 項目トグルとアコーディオン開閉は独立した操作
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
      // ON 時のデフォルトは「すべて × 直近3走」
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
      if (set.size === 1) return;   // 条件≥1 かつ 期間≥1 を必須
      set.delete(idx);
    } else set.add(idx);
    syncCells(metric);
    refreshSaveCta();
  }));
  bindTerms();
  refreshSaveCta();
}
function step1Chip(s) {
  return `<button class="pchip" data-key="${esc(s.key)}">${esc(s.label)}`
    + `${s.term ? term(s.term, 'ⓘ') : ''}${thinChip(s)}</button>`;
}
/* 低サンプル項目は **選ぶ前に** マークする (事前+事後の二段開示) */
function thinChip(s) {
  if (!s.low_sample) return '';
  return `<span class="chip-thin" data-term="low_sample"
    aria-label="データ少なめの説明">データ少なめ</span>`;
}
function bindTerms() {
  $$('[data-term]').forEach((el) => {
    if (el.dataset.bound) return;
    el.dataset.bound = '1';
    el.addEventListener('click', (ev) => {
      ev.preventDefault(); ev.stopPropagation();
      openTermSheet(el.dataset.term);
    });
  });
}
const idxOf = (arr, pred) => { const i = arr.findIndex(pred); return i < 0 ? 0 : i; };
const isAll = (v) => v == null || (Array.isArray(v) && v.length === 0);
const cssEsc = (s) => String(s).replace(/["\\]/g, '\\$&');

function applyStarter(sp) {
  sel.step1 = new Set(sp.step1 || []);
  $$('#step1groups .pchip').forEach((el) =>
    el.classList.toggle('on', sel.step1.has(el.dataset.key)));
  refreshSaveCta();
  toast('「まよったら」の3項目を選びました。そのまま保存できます。');
}

/* サマリーは選択状態と常に同期する */
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
  const wasVersion = state.config ? state.config.version : null;
  btn.disabled = true;
  $('#saveReason').textContent = '保存しています…';
  try {
    const saved = await postJSON('/api/configs', { config: buildConfig() });
    const isNew = wasVersion == null || saved.id !== (state.config && state.config.id);
    state.config = { id: saved.id, name: saved.name, version: saved.version };
    try { sessionStorage.setItem(CFG_ID_KEY, saved.id); } catch (e) { /* 非対応環境は無視 */ }
    $('#saveReason').textContent = `${sel.step1.size + sel.step2.size}項目を選択中`;
    btn.disabled = false;
    // 版が上がったのか新規なのかを必ず区別して知らせる
    toast(isNew && saved.version === 1
      ? `保存しました(${esc(saved.name)} · 新規)`
      : `保存しました(${esc(saved.name)} · v${saved.version})`);
    // 遷移せずに成績カードを見せる (描画済みなのに見えていなかった)
    await loadBacktest();
    const card = $('#btResult');
    if (card && !card.classList.contains('hidden')) {
      card.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
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
      <div class="b">該当期間にレースがありません</div></div></div>
      <button class="cta" data-go="races">レースを選んで予想する</button>`;
    bindGo();
    return;
  }
  box.classList.remove('hidden');
  box.innerHTML = `<div class="section-label">${term('backtest', 'このマイAIのこれまでの成績')}</div>
    <div class="card"><div class="bt-grid">
      ${btCell('◎が1着だった割合', pct(you.hit_rate_win), `1番人気AI ${pct(base.hit_rate_win)}`)}
      ${btCell('◎が3着以内', pct(you.hit_rate_show), `1番人気AI ${pct(base.hit_rate_show)}`)}
      ${btCell('印の中に勝ち馬', pct(you.hit_rate_in_marks), `1番人気AI ${pct(base.hit_rate_in_marks)}`)}
      ${btCell('対象レース数', `${you.races}`, `${ymd(bt.period[0])}以降`)}
    </div></div>
    ${condBreakdown(bt)}
    <p class="note" style="margin-top:8px">${esc(bt.note || '')}</p>
    <button class="cta" data-go="races">レースを選んで予想する</button>`;
  bindTerms();
  bindGo();
}
const btCell = (k, v, b) => `<div class="bt-cell"><div class="k">${esc(k)}</div>
  <div class="v num">${esc(v)}</div><div class="b">${esc(b)}</div></div>`;
function bindGo() {
  $$('[data-go]').forEach((b) => {
    if (b.dataset.bound) return;
    b.dataset.bound = '1';
    b.addEventListener('click', () => go(b.dataset.go));
  });
}

/* ------------------------------------------------------- 画面3: 予想 */
async function renderPredictScreen() {
  // そのレースに前回使ったAIがあれば戻す (記憶している意味を持たせる)
  const remembered = raceConfigMap()[state.selectedRaceId];
  if (remembered && (!state.config || state.config.id !== remembered)) {
    await applyConfigId(remembered, false);
    return;                       // applyConfigId が描画まで行う
  }
  const noAi = !state.config;
  const noRace = !state.selectedRaceId;
  $('#predEmpty').classList.toggle('hidden', !noAi);
  $('#predNoRace').classList.toggle('hidden', noAi || !noRace);
  $('#predBody').classList.toggle('hidden', noAi || noRace);
  bindGo();
  stopPolling();
  if (!noAi && !noRace) { loadPredict(); startPolling(); }
}

async function loadPredict() {
  let p;
  try {
    p = await postJSON('/api/predict',
      { race_id: state.selectedRaceId, config: buildConfig() });
    clearStale('#predWarn');
  } catch (err) {
    const last = state.lastPredict;
    staleChip('#predWarn', 'オッズと印', isoHM(last && last.odds_as_of));
    return;
  }
  const prev = state.lastPredict;
  state.lastPredict = p;
  // 描画中の例外で画面が無言の空白になるのを防ぐ。実際に起きた
  // (変数の宣言順ミスで renderPredict が throw し、印リストが空のまま無警告)。
  try {
    renderPredict(p, prev);
  } catch (e) {
    $('#predWarn').innerHTML = `<div class="warn-card">
      <div class="m">画面を描画できませんでした</div>
      <div class="h">レースを開き直すか、リロードしてください。</div></div>`;
    $('#markList').innerHTML = '';
    $('#markLegend').innerHTML = '';
    $('#betSlip').innerHTML = '';
  }
}

function renderPredict(p, prev) {
  const conf = p.confidence || {};
  // 分母は **参加者が選んだ項目数**。API の columns は重み0の項目を含まないので、
  // それを分母にすると「使えなかった項目」が見えなくなる。
  const used = p.n_columns_used != null
    ? p.n_columns_used : (p.columns || []).filter((c) => c.decision === 'used').length;
  const total = p.n_columns_selected != null ? p.n_columns_selected : used;

  const r0 = state.races.find((x) => x.race_id === p.race_id) || {};
  const cond = raceCondOf({ ...r0, n_horses: (p.marks || []).length || r0.n_horses });
  $('#raceHead').innerHTML = `
    <div class="top">
      <span class="place">${esc(p.race_num || '')}R</span>
      <h2>${esc(raceTitleOf({ ...r0, race_num: p.race_num }))}</h2>
      <span class="off num">発走 ${esc(p.start_time || '--:--')}</span>
    </div>
    <div class="rcond">${esc(cond)}</div>
    <div class="bottom">
      <button class="badge-conf" id="confBadge">${esc(conf.label || '—')}<span class="ci">ⓘ</span></button>
      <span class="head-chip" data-term="coverage">分析に使えた項目 ${used}/${total}</span>
      ${p.weight_announced ? '' : '<span class="head-chip">暫定印(馬体重の発表前)</span>'}
      <button class="ai-pick" id="aiPick">予想: <b>${esc(state.config ? state.config.name : '')}</b> ▾</button>
    </div>
    ${condRecordLine(p)}`;
  $('#confBadge').addEventListener('click', () => openTermSheet('confidence'));
  $('#aiPick').addEventListener('click', openAiSheet);
  setMiniHead(p);

  // warnings は印リストの上に出す
  const warns = p.warnings || [];
  $('#predWarn').innerHTML = warns.map((w) => `<div class="warn-card">
      <div class="m">${esc(w.message)}</div>
      ${w.hint ? `<div class="h">${esc(w.hint)}</div>` : ''}
      ${warnColumns(w)}</div>`).join('');

  // 発走済みのレースでは印を出さず結果を出す (印は発走前のもの)
  if (p.finished) {
    $('#markLegend').innerHTML = '';
    $('#betSlip').innerHTML = '';
    const rows = (p.result || []).map((r) =>
      `<div class="res-row"><span class="o">${esc(r.order)}着</span>
        <span class="n">${esc(r.horse_num)} ${esc(r.horse_name || '')}</span></div>`).join('');
    $('#markList').innerHTML = `<div class="finished">
      <div class="f-title">このレースは終了しています</div>
      ${rows ? `<div class="f-body">${rows}</div>`
             : '<div class="f-note">結果はまだ取り込まれていません。</div>'}
      <div class="f-note">印は発走前のレースにだけ表示します。</div></div>`;
    bindTerms();
    return;
  }

  // 印が意味を持たない警告が1つでもあれば印を出さない。ここは **列挙で塞ぐ**
  // (新しい警告コードが増えたときに黙って印を出してしまわないよう、
  //  「印を出しても良い警告」の側を列挙する)
  const HARMLESS = ['low_sample_columns', 'excluded_columns_dropped',
                    'columns_skipped_in_race'];
  const blocked = warns.some((w) => !HARMLESS.includes(w.code));
  if (blocked) {
    $('#markLegend').innerHTML = ''; $('#markList').innerHTML = '';
    $('#betSlip').innerHTML = '';
    return;
  }

  // 印の凡例 (初見で意味が分かるように印リスト直上に1行)
  const legend = (state.features && state.features.mark_legend || [])
    .filter((m) => m.mark).map((m) => esc(m.term)).join(' ');
  const marks = p.marks || [];
  // 全馬が過去走の少ない馬 (2歳の新馬・未勝利など) なら、行ごとに繰り返さず
  // レース単位で1回だけ知らせる。全行に同じ警告が並ぶと情報にならない。
  const allThin = marks.length > 0
    && marks.filter((m) => m.few_past_runs).length === marks.length;

  $('#markLegend').innerHTML = `<button class="legend-row" id="legendRow">
    <span class="l-marks">${legend}</span><span class="l-more">意味をみる</span></button>`
    + (allThin ? `<div class="thin-race">出走全馬の参照できた過去走が`
        + `${svcMinPastRuns()}走未満です。このレースは評価の確かさが低めです</div>` : '');
  $('#legendRow').addEventListener('click', openMarkSheet);

  const maxAbs = Math.max(...marks.map((m) => Math.abs(m.score || 0)), 1e-9);
  const prevMark = {};
  if (prev) (prev.marks || []).forEach((m) => { prevMark[m.horse_num] = m.mark; });

  $('#markList').innerHTML = marks.map((m) => {
    const isHon = m.mark === '◎';
    const upset = isHon && m.popularity != null && m.popularity !== 1;
    const w = Math.max(2, Math.round((Math.abs(m.score || 0) / maxAbs) * 100));
    const promoted = prev && prevMark[m.horse_num] && prevMark[m.horse_num] !== m.mark
      && rankOf(m.mark) < rankOf(prevMark[m.horse_num]);
    // 行は **button にしない**。中に用語ボタン (data-term) を置くため、
    // button の入れ子になり HTML パーサが内側 button 以降を行の外へ吐き出す。
    // その結果 .why が .horse の子でなくなり `.horse.open .why` が一致せず、
    // 「タップしても根拠が開かない」「人気・オッズが消える」状態になっていた。
    // div[role=button] + keydown でキーボード操作性は維持する。
    return `<div class="horse${isHon ? ' hon' : ''}${promoted ? ' flash' : ''}" data-num="${esc(m.horse_num)}">
      <div class="row" role="button" tabindex="0" aria-expanded="false">
        <span class="mark${isHon ? ' hon' : ''}${m.mark ? '' : ' none'}">${esc(m.mark || '–')}</span>
        ${wakuChip(m)}
        <div class="who">
          <div class="name">${esc(m.horse_name || '')}
            ${upset ? '<span class="badge-upset">人気とは別の根拠</span>' : ''}</div>
          <div class="entry">${entryLine(m, p)}</div>
          <div class="sub">${popLabel(m)}${oddsLabel(m, p)}${coverChip(m)}</div>
          ${(m.few_past_runs && !allThin) ? `<div class="thin-horse">参照できた過去走が`
            + `${Number(m.n_past_runs)}走のみ。評価の確かさは低めです</div>` : ''}
        </div>
        <div class="scorebar"><div class="bar"><i style="width:${w}%"></i></div></div>
      </div>
      ${whyBlock(m)}
    </div>`;
  }).join('');

  $('#betSlip').innerHTML = betSlipBlock(p);
  bindBetSlip(p);

  $$('#markList .row').forEach((row) => {
    const toggle = () => {
      const open = row.parentElement.classList.toggle('open');
      row.setAttribute('aria-expanded', String(open));
    };
    row.addEventListener('click', (ev) => {
      if (ev.target.closest('[data-term]')) return;
      toggle();
    });
    row.addEventListener('keydown', (ev) => {
      if (ev.key !== 'Enter' && ev.key !== ' ') return;
      if (ev.target.closest('[data-term]')) return;
      ev.preventDefault();        // Space でのスクロールを止める
      toggle();
    });
  });
  bindTerms();
}

/* 発走時刻を常に視界に置く縮小ヘッダ */
function setMiniHead(p) {
  const el = $('#miniHead');
  const parts = [`${p.race_num || ''}R`, `発走 ${p.start_time || '--:--'}`];
  if (p.confidence && p.confidence.label) parts.push(p.confidence.label);
  el.textContent = parts.join(' · ');
  el.dataset.for = 'predict';
  updateMiniHead();          // 既にスクロール済みの状態で描画された場合にも出す
}
function updateMiniHead() {
  const el = $('#miniHead');
  if (!el) return;
  const onPredict = $('#scr-predict').classList.contains('active')
    && !$('#predBody').classList.contains('hidden');
  el.hidden = !(onPredict && window.scrollY > 90);
}
window.addEventListener('scroll', updateMiniHead, { passive: true });

/* 警告に項目の内訳が付いている場合は名前を出す。
 * 「何件か」ではなく「どの項目か」が分からないと参加者は判断できない。 */
function warnColumns(w) {
  if (!w.columns || !w.columns.length) return '';
  const rows = w.columns.map((c) => {
    const note = c.train_races != null ? ` — 過去 ${Number(c.train_races)}レースで学習`
      : (c.reason ? ` — ${c.reason}` : '');
    return `<div>${esc(c.label)}${esc(note)}</div>`;
  }).join('');
  const more = w.n_columns > w.columns.length
    ? `<div>ほか ${w.n_columns - w.columns.length}件</div>` : '';
  return `<div class="warn-cols">${rows}${more}</div>`;
}

const rankOf = (mk) => ['◎', '○', '▲', '△', '×'].indexOf(mk);
/* 枠色は **サーバが返す waku をそのまま使う**。馬番から計算してはいけない —
 * JRA の枠割は頭数依存で、7頭立ては馬番=枠番になる (実測で ceil(馬番/2) は
 * 6/7 件外れた)。waku が無い場合は色を付けない (誤った色より無色)。 */
function wakuChip(m) {
  const n = Number(m.horse_num);
  const w = Number(m.waku);
  const cls = (Number.isInteger(w) && w >= 1 && w <= 8) ? ` w${w}` : ' w-none';
  return `<span class="waku${cls}">${esc(String(n))}</span>`;
}
const popLabel = (m) => (m.popularity == null ? ''
  : `<span>${term('popularity', `${Number(m.popularity)}番人気`)}</span>`);
/* オッズには必ず取得時刻を添える。時刻はサーバの odds_fetched_at のみを使い、
 * 取れないときは時刻を出さず「配信オッズ」と書く (クライアント時計で代用しない)。 */
function oddsLabel(m, p) {
  if (m.odds == null) return '';
  const hm = isoHM(p && p.odds_as_of);
  const when = hm ? `${hm}時点` : '配信オッズ';
  return `<span class="num">${term('win_odds', '単勝')} ${m.odds.toFixed(1)}(${esc(when)})</span>`;
}
/* ヘッダの「分析に使えた項目 9/12」は **レース単位**。
 * こちらは **馬単位** (使えた項目のうちこの馬に値があった数)。 */
function coverChip(m) {
  const c = m.coverage || {};
  if (c.n_used == null || c.n_with_value == null) return '';
  if (c.n_with_value >= c.n_used) return '';
  return `<span class="chip muted">この馬のデータ ${c.n_with_value}/${c.n_used}</span>`;
}

/* 学習サンプルが薄い項目は、寄与の行にその事実を添える。 */
const thinNote = (c) => (c.low_sample
  ? `<small>この項目は過去 ${Number(c.train_races)}レース分の学習です</small>` : '');
/* 項目単位のカバレッジ (この項目に値があった頭数)。馬単位の参照走数とは別の軸。 */
const itemCover = (c) => (c.n_with_value != null && c.n_runners != null
  && c.n_with_value < c.n_runners
  ? `<small>この項目に値があったのは ${c.n_with_value}/${c.n_runners}頭</small>` : '');

function whyBlock(m) {
  const all = m.contributions || [];
  const na = all.filter((c) => !c.available);
  const plus = all.filter((c) => c.available && (c.contribution || 0) > 0);
  const minus = all.filter((c) => c.available && (c.contribution || 0) < 0);
  // 押し上げ・押し下げをそれぞれの見出しの下に **正の割合** で出す。
  // 負のパーセントは初心者に読めないため使わない。
  const total = all.reduce((s, c) => s + Math.abs(c.contribution || 0), 0);
  const rows = (list, cls) => {
    const maxAbs = Math.max(...list.map((c) => Math.abs(c.contribution || 0)), 1e-9);
    return list.map((c) => {
      const a = Math.abs(c.contribution || 0);
      const w = Math.max(2, Math.round((a / maxAbs) * 100));
      const share = total > 0 ? Math.round((a / total) * 100) : null;
      const vt = c.value_text ? `<small>${esc(c.value_text)}</small>` : '';
      return `<div class="lbl">${esc(c.label)}${vt}${thinNote(c)}${itemCover(c)}</div>
        <div class="cbar"><i class="${cls}" style="width:${w}%"></i></div>
        <div class="v ${cls} num">${share == null ? '—' : `${share}%`}</div>`;
    }).join('');
  };
  const naRows = na.map((c) => `<div class="lbl">${esc(c.label)}${thinNote(c)}</div>
      <div class="cbar"></div><div class="v na">データなし</div>`).join('');

  // 初心者にはこの一文が本文。サーバが生成した文をそのまま出す。
  const title = m.mark
    ? `なぜこの馬が ${esc(m.mark)} か`
    : `なぜ無印か — 選んだ項目での評価が ${Number(m.rank)}番目で、印は上位5頭まで`;
  return `<div class="why">
    <div class="why-title">${title}</div>
    ${m.decisive ? `<p class="decisive">${esc(m.decisive)}</p>` : ''}
    <div class="c-head">評価を上げた内訳</div>
    ${plus.length ? `<div class="contrib">${rows(plus, 'plus')}</div>`
      : '<p class="c-none">評価を上げた項目はありません。</p>'}
    <div class="c-head">評価を下げた内訳</div>
    ${minus.length ? `<div class="contrib">${rows(minus, 'minus')}</div>`
      : '<p class="c-none">評価を下げた項目はありません。</p>'}
    ${naRows ? `<div class="c-head">使えなかった項目</div>
      <div class="contrib">${naRows}</div>` : ''}
    <div class="cover-why">数字は、評価を上げた/下げた量の内訳です。
      ${na.length ? '「データなし」の項目は評価に加えていません。その分だけ確かさは下がります。' : ''}</div>
    ${profileLine(m)}
    ${m.n_past_runs != null
      ? `<div class="score-line">この馬の過去 ${m.n_past_runs}走を参照しています</div>` : ''}
  </div>`;
}

/* --------------------------------- 馬体重発表のライブ更新 (ポーリング) */
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

/* ------------------------------------------ 画面4: 本日の成績比較 */
async function loadLeaderboard() {
  let d;
  try {
    d = await postJSON('/api/leaderboard', { applied: raceConfigMap() });
    clearStale('#boardWarn');
  } catch (err) {
    if (err.status === 409) {
      $('#boardList').innerHTML = boardEmpty('notready');
      bindGo();
      return;
    }
    staleChip('#boardWarn', '成績');
    return;
  }
  $('#boardSub').textContent = `◎的中数で並べています · ${d.n_races_finished}レース終了時点`
    + (d.scoped_to_applied ? ' · そのレースに使ったAIで集計' : '');
  if (d.ranking_rule) $('#boardRule').textContent = d.ranking_rule;
  const entries = d.entries || [];
  const mine = entries.filter((e) => !e.is_baseline);
  if (!mine.length) {
    // 空の原因を出し分ける: 結果未確定 か 予想実績なし か
    $('#boardList').innerHTML = boardEmpty(
      d.n_races_finished ? 'nomyai' : 'waiting');
    bindGo();
    return;
  }
  $('#boardRoiNote').textContent = d.roi_note || '';
  loadRoiRanking();
  $('#boardList').innerHTML = `<div class="card board">${entries.map((e) => {
    const top = e.rank === 1;
    const stat = e.is_baseline
      ? `いつも1番人気を◎にするAI · ${e.races}レース`
      : `${e.races}レース · ◎が3着以内 ${pct(e.show_rate)}`
        + ` · 人気を出し抜いた的中 <b>${e.upset_hits}回</b>`;
    return `<div class="brow${top ? ' top' : ''}${e.is_baseline ? ' baseline' : ''}">
      <div class="rank">${e.is_baseline ? '—' : esc(String(e.rank))}</div>
      <div class="who"><div class="aname">${esc(e.name)}${e.is_baseline ? '(基準)' : ''}</div>
        <div class="astat">${stat}</div></div>
      <div class="hits"><div class="n num">${e.win_hits}</div><div class="l">◎的中</div></div>
    </div>
    ${roiRow(e)}`;
  }).join('')}</div>`;
}
function boardEmpty(kind) {
  const m = {
    waiting: ['レースの結果待ちです', '確定しだい集計されます。'],
    nomyai: ['マイAIで予想したレースがまだありません',
             'マイAIをつくってレースを選ぶと、ここに成績が並びます。'],
    notready: ['まだ集計できていません', '当日のデータ作成が終わると表示されます。'],
  }[kind] || ['—', ''];
  const cta = kind === 'nomyai'
    ? '<button class="cta" data-go="build">マイAIをつくる</button>' : '';
  return `<div class="empty"><div class="t">${esc(m[0])}</div>
    <div class="d">${esc(m[1])}</div>${cta}</div>`;
}

/* ------------------------------------------------------------- 起動 */
function init() {
  $$('nav.tabs button').forEach((b) => b.addEventListener('click', () => go(b.dataset.scr)));
  bindGo();
  $('#saveBtn').addEventListener('click', saveConfig);
  $('#sheetClose').addEventListener('click', () => $('#sheet').classList.remove('show'));
  $('#sheet').addEventListener('click', (e) => {
    if (e.target.id === 'sheet') $('#sheet').classList.remove('show');
  });

  // 検証モードのバナーは checkVersion() がサーバの応答で決める (UIフラグでは決めない)。
  // デモ演出だけは URL パラメータで切り替える (サーバに触らない見せ方なので)。
  if (IS_DEMO) {
    $('#demoStrip').classList.remove('hidden');
    $('#demoBanner').hidden = false;
    $('#demoBtn').addEventListener('click', simulateWeight);
  } else {
    ['#demoStrip', '#demoBanner'].forEach((s) => { const e = $(s); if (e) e.remove(); });
  }

  // URL のレースを先に state に入れる (リロード・共有・戻るで同じレースに戻る)
  const h = parseHash();
  if (h.raceId) state.selectedRaceId = h.raceId;
  history.replaceState({ scr: h.key, raceId: h.raceId }, '', location.hash || '#races');
  loadRaces();
  checkVersion();
  loadFeatures().then(restoreConfig).then(() => {
    if (h.key !== 'races') go(h.key, false);
  });
}

/* サーバのコードが起動時より新しいと、修正が画面に出ない。黙って迷わせない。 */
async function checkVersion() {
  let v;
  try { v = await getJSON('/api/version'); } catch (e) { return; }
  if (!v) return;
  // 検証モードは結果を知った状態で印を見ることになるので、常時知らせる
  const pb = $('#previewBanner');
  if (pb) {
    if (v.preview) { pb.textContent = v.preview_message; pb.hidden = false; }
    else pb.remove();
  }
  if (!v.stale) return;
  const el = $('#racesWarn');
  if (el) el.innerHTML = `<div class="warn-card"><div class="m">${esc(v.message)}</div></div>`;
}

/* リロード後の復元: sessionStorage の id からサーバの設定を取り直して選択状態に戻す。 */
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
  $$('#step1groups .pchip').forEach((el) =>
    el.classList.toggle('on', sel.step1.has(el.dataset.key)));

  (cfg.step2 || []).forEach((cell) => {
    const mi = state.features.step2_matches.findIndex(
      (x) => JSON.stringify(x.value || []) === JSON.stringify(cell.match || []));
    const li = state.features.step2_lookbacks.findIndex((x) => x.value === cell.lookback);
    if (mi < 0 || li < 0) return;
    const s = sel.step2.get(cell.metric) || { matches: new Set(), lookbacks: new Set() };
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

/* ------------------------------------ R3-a: このレース条件での成績1行 */
/* 「このレースに向くAIか」を判断する材料。数値はサーバ集計で、
 * レース数が閾値未満なら数値を出さない (偶然を実力と誤読させない)。 */
function condRecordLine(p) {
  const c = p.condition_record;
  if (!c) return '';
  if (!c.enough) {
    return `<div class="cond-rec muted">${esc(c.label)}でのこのAIの成績: `
      + `データ不足 (${c.races}レース)</div>`;
  }
  const you = pct(c.your_ai.hit_rate_win);
  const base = pct(c.baseline_favorite.hit_rate_win);
  return `<div class="cond-rec">${esc(c.label)}でのこのAIの◎的中率: `
    + `<b>${you}</b> (1番人気AI ${base} · ${c.races}レース)</div>`;
}

/* ------------------------------------ R3-b: 適用AIの切替 */
async function openAiSheet() {
  let list = [];
  try { list = (await getJSON('/api/configs')).configs || []; } catch (e) { list = []; }
  if (!list.length) {
    openSheet('マイAIの切替', '<p>保存済みのマイAIがありません。</p>');
    return;
  }
  const rows = list.map((c) => {
    const on = state.config && c.id === state.config.id;
    return `<button class="ai-row${on ? ' on' : ''}" data-cfg="${esc(c.id)}">
      <span class="an">${esc(c.name)}</span>
      <span class="am">${c.n_items}項目 · v${c.version}</span>
      ${on ? '<span class="ac">適用中</span>' : ''}</button>`;
  }).join('');
  openSheet('このレースに使うマイAI', rows
    + '<p class="sheet-foot">レースごとに使い分けられます。選んだ組み合わせは記憶されます。</p>');
  $$('#sheetBody .ai-row').forEach((b) => b.addEventListener('click', async () => {
    $('#sheet').classList.remove('show');
    await applyConfigId(b.dataset.cfg, true);
  }));
}

/* レースごとの適用AIを覚える (race_id → config_id)。正本はサーバの設定、
 * ここに置くのは「どのレースにどれを使ったか」の対応だけ。 */
const RACE_CFG_KEY = 'maib.race_config';
function raceConfigMap() {
  try { return JSON.parse(sessionStorage.getItem(RACE_CFG_KEY) || '{}'); }
  catch (e) { return {}; }
}
function rememberRaceConfig(raceId, cfgId) {
  if (!raceId || !cfgId) return;
  const m = raceConfigMap();
  m[raceId] = cfgId;
  try { sessionStorage.setItem(RACE_CFG_KEY, JSON.stringify(m)); } catch (e) { /* 無視 */ }
}

async function applyConfigId(cfgId, remember) {
  let got;
  try { got = await getJSON(`/api/configs/${encodeURIComponent(cfgId)}`); }
  catch (e) { toast('マイAIを読み込めませんでした'); return; }
  if (!got || !got.config) return;
  state.config = { id: got.id, name: got.name, version: got.version };
  try { sessionStorage.setItem(CFG_ID_KEY, got.id); } catch (e) { /* 無視 */ }
  applyConfigToForm(got.config, got.name);
  if (remember) rememberRaceConfig(state.selectedRaceId, got.id);
  $('#predEmpty').classList.add('hidden');
  $('#predNoRace').classList.add('hidden');
  $('#predBody').classList.remove('hidden');
  await loadPredict();
  if (remember) toast(`「${esc(got.name)}」で予想しました`);
}

/* ------------------------------------ R5: 出走情報の行 */
/* サーバの値をそのまま出す。馬体重は発表前は null なので「発表待ち」と書く
 * (PIT を迂回して前走の体重を出したりしない)。 */
function entryLine(m, p) {
  const parts = [];
  if (m.jockey) parts.push(esc(m.jockey));
  if (m.burden_weight != null) {
    parts.push(`${term('burden_weight', '斤量')} ${m.burden_weight.toFixed(1)}kg`);
  }
  if (m.horse_weight != null) {
    const ch = m.horse_weight_change;
    const sign = ch == null ? '' : (ch > 0 ? `+${ch}` : `${ch}`);
    parts.push(`${m.horse_weight}kg${sign ? `(${sign})` : ''}`);
  } else if (p && p.weight_announced === false) {
    parts.push('馬体重 発表待ち');
  }
  return parts.join(' · ');
}

/* 行に出しきらない属性は whyカード側に置く (行の情報密度を上げすぎない) */
function profileLine(m) {
  const parts = [];
  if (m.sex_age) parts.push(esc(m.sex_age));
  if (m.trainer) parts.push(`${esc(m.trainer)}厩舎`);
  return parts.length ? `<div class="score-line">${parts.join(' · ')}</div>` : '';
}

/* ------------------------------------ R3-a: 条件別の内訳 */
/* 芝ダート・距離帯ごとの成績。**レース数が閾値未満は数値を出さない** —
 * 少数の当たり外れを「この条件は得意」と誤読させないため。 */
function condBreakdown(bt) {
  const list = (bt.by_condition || []).filter((c) => !c.key.includes(':'));
  if (!list.length) return '';
  const rows = list.map((c) => {
    const right = c.enough
      ? `<b>${pct(c.your_ai.hit_rate_win)}</b> <span class="cb-base">1番人気AI `
        + `${pct(c.baseline_favorite.hit_rate_win)}</span>`
      : `<span class="cb-none">データ不足 (${c.races}レース)</span>`;
    return `<div class="cb-row"><span class="cb-k">${esc(c.label)}</span>
      <span class="cb-v">${right}</span></div>`;
  }).join('');
  return `<div class="section-label">条件別の内訳</div>
    <div class="card cb">${rows}</div>
    <p class="note" style="margin-top:6px">${bt.min_races_for_rate}レース未満の条件は`
    + `数値を出しません (偶然に左右されるため)。</p>`;
}

/* 「過去走が少ない」の閾値はサーバが決める (labels/predict_service と揃える)。
 * features に載っているので UI で数値を持たない。 */
function svcMinPastRuns() {
  return (state.features && state.features.min_past_runs) || 3;
}

/* ---------------------------------------- 回収率 (信頼区間つき) */
/* **点推定だけを見せない。** 1日36レースでは回収率は「◎に30倍が来たか」で
 * ほぼ決まるので、レース数・信頼区間・控除率上限を必ず併記し、
 * 差が誤差の範囲なら順位を確定させない。数値はすべてサーバ集計 (roi.py)。 */
function roiRow(e) {
  const s = e.roi_stats;
  if (!s) return '';
  if (!s.enough) {
    return `<div class="roi-row muted">回収率: 判定できません`
      + `(${s.races}/${s.min_races}レース)</div>`;
  }
  const ci = s.ci ? `幅 ${pct(s.ci[0])}〜${pct(s.ci[1])}` : '';
  const rank = e.roi_rank ? `<span class="roi-rank">回収率 ${e.roi_rank}位</span>` : '';
  const tied = (e.roi_rank && e.roi_rank !== 1 && e.roi_tied_with_leader)
    ? '<span class="roi-tie">首位との差は誤差の範囲</span>' : '';
  // 最大配当1本が半分以上を占めるなら、それは AI の性能の話ではない
  const dom = (s.top_share != null && s.top_share >= 0.5)
    ? `<span class="roi-tie">${pct(s.top_share)}が最大配当1本によるもの</span>` : '';
  return `<div class="roi-row">${rank}
    <b class="num">${pct(s.roi)}</b> <span class="roi-ci">${esc(ci)}</span>
    ${tied}${dom}</div>`;
}

/* ---------------------------------------- 買い目 (公式サイトへ手入力) */
/* QR は生成しない。**スマッピー投票の QR データ形式は非公開**で、JRA 公式の
 * 生成サイトだけが正規の経路。形式を推測すると、読めないか間違った馬券を
 * 実際のお金で登録する危険がある。ここは券種と馬番の一覧まで。金額は扱わない。 */
function betSlipBlock(p) {
  const slip = p.bet_slip || [];
  if (!slip.length) return '';
  const rows = slip.map((t) => `<div class="bs-row">
      <div class="bs-k">${esc(t.label)}<small>${esc(t.desc)}</small></div>
      <div class="bs-v num">${t.combos.map((c) =>
        esc(c.map((x) => Number(x)).join('-'))).join(' / ')}</div>
      <div class="bs-n">${t.n}点</div>
    </div>`).join('');
  return `<div class="section-label">買い目(印の並べ替え)</div>
    <div class="card bs">${rows}</div>
    <div class="bs-actions">
      <button class="bs-copy" id="bsCopy">買い目をコピー</button>
      <a class="bs-link" href="https://qrcode.jra.go.jp/" target="_blank"
         rel="noopener noreferrer">JRA公式QR作成サイトを開く</a>
    </div>
    <p class="note bs-note">これは印を券種ごとに並べ替えたものです。
      金額は扱いません。QRコードはJRA公式サイトでのみ作成できます
      (形式が公開されていないため、このツールでは作りません)。
      ◎の的中率は実測で約20%(1番人気は約33%)、回収率は長期では控除率に収束します。</p>`;
}
function bindBetSlip(p) {
  const btn = $('#bsCopy');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    const text = (p.bet_slip || []).flatMap((t) =>
      t.combos.map((c) => `${t.label} ${c.map((x) => Number(x)).join('-')}`)).join('\n');
    try {
      await navigator.clipboard.writeText(text);
      toast('買い目をコピーしました。JRA公式サイトに貼り付けてください。');
    } catch (e) {
      toast('コピーできませんでした。画面の一覧をご利用ください。');
    }
  });
}

/* ---------------------------------------- 回収率ランキング (表示期間) */
/* 当日36レースでは最小レース数 (50) に届かないので、**表示期間の数千レース**で
 * 集計したものを並べる。回収率は蓄積して初めて意味を持つ数値なので、
 * 1日単位で競わせない。数値・順位・区間はすべてサーバ集計 (roi.py)。 */
async function loadRoiRanking() {
  const box = $('#roiRanking');
  if (!box) return;
  let d;
  try { d = await getJSON('/api/roi_ranking'); } catch (err) {
    box.innerHTML = err.status === 409
      ? `<div class="section-label">回収率ランキング</div>
         <div class="card"><p class="roi-row muted">
         バックテスト用のデータが読み込まれていません
         (起動時に期間を指定すると出ます)。</p></div>`
      : '';
    return;
  }
  // 全員が同順位 (区間が全部重なる) なら「1位」を並べても情報にならないので、
  // 順位の代わりに「同順位」と出して、その事実を見出しで述べる。
  const scored = (d.entries || []).filter((e) => !e.is_baseline && e.roi_rank);
  const allTied = scored.length > 1 && scored.every((e) => e.roi_rank === 1);
  const rows = (d.entries || []).map((e) => {
    const s = e.roi_stats || {};
    const rank = e.is_baseline ? '基準'
      : (!e.roi_rank ? '—' : (allTied ? '同' : `${e.roi_rank}位`));
    const right = s.enough
      ? `<b class="num">${pct(s.roi)}</b>
         <span class="roi-ci">幅 ${pct(s.ci[0])}〜${pct(s.ci[1])}</span>`
      : `<span class="roi-ci">判定できません(${s.races}/${s.min_races})</span>`;
    const tie = (!e.is_baseline && e.roi_rank && e.roi_rank !== 1
                 && e.roi_tied_with_leader)
      ? '<span class="roi-tie">首位との差は誤差の範囲</span>' : '';
    return `<div class="rr-row${e.is_baseline ? ' baseline' : ''}">
      <div class="rr-rank">${esc(rank)}</div>
      <div class="rr-who"><div class="rr-name">${esc(e.name)}</div>
        <div class="rr-sub">${e.races}レース · ◎的中 ${pct(e.hit_rate_win)}${tie}</div></div>
      <div class="rr-v">${right}</div>
    </div>`;
  }).join('');
  box.innerHTML = `<div class="section-label">回収率ランキング</div>
    ${allTied ? `<div class="rr-tied">どのマイAIも回収率の差は誤差の範囲です`
      + `(信頼区間が重なっています)。順位は付けられません。</div>` : ''}
    <div class="card rr">${rows || '<p class="roi-row muted">マイAIがありません</p>'}</div>
    <p class="note" style="margin-top:6px">
      ${esc(ymd((d.period || [])[0] || ''))}以降の全レースに同じ設定を当てはめた集計です。
      ${esc(d.roi_note || '')}</p>`;
}
