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
  selectedRaceDate: null,
  polling: null,         // 馬体重発表の検知タイマー
  races: [],
  todayDate: null,       // サーバが返した開催日（端末時計では決めない）
  todayRaceCount: null,  // null=未確認。0 の日はマイAIの作成・編集を開かない
  listDate: null,        // 一覧で現在表示している日
  features: null,
  contextTrend: null,
  glossary: {},          // term key → {term, desc}
  lastPredict: null,     // 印の変動判定に使う前回レスポンス
  stale: false,          // 直近の取得に失敗しているか
  auth: null,            // 共有モードの利用者 {role,display_name,user_id}
  authExpiryTimer: null, // 招待期限到達時に画面を閉じるタイマー
  screenPolling: null,   // レース一覧・成績画面の更新タイマー
  screenRefreshing: false,
  boardLoading: false,
  aiBoardLoading: false,
  purchaseFilter: 'focus',
  performanceRange: 'today',
  raceDateCatalog: null,
  resultTrack: null,      // 結果一覧で選択中の競馬場（スマホの競馬場タブ）
};

/* 設定の正本はサーバ。ここに置くのは **id だけ** (リロード後の復元用)。
 * 設定本体は毎回 GET /api/configs/{id} で取り直す。 */
const CFG_ID_KEY = 'maib.config_id';

const IS_DEMO = new URLSearchParams(location.search).get('demo') === '1';
const TITLES = {
  races:   ['きょうのレース', '予想できるレースから選べます'],
  build:   ['マイAIをつくる', '重視する項目を選ぶだけ · 2〜3分'],
  predict: ['マイAIの予想', 'タップすると根拠がひらきます'],
  board:   ['成績', '購入・払戻を確認します'],
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
    if (res.status === 401 && body && body.error === 'authentication_required') {
      lockSharedAccess();
      throw err;
    }
    // 起動後にサーバ側コードが変わると、無い経路が 404 になって理由が分からない。
    // 失敗のたびに動作モードを確かめ、古いプロセスなら画面に出す。
    // 項目別振り返りは旧サービス用のフォールバックを持つ。新APIの404を
    // 「アプリ全体が使えない」と扱うと、正常な一覧の上へ再起動警告が重なる。
    if (path !== '/api/version' && !path.startsWith('/api/result-item-review')) maybeWarnStale();
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
  if (key === 'build' && state.todayRaceCount === 0) {
    key = 'races';
    state.listDate = state.todayDate;
    toast('本日の開催レースがないため、マイAIの作成・編集は開催日に利用できます。');
    history.replaceState({ scr: 'races', raceId: state.selectedRaceId }, '', '#races');
    push = false;
  }
  stopScreenPolling();
  $$('.screen').forEach((s) => s.classList.remove('active'));
  $(`#scr-${key}`).classList.add('active');
  document.body.classList.toggle('wide-board', key === 'board');
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
  if (key === 'board') { loadLeaderboard(); startScreenPolling('board'); }
  if (key === 'races') { loadRaces(); startScreenPolling('races'); }
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
    state.selectedRaceDate = /^\d{8}/.test(raceId) ? raceId.slice(0, 8) : null;
    state.lastPredict = null;
  }
  go(key, false);
});

/* --------------------------------------------------- 画面1: レース一覧 */
function raceChip(r) {
  if (r.finished) return '<span class="chip">終了</span>';
  if (r.started) return '<span class="chip wait">結果取込待ち</span>';
  // 「暫定印」= 馬体重の発表前という意味。項目のカバレッジ不足は別のことなので
  // 同じ言葉を使わない (終了レースに「暫定印」が付いて見えたのはこの混同が原因)。
  if (!r.weight_announced) return '<span class="chip wait">馬体重の発表待ち</span>';
  if (r.live_source_fresh === false) return '<span class="chip wait">速報情報を更新中</span>';
  if (!r.ready) return '<span class="chip muted">分析できる項目がありません</span>';
  // 使えない項目のチップは行に出さない。全レースに同じ内容が並ぶと情報にならない
  // ので、共通なら一覧上部に1回だけ出す (gateNotice)。
  return '<span class="chip ok">予想できます</span>';
}

/* 使えない項目が全レース共通なら、一覧上部に1回だけ具体名で知らせる。
 * レースごとに違う場合は行数が多いので件数だけを出す。 */
function gateNotice(races) {
  const upcoming = races.filter((r) => !r.finished && !r.started && (r.n_gate_missing || 0) > 0);
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

async function loadRaces(force = false, requestedDate = state.listDate) {
  let data;
  if (!state.raceDateCatalog || force) {
    try { state.raceDateCatalog = await getJSON('/api/race-dates'); }
    catch (err) { /* 一覧本体は開催日カタログが取れなくても表示する */ }
  }
  if (!state.todayDate && state.raceDateCatalog && state.raceDateCatalog.today) {
    state.todayDate = state.raceDateCatalog.today;
  }
  try {
    const params = new URLSearchParams();
    if (requestedDate) params.set('date', requestedDate);
    if (force && (!requestedDate || requestedDate === state.todayDate)) params.set('refresh', '1');
    data = await getJSON(`/api/races/today${params.size ? `?${params}` : ''}`);
    clearStale('#racesWarn');
  } catch (err) {
    if (err.status === 409) {
      $('#raceList').innerHTML = `<div class="empty"><div class="t">この日のデータを取得できませんでした</div>
        <div class="d">「開催なし」ではなく、データがまだ準備できていない状態です。</div>
        <button class="cta" id="retryRaceLoad">もう一度確認する</button></div>`;
      const retry = $('#retryRaceLoad');
      if (retry) retry.addEventListener('click', () => loadRaces(true, requestedDate));
      return;
    }
    staleChip('#racesWarn', 'レース一覧');
    return;
  }
  if (!state.todayDate || !requestedDate) state.todayDate = data.date;
  state.listDate = data.date;
  state.races = data.races || [];
  if (data.date === state.todayDate) {
    state.todayRaceCount = state.races.length;
    syncBuildAvailability();
    // #build を直接開いた場合も、開催有無の取得が終わった時点で閉じる。
    if (!state.todayRaceCount && parseHash().key === 'build') go('build', false);
  }
  $('#hdrDate').textContent = formatDate(data.date);
  const historical = Boolean(state.todayDate && data.date !== state.todayDate);
  const allFinished = Boolean(state.races.length && state.races.every((race) => race.finished));
  $('#raceLiveNote').hidden = historical || !state.races.length;
  $('#screenTitle').textContent = historical ? '過去の結果'
    : (allFinished ? '本日の結果' : TITLES.races[0]);
  $('#screenSub').textContent = historical ? `${formatDate(data.date)}の確定結果`
    : (allFinished ? `全${state.races.length}レース終了` : TITLES.races[1]);

  const dateNav = raceDateNav(data.date);

  if (!state.races.length) {
    const catalog = state.raceDateCatalog || {};
    const next = catalog.next;
    const previous = catalog.previous;
    const detail = historical
      ? 'この日は開催レースがありません。別の開催日を選んでください。'
      : (next ? `次の開催は ${formatDate(next.date)} です。`
        : '次の開催データはまだ準備されていません。過去の結果は開催日から確認できます。');
    const previousButton = !historical && previous
      ? `<button class="cta" id="showPreviousRaceDay">前回開催（${formatDate(previous.date)}）の結果を見る</button>` : '';
    $('#raceList').innerHTML = dateNav + `<div class="empty"><div class="t">${historical
      ? 'この日は開催がありません' : '本日は開催がありません'}</div><div class="d">${esc(detail)}</div>${previousButton}</div>`;
    bindRaceDateNav();
    return;
  }
  // 発走時刻が近い順に最大3レースだけを先頭へ。全36レースを縦に並べず、
  // それ以外は1つの選択欄から直接開けるようにする。
  const now = new Date();
  const nowMinutes = now.getHours() * 60 + now.getMinutes();
  const proximity = (r) => {
    const m = /^(\d{2}):(\d{2})$/.exec(r.start_time || '');
    if (!m) return 9999;
    const start = Number(m[1]) * 60 + Number(m[2]);
    return start >= nowMinutes - 5 ? start - nowMinutes : 1440 + start - nowMinutes;
  };
  const upcoming = state.races.filter((r) => !r.finished && !r.started).slice()
    .sort((a, b) => proximity(a) - proximity(b));
  const featured = historical ? [] : upcoming.slice(0, 3);
  const rest = historical ? [] : upcoming.slice(3);
  const pending = historical ? [] : state.races.filter((r) => !r.finished && r.started).slice()
    .sort((a, b) => (b.start_time || '').localeCompare(a.start_time || ''));
  const finished = state.races.filter((r) => r.finished).slice()
    .sort((a, b) => historical
      ? String(a.track_label || '').localeCompare(String(b.track_label || ''), 'ja')
        || Number(a.race_num || 0) - Number(b.race_num || 0)
      : (b.start_time || '').localeCompare(a.start_time || ''));
  const optionRaces = historical ? finished : [...rest, ...pending, ...finished];
  const picker = raceButtonPicker(optionRaces, historical, {
    rest: rest.length, pending: pending.length, finished: finished.length,
  });
  const near = historical ? '' : `<div class="race-near"><div class="section-label">発走時刻が近いレース</div>
       <div class="card">${featured.length ? featured.map(raceRow).join('')
         : '<div class="empty compact"><div class="t">本日のレースは終了しました</div></div>'}</div></div>`;
  const itemReview = finished.length ? `<section class="result-review-launch card">
      <div><b>${historical ? 'この日' : '本日'}の項目別振り返り</b>
        <p>${finished.length}/${state.races.length}レースの結果をまとめて確認</p></div>
      <button id="loadResultItemReview">一覧を表示</button>
      <div id="resultItemReview"></div>
    </section>` : '';
  $('#raceList').innerHTML = dateNav + (historical ? '' : gateNotice(state.races))
    + near
    + picker
    + itemReview;
  bindRaceDateNav();
  $$('#raceList .race-item').forEach((el) => {
    el.addEventListener('click', () => selectRace(el.dataset.race));
  });
  bindRaceButtonPicker();
  const reviewButton = $('#loadResultItemReview');
  if (reviewButton) reviewButton.addEventListener('click', () => loadResultItemReview(data.date));
}

/* 36レースのselectはスマホで現在地を見失いやすい。競馬場を先に選び、
 * レース番号を1タップで開く二段階の選択にする。 */
function raceButtonPicker(races, historical, counts) {
  if (!races.length) return '';
  const grouped = new Map();
  races.forEach((race) => {
    const track = race.track_label || '競馬場不明';
    if (!grouped.has(track)) grouped.set(track, []);
    grouped.get(track).push(race);
  });
  const tracks = Array.from(grouped.keys());
  if (!tracks.includes(state.resultTrack)) state.resultTrack = tracks[0];
  const tabs = tracks.map((track) => {
    const on = track === state.resultTrack;
    return `<button type="button" class="result-track${on ? ' on' : ''}"
      data-result-track="${esc(track)}" role="tab" aria-selected="${on}"
      aria-controls="result-races-${tracks.indexOf(track)}">${esc(track)}<small>${grouped.get(track).length}R</small></button>`;
  }).join('');
  const groups = tracks.map((track, index) => {
    const on = track === state.resultTrack;
    const buttons = grouped.get(track).slice()
      .sort((a, b) => Number(a.race_num || 0) - Number(b.race_num || 0))
      .map((race) => {
        const waiting = race.started && !race.finished;
        const status = race.finished ? '確定' : (waiting ? '結果待ち' : '開催前');
        return `<button type="button" class="result-race-button${race.finished ? ' finished' : ''}"
          data-race="${esc(race.race_id)}"${waiting ? ' disabled' : ''}
          aria-label="${esc(`${track} ${Number(race.race_num)}R ${raceTitleOf(race)} ${status}`)}">
          <span><b>${Number(race.race_num)}R</b><small class="num">${esc(race.start_time || '--:--')}</small></span>
          <span class="result-race-title">${esc(raceTitleOf(race))}</span><em>${status}</em></button>`;
      }).join('');
    return `<div class="result-race-grid" id="result-races-${index}" role="tabpanel"
      data-result-group="${esc(track)}"${on ? '' : ' hidden'}>${buttons}</div>`;
  }).join('');
  const summary = historical ? `${counts.finished}レースの確定結果`
    : `${counts.rest}レース開催前・${counts.pending}レース結果待ち・${counts.finished}レース終了`;
  return `<section class="race-picker card" aria-labelledby="resultRacePickerTitle">
    <div class="race-picker-head"><h3 id="resultRacePickerTitle">${historical ? '結果を見るレース' : 'そのほかのレース'}</h3>
      <span>競馬場を選択</span></div>
    <div class="result-track-tabs" role="tablist" aria-label="競馬場を選択">${tabs}</div>
    ${groups}<p>${summary}</p></section>`;
}

function bindRaceButtonPicker() {
  $$('#raceList [data-result-track]').forEach((button) => {
    button.addEventListener('click', () => {
      state.resultTrack = button.dataset.resultTrack;
      $$('#raceList [data-result-track]').forEach((item) => {
        const on = item.dataset.resultTrack === state.resultTrack;
        item.classList.toggle('on', on);
        item.setAttribute('aria-selected', String(on));
      });
      $$('#raceList [data-result-group]').forEach((group) => {
        group.hidden = group.dataset.resultGroup !== state.resultTrack;
      });
    });
  });
  $$('#raceList .result-race-button[data-race]').forEach((button) => {
    button.addEventListener('click', () => selectRace(button.dataset.race));
  });
}

async function loadResultItemReview(date) {
  const button = $('#loadResultItemReview');
  const box = $('#resultItemReview');
  if (!box) return;
  if (button) { button.disabled = true; button.textContent = '集計中…'; }
  box.innerHTML = '<div class="review-loading">36レースを項目別に再採点しています…</div>';
  try {
    let review;
    try {
      review = await getJSON(`/api/result-item-review?date=${encodeURIComponent(date)}`);
    } catch (err) {
      // Pythonサービスをすぐ再起動できない遠隔運用でも使えるよう、旧サービスが
      // 新集約APIをまだ持たない場合だけ、既存の結果APIを4並列で束ねる。
      if (err.status !== 404) throw err;
      review = await buildResultItemReviewFromPredictions(date);
    }
    box.innerHTML = resultItemReviewHtml(review);
    if (button) button.hidden = true;
    bindResultReviewFilters(box);
  } catch (err) {
    box.innerHTML = '<div class="review-error">項目別振り返りを取得できませんでした。もう一度お試しください。</div>';
    if (button) { button.disabled = false; button.textContent = 'もう一度表示'; }
  }
}

async function buildResultItemReviewFromPredictions(date) {
  const races = (state.races || []).slice();
  const output = new Array(races.length);
  let cursor = 0;
  async function worker() {
    while (cursor < races.length) {
      const index = cursor++;
      const race = races[index];
      const row = {
        race_id: race.race_id, race_num: race.race_num, start_time: race.start_time,
        race_title: race.race_title || race.race_class || '',
        track: race.track, track_label: race.track_label,
        surface: race.surface, surface_label: race.surface_label,
        distance: race.distance, condition_label: race.condition_label,
        finished: Boolean(race.finished), top3: [],
      };
      if (race.finished) {
        const prediction = await postJSON('/api/predict', {
          race_id: race.race_id, date, result_only: true,
        });
        const analyses = ((prediction.result_pickup_analysis || {}).horses || {});
        row.top3 = (prediction.result || []).map((placed) => {
          const analysis = analyses[String(placed.horse_num)] || {};
          return {...placed, candidate: (analysis.candidates || [])[0] || null,
            candidate_status: analysis.status || 'none'};
        });
        row.review_ai = await buildReviewAiExample(race, prediction, date);
      }
      output[index] = row;
    }
  }
  const workers = Array.from({length: Math.min(4, Math.max(1, races.length))}, () => worker());
  await Promise.all(workers);
  return {date, race_count: output.length,
    finished_count: output.filter((race) => race.finished).length,
    basis: 'pre_race_features', mode: 'retrospective_ai', races: output};
}

async function buildReviewAiExample(race, prediction, date) {
  const analyses = ((prediction.result_pickup_analysis || {}).horses || {});
  const placed = prediction.result || [];
  const chosen = [];
  const keys = new Set();
  for (let candidateIndex = 0; candidateIndex < 3 && chosen.length < 3; candidateIndex += 1) {
    placed.forEach((horse) => {
      const candidate = (((analyses[String(horse.horse_num)] || {}).candidates || [])[candidateIndex]);
      const key = candidate && (candidate.key || String(candidate.id || '').split('|')[0]);
      if (candidate && key && !keys.has(key) && chosen.length < 3) {
        chosen.push(candidate); keys.add(key);
      }
    });
  }
  if (!chosen.length) return null;
  const config = reviewConfigFromCandidates(chosen,
    `${race.track_label || ''}${Number(race.race_num)}R 振り返りAI`);
  const scored = await postJSON('/api/predict', {
    race_id: race.race_id, date, result_only: true, config,
  });
  const byNum = new Map((scored.marks || []).map((mark) => [String(mark.horse_num), mark]));
  const evaluated = placed.map((horse) => {
    const mark = byNum.get(String(horse.horse_num));
    const rank = mark ? Number(mark.rank) : null;
    return {...horse, ai_rank: rank, ai_mark: mark ? mark.mark : '',
      in_marks: Boolean(rank && rank <= 5)};
  });
  return {mode: 'retrospective_ai', config,
    items: chosen.map((candidate) => ({...candidate,
      section: String(candidate.id || '').includes('|lb=') ? '詳細設定' : '基本項目'})),
    placed: evaluated, placed_in_marks: evaluated.filter((horse) => horse.in_marks).length,
    n_placed: evaluated.length};
}

function reviewConfigFromCandidates(candidates, name) {
  const step1 = [];
  const step2 = [];
  candidates.forEach((candidate) => {
    const id = String(candidate.id || '');
    const match = /^(.+)\|lb=([^|]+)\|m=(.*)$/.exec(id);
    if (!match) { step1.push(candidate.key || id); return; }
    step2.push({metric: candidate.key || match[1],
      lookback: match[2] === 'None' ? null : Number(match[2]),
      match: match[3] ? match[3].split(',').filter(Boolean) : []});
  });
  return {name, step1, step2};
}

function reviewAiCompact(ai) {
  if (!ai || !(ai.items || []).length) return '<span class="review-none">選択例を作成できませんでした</span>';
  return `<span class="review-ai-compact">選択例: ${(ai.items || []).map((item) => esc(item.label)).join('＋')}</span>`;
}

function reviewAiDetails(ai) {
  if (!ai || !(ai.items || []).length) return '<div class="review-ai-box review-none">選択例を作成できませんでした</div>';
  return `<div class="review-ai-box"><b>マイAIで選ぶ項目</b>
    <ol>${ai.items.map((item) => `<li><span>${esc(item.section || '')}</span>${esc(item.label)}</li>`).join('')}</ol>
    <p>この組み合わせで上位3頭のうち <strong>${Number(ai.placed_in_marks)}/${Number(ai.n_placed)}</strong>頭が印圏内</p></div>`;
}

function resultItemReviewHtml(data) {
  const races = data.races || [];
  const tracks = [...new Set(races.map((r) => r.track_label || '競馬場不明'))];
  const filters = tracks.map((track) => `<button data-review-track="${esc(track)}">${esc(track)}</button>`).join('');
  const groups = tracks.map((track) => {
    const rows = races.filter((r) => (r.track_label || '競馬場不明') === track);
    return `<section class="review-track" data-review-group="${esc(track)}">
      <div class="review-track-title">${esc(track)} <span>${rows.filter((r) => r.finished).length}/${rows.length}R確定</span></div>
      <div class="review-races">${rows.map((r) => {
        const cond = [r.surface_label, r.distance ? `${Number(r.distance)}m` : '', r.condition_label]
          .filter(Boolean).join('・');
        const title = r.race_title || '';
        if (!r.finished) return `<div class="review-race waiting"><div class="review-head">
          <b>${Number(r.race_num)}R</b><span>${esc(title)}</span><em>${esc(cond)}</em></div><p>結果取込待ち</p></div>`;
        const top = r.top3 || [];
        const winner = top[0] || {};
        const ai = r.review_ai || null;
        const aiPlaced = new Map(((ai && ai.placed) || []).map((horse) => [String(horse.horse_num), horse]));
        const winnerEval = aiPlaced.get(String(winner.horse_num)) || {};
        return `<details class="review-race">
          <summary><div class="review-head"><b>${Number(r.race_num)}R</b><span>${esc(title)}</span><em>${esc(cond)}</em></div>
            <div class="review-winner"><strong>1着 ${esc(winner.horse_num || '')} ${esc(winner.horse_name || '')}</strong>
              ${reviewAiCompact(ai)}<small>${winnerEval.ai_rank
                ? `この組み合わせで評価${Number(winnerEval.ai_rank)}位${winnerEval.in_marks ? '・印圏内' : '・印圏外'}` : ''}</small></div></summary>
          <div class="review-places">${reviewAiDetails(ai)}${top.map((horse) => {
            const evaluated = aiPlaced.get(String(horse.horse_num)) || {};
            return `<div class="review-place">
            <span class="review-order">${Number(horse.order)}着</span>
            <span class="review-horse"><b>${esc(horse.horse_num)} ${esc(horse.horse_name || '')}</b>
              <small>${evaluated.ai_rank ? `この組み合わせで評価${Number(evaluated.ai_rank)}位${evaluated.ai_mark ? `・${esc(evaluated.ai_mark)}` : '・無印'}` : '評価できませんでした'}</small></span></div>`;
          }).join('')}</div>
        </details>`;
      }).join('')}</div></section>`;
  }).join('');
  return `<div class="review-summary">
      <b>${esc(formatDate(data.date))}・全${Number(data.race_count)}レース</b>
      <span>${Number(data.finished_count)}レース確定</span>
    </div>
    <p class="review-note">複数項目を組み合わせた振り返り用マイAIの選択例と、その組み合わせでの評価順位です。結果から逆算した説明であり、同じ選択を将来のレースへ推奨するものではありません。</p>
    <div class="review-filters"><button class="on" data-review-track="">すべて</button>${filters}</div>
    ${groups}`;
}

function bindResultReviewFilters(box) {
  box.querySelectorAll('[data-review-track]').forEach((button) => {
    button.addEventListener('click', () => {
      const track = button.dataset.reviewTrack || '';
      box.querySelectorAll('[data-review-track]').forEach((b) => b.classList.toggle('on', b === button));
      box.querySelectorAll('[data-review-group]').forEach((group) => {
        group.hidden = Boolean(track && group.dataset.reviewGroup !== track);
      });
    });
  });
}
function raceDateNav(selectedDate) {
  const catalog = state.raceDateCatalog || {};
  const previous = catalog.previous;
  const dates = (catalog.dates || []).slice().reverse();
  const options = dates.map((item) => `<option value="${esc(item.date)}"${item.date === selectedDate ? ' selected' : ''}>
    ${esc(formatDate(item.date))}（${Number(item.race_count || 0)}レース）</option>`).join('');
  return `<div class="race-date-nav" aria-label="表示する開催日">
    <button id="showToday"${selectedDate === state.todayDate ? ' class="on"' : ''}>本日のレース</button>
    ${previous ? `<button id="showPrevious"${selectedDate === previous.date ? ' class="on"' : ''}>前回開催 ${esc(formatDate(previous.date))}</button>` : ''}
  </div>${options ? `<label class="race-date-select">過去の開催日
    <select id="raceDateSelect"><option value="">開催日を選ぶ</option>${options}</select></label>` : ''}`;
}
function bindRaceDateNav() {
  const todayButton = $('#showToday');
  const previous = state.raceDateCatalog && state.raceDateCatalog.previous;
  const select = $('#raceDateSelect');
  if (todayButton) todayButton.addEventListener('click', () => loadRaces(false, state.todayDate));
  if (previous) {
    ['#showPrevious', '#showPreviousRaceDay'].forEach((selector) => {
      const button = $(selector);
      if (button) button.addEventListener('click', () => loadRaces(false, previous.date));
    });
  }
  if (select) select.addEventListener('change', () => {
    if (select.value) loadRaces(false, select.value);
  });
}
function syncBuildAvailability() {
  const closed = state.todayRaceCount === 0;
  const tab = $('nav.tabs button[data-scr="build"]');
  if (!tab) return;
  tab.disabled = closed;
  tab.setAttribute('aria-disabled', String(closed));
  tab.title = closed ? '本日の開催レースはありません' : '';
}
function shiftYmd(s, days) {
  const m = /^(\d{4})(\d{2})(\d{2})$/.exec(String(s || ''));
  if (!m) return '';
  const dt = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]) + days));
  return `${dt.getUTCFullYear()}${String(dt.getUTCMonth() + 1).padStart(2, '0')}`
    + `${String(dt.getUTCDate()).padStart(2, '0')}`;
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
  const going = (r.condition_announced === false || r.condition_label === '不明')
    ? '馬場発表待ち' : r.condition_label;
  const scratched = Number(r.n_scratched || 0) > 0 ? `取消・除外 ${Number(r.n_scratched)}頭` : '';
  return [surface + dist, r.n_horses ? `${r.n_horses}頭` : '', going, scratched]
    .filter(Boolean).join(' · ');
}
function raceChangeLine(r) {
  const parts = [];
  if (r.start_time_changed && r.original_start_time) {
    parts.push(`発走変更 ${r.original_start_time}→${r.start_time || '--:--'}`);
  }
  const c = r.course_change || {};
  if (r.course_changed) {
    const oldCourse = `${c.old_surface_label || ''}${c.old_distance ? `${c.old_distance}m` : ''}`;
    const newCourse = `${c.new_surface_label || ''}${c.new_distance ? `${c.new_distance}m` : ''}`;
    parts.push(`コース変更 ${oldCourse || '変更前不明'}→${newCourse || '変更後不明'}`);
  }
  return parts.length ? `<div class="race-change">${parts.map(esc).join(' · ')}</div>` : '';
}
function raceRow(r) {
  // 日本語ラベルはサーバ (labels.py) の値をそのまま出す。UI に対応表を持たない。
  const sel = r.race_id === state.selectedRaceId ? ' on' : '';
  const cls = (r.race_title && r.race_class) ? `<span class="rcls">${esc(r.race_class)}</span>` : '';
  return `<button class="race-item${sel}" data-race="${esc(r.race_id)}">
      <div class="race-time"><div class="t num">${esc(r.start_time || '--:--')}</div>
        <div class="r">${esc(Number(r.race_num))}R</div></div>
      <div class="race-name">
        <div class="n"><span class="track-patch">${esc(r.track_label || '競馬場不明')}</span>${esc(raceTitleOf(r))}${cls}</div>
        <div class="cond">${esc(raceCondOf(r))}</div>
        ${raceChangeLine(r)}
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
  const selected = state.races.find((race) => race.race_id === raceId);
  if (selected && selected.started && !selected.finished) {
    toast('発走済みです。確定結果の取込後に開けるようになります。');
    return;
  }
  state.selectedRaceId = raceId;
  state.selectedRaceDate = (selected && selected.date) || state.listDate
    || (/^\d{8}/.test(raceId) ? raceId.slice(0, 8) : null);
  state.lastPredict = null;
  go('predict');
}

/* ------------------------------------------------ 画面2: マイAIをつくる */
const sel = { step1: new Set(), step2: new Map() };
/* 買い目。**レースごとに持つ** — オッズ更新で予想を取り直しても参加者が組んだ
 * 買い目を消さないため (30秒ごとに再取得している)。
 *   entries … 追加済みの買い目 [{type, mode, groups}]
 *   draft   … いま組んでいる1件 (券種・買い方・段ごとの選択)
 * 組み合わせと点数は **サーバが作る** (slip/total/text)。 */
const bet = { raceId: null, entries: [], suggested: [], dirty: false,
              draft: { type: null, mode: null, groups: [] },
              slip: [], skipped: [], total: 0, totalYen: 0, text: '',
              budgetYen: null, oddsNote: '',
              qr: null, qrKey: null, qrError: '', qrPending: false,
              recordOnly: false, recordPending: false,
              savedPurchaseId: null, savedKey: null,
              openSlipDetails: new Set() };  // step2: metric → {matches,lookbacks}

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

  // レース区分ごとの検証済み入口。新馬に通常レース用の戦歴項目を勧めない。
  const starters = (f.starter_presets && f.starter_presets.length)
    ? f.starter_presets : (f.starter_preset ? [f.starter_preset] : []);
  $('#starterBox').innerHTML = starters.length ? `<div class="starter-list">
    ${starters.map((sp, i) => `<div class="starter">
      <div class="s-title">${esc(sp.label)}</div>
      <div class="s-desc">${esc(sp.desc)}</div>
      <button class="s-btn" data-starter="${i}">${(sp.step1 || []).length}項目で始める</button>
    </div>`).join('')}</div>` : '';
  $$('#starterBox [data-starter]').forEach((btn) => btn.addEventListener('click', () =>
    applyStarter(starters[Number(btn.dataset.starter)])));

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
    const mt = esc(m.metric);
    const matches = f.step2_matches.map((x, j) =>
      `<button class="cchip" data-kind="match" data-metric="${mt}" data-idx="${j}"
         aria-pressed="false">${esc(x.label)}</button>`).join('');
    return `<div class="item" data-metric="${mt}">
      <div class="head">
        <label class="toggle">
          <input type="checkbox" data-metric="${mt}" aria-label="${esc(m.label)}を使う">
          <span class="nm">${esc(m.label)}${thinMark(m)}</span>
        </label>
        <span class="st">使わない</span>
        ${(m.term || m.low_sample) ? `<button class="pinfo" data-term="${esc(m.term || 'low_sample')}"
          aria-label="${esc(m.label)}の説明">ⓘ</button>` : ''}
        <button class="disc" aria-label="${esc(m.label)}の詳細を開く" aria-expanded="false">▾</button>
      </div>
      <div class="body">
        <div class="mini-label">どの条件のレースで</div>
        <div class="cellrow">${matches}</div>
        <div class="mini-label">どこまでさかのぼる</div>
        ${lookbackControl(m, f)}
        <p class="cellnote" data-cells="${mt}"></p>
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
  $$('#step2list .cchip[data-kind="match"]').forEach((chip) => chip.addEventListener('click', () => {
    const metric = chip.dataset.metric;
    const s = sel.step2.get(metric);
    if (!s) {   // 項目トグルOFFのままセルを押したらONにする (迷子防止)
      const cb = $(`#step2list input[data-metric="${cssEsc(metric)}"]`);
      cb.checked = true; cb.dispatchEvent(new Event('change'));
      return;
    }
    const set = s.matches;
    const idx = Number(chip.dataset.idx);
    if (set.has(idx)) {
      if (set.size === 1) return;   // 条件≥1 かつ 期間≥1 を必須
      set.delete(idx);
    } else set.add(idx);
    syncCells(metric);
    refreshSaveCta();
  }));
  bindLookback();
  bindTerms();
  bindAdvanced();
  refreshSaveCta();
}

/* C-1: 詳細設定 (STEP2) の開閉。閉じているときも選択数を見せる。 */
function bindAdvanced() {
  const head = $('#advHead'), body = $('#advBody');
  head.addEventListener('click', () => {
    const open = body.hidden;
    body.hidden = !open;
    head.setAttribute('aria-expanded', String(open));
    head.querySelector('.adv-caret').textContent = open ? '\u25b4' : '\u25be';
  });
}
/* A-2 回帰: 以前は <button class="pchip"> の中に ⓘ ボタンと「データ少なめ」チップを
 * 入れていた。**button の中に操作可能な要素を置けない** (HTML の内容モデル違反で、
 * パーサが吐き出す環境では表示が崩れ、そうでない環境でもスクリーンリーダーと
 * タップ判定が壊れる)。ⓘ をチップの **兄弟** に出し、チップ内は選択だけにする。
 * 「データ少なめ」はチップ内に残すが **操作不可の印** にし、説明は ⓘ に寄せる。 */
function step1Chip(s) {
  const info = s.term || s.low_sample
    ? `<button class="pinfo" data-term="${esc(s.term || 'low_sample')}"
        aria-label="${esc(s.label)}の説明">ⓘ</button>` : '';
  return `<span class="pchip-wrap">
    <button class="pchip" data-key="${esc(s.key)}">${esc(s.label)}${thinMark(s)}</button>
    ${info}</span>`;
}
/* 低サンプル項目の印。**操作不可** (説明は隣の ⓘ が開く)。 */
function thinMark(s) {
  return s.low_sample ? '<span class="thin-mark" aria-label="データ少なめ">少</span>' : '';
}
/* C-2: 「直近1〜10レース」の11チップをスライダー1つに畳む。9項目 × 11 = 99個の
 * チップが並んでいた。期間は連続量なので選択肢として並べる必要がない。
 * 「これまでの全走」は連続量の外にあるので別トグルで残す (どちらか一方は必須)。
 * 内部のデータ構造 (lookbacks は Set) は変えない — 複数期間を持つ保存済み設定を
 * 黙って書き換えないため。複数持っている設定はその旨を表示して保持する。 */
const LB_MAX = 10;
function lookbackControl(m, f) {
  const mt = esc(m.metric);
  const allIdx = f.step2_lookbacks.findIndex((x) => x.value == null);
  const allLabel = allIdx >= 0 ? f.step2_lookbacks[allIdx].label : 'これまでの全走';
  // ON/OFF は <label> + checkbox ではなく button チップにする。
  // 実機で checkbox を label で包むと、プログラム的な click が label に転送されて
  // 二重にトグルされ、状態が戻る事象が出た。一致条件と同じ button 方式に揃えると
  // 「押した = 選んだ」が DOM の checked に依存しなくなる (正本は sel.step2)。
  return `<div class="lbctl">
    <div class="cellrow">
      <button class="cchip" data-kind="lbuse" data-metric="${mt}"
        aria-pressed="false">直近のレース数で区切る</button>
      <button class="cchip" data-kind="lball" data-metric="${mt}"
        aria-pressed="false">${esc(allLabel)}も使う</button>
    </div>
    <div class="lb-slide">
      <input type="range" min="1" max="${LB_MAX}" value="3" step="1"
        data-metric="${mt}" data-kind="lbrange"
        aria-label="${esc(m.label)}をさかのぼるレース数">
      <output class="lb-out" data-metric="${mt}">直近3レース</output>
    </div>
    <p class="lb-multi hidden" data-metric="${mt}"></p>
  </div>`;
}
/* lookbacks の Set (index) → スライダーとトグルの状態 */
function lbState(sset) {
  const all = state.features.step2_lookbacks;
  const idx = Array.from(sset.lookbacks);
  const nums = idx.filter((i) => all[i] && all[i].value != null)
    .map((i) => Number(all[i].value)).sort((a, b) => a - b);
  return { hasAll: idx.some((i) => all[i] && all[i].value == null), nums };
}
function lbIdx(value) {
  return state.features.step2_lookbacks.findIndex((x) =>
    (value == null ? x.value == null : Number(x.value) === Number(value)));
}
function bindLookback() {
  $$('#step2list [data-kind="lbrange"]').forEach((el) => el.addEventListener('input', () => {
    const metric = el.dataset.metric;
    const sset = ensureMetricOn(metric);
    if (!sset) return;
    // スライダーは「直近N走」を1つに確定させる (複数持ちだった設定はここで1つになる)
    lbState(sset).nums.forEach((n) => sset.lookbacks.delete(lbIdx(n)));
    sset.lookbacks.add(lbIdx(Number(el.value)));
    syncCells(metric);
    refreshSaveCta();
  }));
  $$('#step2list [data-kind="lball"], #step2list [data-kind="lbuse"]')
    .forEach((el) => el.addEventListener('click', () => {
      const metric = el.dataset.metric;
      const sset = ensureMetricOn(metric);
      if (!sset) return;
      const kind = el.dataset.kind;
      const range = $(`#step2list [data-kind="lbrange"][data-metric="${cssEsc(metric)}"]`);
      const st = lbState(sset);
      const on = kind === 'lball' ? st.hasAll : st.nums.length > 0;
      if (kind === 'lball') {
        if (on) sset.lookbacks.delete(lbIdx(null));
        else sset.lookbacks.add(lbIdx(null));
      } else if (on) {
        st.nums.forEach((n) => sset.lookbacks.delete(lbIdx(n)));
      } else {
        sset.lookbacks.add(lbIdx(Number(range.value)));
      }
      // 期間が空になる操作は認めない (片方は必ず残す)
      if (sset.lookbacks.size === 0) {
        if (kind === 'lball') sset.lookbacks.add(lbIdx(null));
        else sset.lookbacks.add(lbIdx(Number(range.value)));
        toast('期間はどちらか一方は必要です');
      }
      syncCells(metric);
      refreshSaveCta();
    }));
}
/* 項目トグルOFFのまま期間を触ったらONにする (チップと同じ迷子防止) */
function ensureMetricOn(metric) {
  const got = sel.step2.get(metric);
  if (got) return got;
  const cb = $(`#step2list input[type=checkbox][data-metric="${cssEsc(metric)}"]:not([data-kind])`);
  cb.checked = true; cb.dispatchEvent(new Event('change'));
  return sel.step2.get(metric);
}
/* チップの見た目と aria-pressed を選択状態に合わせる (正本は sel.step2) */
function chipOn(el, on) {
  if (!el) return;
  el.classList.toggle('on', !!on);
  el.setAttribute('aria-pressed', String(!!on));
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
  sel.step2 = new Map();
  $$('#step1groups .pchip').forEach((el) =>
    el.classList.toggle('on', sel.step1.has(el.dataset.key)));
  $$('#step2list .item').forEach((item) => {
    const cb = item.querySelector('input[type=checkbox]');
    if (cb) cb.checked = false;
    item.classList.remove('open');
    const disc = item.querySelector('.disc');
    if (disc) { disc.textContent = '▾'; disc.setAttribute('aria-expanded', 'false'); }
    syncCells(item.dataset.metric);
  });
  refreshSaveCta();
  toast(`「${sp.label}」の項目を選びました。そのまま保存できます。`);
}

/* サマリーは選択状態と常に同期する */
function syncCells(metric) {
  const item = $(`#step2list .item[data-metric="${cssEsc(metric)}"]`);
  const s = sel.step2.get(metric);
  item.querySelectorAll('.cchip[data-kind="match"]').forEach((c) => {
    chipOn(c, !!s && s.matches.has(Number(c.dataset.idx)));
  });
  syncLookbackUi(item, metric, s);
  const st = item.querySelector('.st');
  if (!s) { st.textContent = '使わない'; return; }
  // C-2b: 折りたたんでいるときはこの1行しか見えないので、組数もここに出す
  const n = s.matches.size * s.lookbacks.size;
  st.textContent = `${summary(state.features.step2_matches, s.matches)}`
    + ` × ${summary(state.features.step2_lookbacks, s.lookbacks)}`
    + (n > 1 ? ` · ${n}通り` : '');
}
/* スライダー・トグル・組数表示を選択状態に合わせる (C-2) */
function syncLookbackUi(item, metric, s) {
  const range = item.querySelector('[data-kind="lbrange"]');
  const all = item.querySelector('[data-kind="lball"]');
  const use = item.querySelector('[data-kind="lbuse"]');
  const out = item.querySelector('.lb-out');
  const multi = item.querySelector('.lb-multi');
  const note = item.querySelector('.cellnote');
  if (!range) return;
  const st = s ? lbState(s) : { hasAll: false, nums: [] };
  const useOn = st.nums.length > 0;
  chipOn(all, st.hasAll);
  chipOn(use, useOn);
  if (useOn) range.value = String(st.nums[0]);
  range.disabled = !useOn;
  out.textContent = lbLabel(Number(range.value));
  // 複数の「直近N走」を持つ保存済み設定を黙って1つに丸めない。
  // 保持したまま、スライダーを動かせば1つになることを明示する。
  if (st.nums.length > 1) {
    multi.classList.remove('hidden');
    multi.textContent = `この設定は期間を${st.nums.length}種類使っています`
      + `(${st.nums.map((n) => lbLabel(n)).join('・')})。`
      + `スライダーを動かすと1つにまとまります。`;
  } else {
    multi.classList.add('hidden');
    multi.textContent = '';
  }
  // C-2b: 選択の直積でセルが生成されるのに、生成数が画面に出ていなかった
  const cells = s ? s.matches.size * s.lookbacks.size : 0;
  note.textContent = cells ? `この項目で${cells}通りの組み合わせを使います` : '';
}
function lbLabel(n) {
  const got = state.features.step2_lookbacks.find((x) => Number(x.value) === Number(n));
  return got ? got.label : `直近${n}レース`;
}
function summary(all, set) {
  const idx = Array.from(set).sort((a, b) => a - b);
  if (!idx.length) return '未選択';
  const first = all[idx[0]].label;
  return idx.length === 1 ? first : `${first} ほか${idx.length - 1}`;
}
/* 詳細設定 (STEP2) で生成されるセルの総数。C-2b: 直積の規模を見せる。 */
function totalCells() {
  let n = 0;
  sel.step2.forEach((s) => { n += s.matches.size * s.lookbacks.size; });
  return n;
}

function refreshSaveCta() {
  const n = sel.step1.size + sel.step2.size;
  const btn = $('#saveBtn');
  btn.disabled = n === 0;
  const cells = totalCells();
  $('#saveReason').textContent = n === 0
    ? '項目を1つ以上選んでください'
    : (cells ? `${n}項目を選択中(詳細設定で${cells}通りの組み合わせ)` : `${n}項目を選択中`);
  // 折りたたんだままでも詳細設定の中身が分かるようにする (C-1)
  const badge = $('#advCount');
  if (badge) {
    badge.textContent = sel.step2.size
      ? `${sel.step2.size}項目 · ${cells}通り` : '未設定';
    badge.classList.toggle('on', sel.step2.size > 0);
  }
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
    const saved = await postJSON('/api/configs', {
      config: buildConfig(), config_id: state.config && state.config.id,
    });
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
    bt = await postJSON('/api/backtest', {
      config: buildConfig(),
      config_id: state.config ? state.config.id : undefined,
    });
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
    <div class="card bt2">
      <div class="bt2-head"><span></span>
        <span class="bt2-pair"><span>このAI</span><span class="bt2-sep"></span>
          <span>1番人気AI</span></span><span>差</span></div>
      ${btRow('◎が1着だった割合', you.hit_rate_win, base.hit_rate_win)}
      ${btRow('◎が3着以内', you.hit_rate_show, base.hit_rate_show)}
      ${btRow('印の中に勝ち馬', you.hit_rate_in_marks, base.hit_rate_in_marks)}
      <div class="bt2-foot">${you.races}レース · ${esc(ymd(bt.period[0]))}以降</div>
    </div>
    ${btRoiBlock(bt)}
    ${condBreakdown(bt)}
    ${generationBlock(bt)}
    ${holdoutBlock(bt)}
    <p class="note" style="margin-top:8px">${esc(bt.note || '')}</p>
    <button class="cta" data-go="races">レースを選んで予想する</button>`;
  bindTerms();
  bindGo();
  bindHoldout();
}

/* ======================= 選び直しの記録と封印期間 =========================
 *
 * バックテストの数字を見ながら項目を選び直すと、その数字は **選び直した回数の
 * ぶんだけ楽観側に寄る**。182,594 候補を機械で探索して out-of-sample のエッジが
 * 出なかったのと同じことが手作業でも起きる。
 * ここでやるのは2つだけ:
 *   1. 何世代目かと、世代ごとの数字の動きを見せる (上がっていく形が痕跡)
 *   2. 封印期間の成績は **開けるまで出さない** (サーバが返さない)
 * どちらも「この数字を信じてよいか」を判断する材料であって、良し悪しは言わない。 */

function generationBlock(bt) {
  const n = bt.generations || 0;
  const hist = bt.score_history || [];
  if (n < 2) return '';
  const rows = hist.filter((h) => h.win_rate != null).map((h) => {
    const w = Math.round(h.win_rate * 100);
    return `<li class="gen-item"><span class="gen-v">v${h.version}</span>
      <span class="gen-bar"><i style="width:${Math.max(2, w * 2)}%"></i></span>
      <span class="gen-n num">${w}%</span></li>`;
  }).join('');
  return `<div class="card gen">
    <div class="gen-h">この設定は <b>${n}世代目</b> です</div>
    <p class="gen-note">数字を見ながら選び直すと、上の成績は<b>選び直した回数のぶんだけ
      良く出ます</b>。下は世代ごとの「◎が1着だった割合」です。
      右肩上がりなら、その分だけ差し引いて読んでください。</p>
    <ol class="gen-list">${rows}</ol>
  </div>`;
}

function holdoutBlock(bt) {
  const h = bt.holdout;
  if (!h || !h.races) return '';
  // 開封済みかどうかは **成績が返ってきているか** で判定する
  // (サーバは頼まれるまで your_ai を返さない)
  const opened = !!h.your_ai;
  const wasOpened = h.revealed_at_version != null;
  if (opened) {
    const you = h.your_ai || {}, base = h.baseline_favorite || {};
    return `<div class="card hold open">
      <div class="hold-h">封印期間の成績 <span class="hold-n">${h.races}レース</span></div>
      <p class="gen-note">${esc(ymd(h.from))}以降は、項目を選んでいるあいだ
        見えていなかった期間です。<b>上の成績との差が、選び直しで得た見かけの分</b>です。</p>
      <div class="bt2">
        <div class="bt2-head"><span></span>
          <span class="bt2-pair"><span>このAI</span><span class="bt2-sep"></span>
            <span>1番人気AI</span></span><span>差</span></div>
        ${btRow('◎が1着だった割合', you.hit_rate_win, base.hit_rate_win)}
        ${btRow('◎が3着以内', you.hit_rate_show, base.hit_rate_show)}
      </div>
      <p class="gen-note hold-once">この設定の封印は開封済みです
        (v${h.revealed_at_version})。<b>これ以降に項目を選び直すと、封印の意味は
        失われます</b> — 見た数字に合わせて選べてしまうためです。</p>
    </div>`;
  }
  return `<div class="card hold">
    <div class="hold-h">封印期間 <span class="hold-n">${h.races}レース</span></div>
    <p class="gen-note">${esc(ymd(h.from))}以降は<b>上の成績に入っていません</b>。
      項目を選び終えてから開けると、選び直しの影響を受けていない数字で確かめられます。</p>
    ${wasOpened
      ? `<p class="gen-note hold-once">この設定は v${h.revealed_at_version} で
          開封済みです。以降の選び直しは封印の意味を失っています。</p>`
      : ''}
    <button class="hold-btn" id="holdBtn">封印期間の成績を見る</button>
    <p class="gen-note">一度見ると取り消せません。</p>
  </div>`;
}

function bindHoldout() {
  const btn = $('#holdBtn');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    if (!state.config) { toast('先にマイAIを保存してください'); return; }
    btn.disabled = true;
    btn.textContent = '開けています…';
    try {
      const bt = await postJSON('/api/backtest', {
        config: buildConfig(), config_id: state.config.id, reveal: true,
      });
      renderHoldout(bt);
    } catch (err) {
      btn.disabled = false;
      btn.textContent = '封印期間の成績を見る';
      toast('封印期間を開けませんでした');
    }
  });
}

function renderHoldout(bt) {
  const card = $('#btResult .hold');
  if (!card) return;
  card.outerHTML = holdoutBlock(bt);
  bindTerms();
}

/* B-1 回帰: 自分の値を 1.2rem 太字、基準を 0.72rem 淡色で出していたため、
 * **数字の強弱が実際の優劣と逆** になっていた (19% が大きく、基準 33% が小さい)。
 * 基準を同格に並べ、差を主表示にする。 */
const btCell = (k, v, b) => `<div class="bt-cell"><div class="k">${esc(k)}</div>
  <div class="v num">${esc(v)}</div><div class="b">${esc(b)}</div></div>`;
function btRow(label, you, base) {
  if (you == null || base == null) {
    return `<div class="bt2-row"><div class="bt2-k">${esc(label)}</div>
      <div class="bt2-v">—</div></div>`;
  }
  const diff = Math.round((you - base) * 100);
  const sign = diff > 0 ? `+${diff}` : `${diff}`;
  const cls = diff > 0 ? 'up' : (diff < 0 ? 'down' : 'even');
  return `<div class="bt2-row">
    <div class="bt2-k">${esc(label)}</div>
    <div class="bt2-pair"><span class="bt2-you num">${pct(you)}</span>
      <span class="bt2-sep">対</span>
      <span class="bt2-base num">${pct(base)}</span></div>
    <div class="bt2-diff ${cls} num">${sign}pt</div>
  </div>`;
}
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
  // 終了レースの結果はマイAI不要。サーバ側で終了を確認してから結果だけを返す。
  // 深いURLから開いた直後で一覧取得が未完了でも、この経路なら結果を表示できる。
  if (noAi && !noRace) {
    $('#predEmpty').classList.add('hidden');
    $('#predNoRace').classList.add('hidden');
    $('#predBody').classList.remove('hidden');
    bindGo();
    stopPolling();
    await loadRaceResult();
    return;
  }
  $('#predEmpty').classList.toggle('hidden', !noAi);
  $('#predNoRace').classList.toggle('hidden', noAi || !noRace);
  $('#predBody').classList.toggle('hidden', noAi || noRace);
  bindGo();
  stopPolling();
  if (!noAi && !noRace) {
    loadPredict();
    const selected = state.races.find((race) => race.race_id === state.selectedRaceId);
    if ((!selected || !selected.finished)
        && (!state.selectedRaceDate || state.selectedRaceDate === state.todayDate)) startPolling();
  }
}

async function loadRaceResult() {
  $('#contextTrend').innerHTML = '';
  let p;
  try {
    const payload = {
      race_id: state.selectedRaceId, date: state.selectedRaceDate, result_only: true,
    };
    // AIを選んでいる場合は、そのAIへ何を1項目追加すれば好走馬を拾えたかを
    // 比較する。未選択でも結果閲覧は維持し、その場合は各項目単独で比較する。
    if (state.config) {
      payload.config = buildConfig();
      payload.config_id = state.config.id;
    }
    p = await postJSON('/api/predict', payload);
    clearStale('#predWarn');
  } catch (err) {
    // 発走前レースなら従来どおりマイAI作成案内を出す。
    if (err.status === 409) {
      $('#predBody').classList.add('hidden');
      $('#predEmpty').classList.remove('hidden');
      bindGo();
      return;
    }
    staleChip('#predWarn', 'レース結果');
    return;
  }
  state.lastPredict = p;
  renderPredict(p, null);
}

async function loadPredict() {
  let p;
  try {
    p = await postJSON('/api/predict',
      { race_id: state.selectedRaceId, config: buildConfig(),
        date: state.selectedRaceDate,
        config_id: state.config && state.config.id });
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
    if (p.finished) $('#contextTrend').innerHTML = '';
    else loadContextTrend(p);
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
  const resultOnly = Boolean(p.result_only);
  $('#predBody').classList.toggle('result-mode', Boolean(p.finished));
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
      <h2>${esc(raceTitleOf({ ...r0, race_num: p.race_num,
        race_title: p.race_title, race_class: p.race_class }))}</h2>
      <span class="off num">発走 ${esc(p.start_time || '--:--')}</span>
    </div>
    <div class="rcond">${esc(cond)}</div>
    <div class="bottom">
      ${resultOnly ? '<span class="head-chip">確定結果・払戻</span>' : `
      <button class="badge-conf${conf.downgraded ? ' low' : ''}" id="confBadge">${esc(conf.label || '—')}<span class="ci">ⓘ</span></button>
      <span class="head-chip" data-term="coverage">分析に使えた項目 ${used}/${total}</span>
      ${(p.history_reliability && p.history_reliability.profile !== 'standard')
        ? `<span class="head-chip">${esc(p.history_reliability.label || '')}</span>` : ''}
      ${p.weight_announced ? '' : '<span class="head-chip">暫定印(馬体重の発表前)</span>'}`}
      ${isoHM(p.live_source_checked_at || p.live_updated_at) ? `<span class="head-chip">速報確認 ${esc(isoHM(p.live_source_checked_at || p.live_updated_at))}</span>` : ''}
      ${resultOnly ? '' : `<button class="ai-pick" id="aiPick">予想: <b>${esc(state.config ? state.config.name : '')}</b> ▾</button>`}
      ${p.finished ? '<button class="result-back" id="resultBack">同日のレース一覧</button>' : ''}
      <button class="refresh" id="refreshBtn" aria-label="${p.finished ? 'レース結果を更新' : 'オッズと印を更新'}">更新</button>
    </div>
    ${!resultOnly && conf.downgrade_reason ? `<div class="conf-note">${esc(conf.downgrade_reason)}</div>` : ''}
    ${resultOnly ? '' : condRecordLine(p)}`;
  if (!resultOnly) {
    $('#confBadge').addEventListener('click', () => openTermSheet('confidence'));
    $('#aiPick').addEventListener('click', openAiSheet);
  } else if (!p.finished) {
    $('#screenTitle').textContent = 'レース結果';
    $('#screenSub').textContent = '確定した着順と払戻を確認できます';
  }
  if (p.finished) {
    $('#screenTitle').textContent = 'レース結果';
    $('#screenSub').textContent = '着順・払戻と評価項目を確認できます';
  }
  $('#predictDisclaimer').hidden = Boolean(p.finished);
  const resultBack = $('#resultBack');
  if (resultBack) resultBack.addEventListener('click', () => go('races'));
  $('#refreshBtn').addEventListener('click', async () => {
    await loadRaces(true);
    if (resultOnly) await loadRaceResult(); else await loadPredict();
    if (bet.entries.length) await refreshBet(state.lastPredict || p);
    toast(p.finished ? 'レース結果を更新しました' : 'オッズと印を取り直しました');
  });
  setMiniHead(p);

  // warnings は印リストの上に出す
  const warns = resultOnly ? [] : (p.warnings || []);
  const scratches = p.scratched_horses || [];
  const staleNotice = p.live_source_fresh === false ? `<div class="warn-card">
      <div class="m">速報情報を更新中です</div>
      <div class="h">開催変更情報が90秒以内に確認できるまで、スマッピーQRは作成しません。</div></div>` : '';
  const scratchNotice = scratches.length ? `<div class="warn-card scratch-card">
      <div class="m">取消・除外を反映しました</div>
      <div class="h">${scratches.map((h) => `${esc(Number(h.horse_num))}番 ${esc(h.horse_name || '')}（${esc(h.label || '取消・除外')}）`).join('・')}</div>
      <div class="h">この馬はAI評価・買い目・スマッピーQRの対象から外れています。</div></div>` : '';
  const timeChangeNotice = p.start_time_changed ? `<div class="warn-card change-card">
      <div class="m">発走時刻が変更されました</div>
      <div class="h">${esc(p.original_start_time || '--:--')} → ${esc(p.start_time || '--:--')}。締切と結果取得も変更後の時刻で処理します。</div></div>` : '';
  const courseChange = p.course_change || {};
  const courseChangeNotice = p.course_changed ? `<div class="warn-card change-card">
      <div class="m">コースが変更されました</div>
      <div class="h">${esc(`${courseChange.old_surface_label || ''}${courseChange.old_distance ? `${courseChange.old_distance}m` : ''}`)} → ${esc(`${courseChange.new_surface_label || ''}${courseChange.new_distance ? `${courseChange.new_distance}m` : ''}`)}（${esc(courseChange.reason || '主催者発表')}）。AI評価は変更後の条件で再計算しています。</div></div>` : '';
  const notices = [staleNotice, timeChangeNotice, courseChangeNotice, scratchNotice,
    ...warns.map((w) => `<div class="warn-card">
      <div class="m">${esc(w.message)}</div>
      ${w.hint ? `<div class="h">${esc(w.hint)}</div>` : ''}
      ${warnColumns(w)}</div>`)].filter(Boolean);
  const predWarn = $('#predWarn');
  if (p.finished) {
    predWarn.innerHTML = notices.length ? `<details class="result-diagnostics">
      <summary>変更・注意情報を確認（${notices.length}件）</summary>
      <div class="result-diagnostics-body">${notices.join('')}</div></details>` : '';
  } else {
    predWarn.innerHTML = notices.join('');
  }

  // 発走済みのレースでは印を出さず結果を出す (印は発走前のもの)
  if (p.finished) {
    $('#markLegend').innerHTML = '';
    $('#markList').innerHTML = `<div class="finished">
      <div class="f-title">このレースは終了しています</div>
      ${resultReviewAiBlock(p.result_review_ai)}
      ${(p.result || []).length ? `<div class="f-body">${finishedResultRows(p)}</div>`
             : '<div class="f-note">結果はまだ取り込まれていません。</div>'}
      ${payoutsBlock(p.payouts || [])}
      <div class="f-note">各馬の内訳は、発走前データで評価を上げた項目と、実際の評価順位です。</div>
      <div class="f-note">印は発走前のレースにだけ表示します。</div></div>`;
    // 締切後はJRAへ送らない。実際に購入済みの内容だけを事後記録できる。
    // 出走馬一覧は result_only 応答にも含まれるため、マイAI未選択でも入力可能。
    initBet(p, {recordOnly: true});
    $('#betSlip').innerHTML = betSlipBlock(p);
    renderBet(p);
    bindTerms();
    return;
  }

  // 印が意味を持たない警告が1つでもあれば印を出さない。ここは **列挙で塞ぐ**
  // (新しい警告コードが増えたときに黙って印を出してしまわないよう、
  //  「印を出しても良い警告」の側を列挙する)
  const HARMLESS = ['low_sample_columns', 'excluded_columns_dropped',
                    'columns_skipped_in_race', 'limited_history'];
  const blocked = warns.some((w) => !HARMLESS.includes(w.code));
  if (blocked) {
    const manualAllowed = warns.some((w) => w.code === 'all_columns_gated_out')
      && warns.every((w) => HARMLESS.includes(w.code) || w.code === 'all_columns_gated_out');
    $('#markLegend').innerHTML = '';
    if (!manualAllowed) {
      $('#markList').innerHTML = ''; $('#betSlip').innerHTML = '';
      return;
    }
    $('#markList').innerHTML = `<div class="manual-bet-note">
      AI印はデータ不足のため表示できません。買い目は下の出走馬から手動で選択できます。</div>`;
    initBet(p);
    $('#betSlip').innerHTML = betSlipBlock(p);
    renderBet(p);
    bindTerms();
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

  // A-1 回帰: 以前は |score| / max|score| でバーを描いていた。スコアは符号付き
  // (重み付き z の総和) なので、**大きく負の馬ほどバーが長くなり**、12頭中12位の
  // 無印馬が◎と同じ長さになっていた。最小〜最大で正規化して順位と一致させる。
  const scores = marks.map((m) => m.score || 0);
  const sMin = Math.min(...scores, 0);
  const sMax = Math.max(...scores, 0);
  const sSpan = (sMax - sMin) || 1;
  const prevMark = {};
  if (prev) (prev.marks || []).forEach((m) => { prevMark[m.horse_num] = m.mark; });

  $('#markList').innerHTML = marks.map((m) => {
    const isHon = m.mark === '◎';
    const upset = isHon && m.popularity != null && m.popularity !== 1;
    const w = Math.max(2, Math.round((((m.score || 0) - sMin) / sSpan) * 100));
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

  initBet(p);
  $('#betSlip').innerHTML = betSlipBlock(p);
  renderBet(p);
  // 参加者が組み替えた状態で予想を取り直したら、その指定で作り直す
  if (bet.dirty) refreshBet(p);

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

/* A-4 回帰: `.track-head{top:96px}` を決め打ちしていたため、検証モードバナー
 * (sticky) が挟まると会場見出しがヘッダに食い込んだ。実測して変数に入れる。 */
function syncHeaderHeight() {
  let h = 0;
  ['header.app', '#demoBanner', '#previewBanner'].forEach((sel) => {
    const el = $(sel);
    if (el && !el.hidden) h += el.offsetHeight;
  });
  if (h > 0) document.documentElement.style.setProperty('--head-h', `${h}px`);
}
window.addEventListener('resize', syncHeaderHeight);

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
  const nUsable = plus.length + minus.length;
  const rows = (list, cls) => {
    const maxAbs = Math.max(...list.map((c) => Math.abs(c.contribution || 0)), 1e-9);
    return list.map((c) => {
      const a = Math.abs(c.contribution || 0);
      const w = Math.max(2, Math.round((a / maxAbs) * 100));
      // B-5: 使えた項目が1つだけならシェアは定義上必ず 100% で、全馬に
      // 「100%」が並ぶだけになる。意味を持たないので数値を出さない。
      const share = (total > 0 && nUsable > 1) ? Math.round((a / total) * 100) : null;
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
    const before = state.races.find((x) => x.race_id === state.selectedRaceId) || {};
    const r = (data.races || []).find((x) => x.race_id === state.selectedRaceId);
    if (!r) {
      state.races = data.races || [];
      state.selectedRaceId = null;
      state.lastPredict = null;
      stopPolling();
      go('races');
      toast('日付が変わりました。本日のレース一覧へ切り替えました。');
      return;
    }
    state.races = data.races;
    const weightPublished = !before.weight_announced && r.weight_announced;
    const shownAsOf = state.lastPredict && state.lastPredict.odds_as_of;
    const oddsChanged = Boolean(r.odds_as_of && r.odds_as_of !== shownAsOf);
    const revisionChanged = Boolean(
      r.live_revision && before.live_revision && r.live_revision !== before.live_revision);
    if (weightPublished || oddsChanged || revisionChanged) {
      const prev = state.lastPredict;
      await loadPredict();
      if (weightPublished) announceChanges(prev, state.lastPredict);
    }
    // E-1 回帰: 再取得のトリガーが馬体重発表だけだったため、「09:50時点」の
    // オッズが **最も動く発走直前まで** 残っていた。取得時刻が進んでいたら
    // 印も取り直す (印はオッズを使わないが、表示中のオッズを古くしない)。
    // 券種別オッズは日次行列ではなく read-only の最新DB値を読むため、
    // 一覧の odds_as_of が変わらなくても選択済み買い目だけは更新する。
    if (bet.entries.length) await refreshBet(state.lastPredict);
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

/* ------------------------------------------ 画面4: 本日の利用者別成績 */
function registrationRaceDetailsHtml(entry, range) {
  if (!state.auth || state.auth.role !== 'admin') return '';
  const details = (entry.details || []).slice().reverse();
  if (!details.length) return '';
  const multipleDates = (range.dates || []).length > 1;
  const rows = details.map((x) => {
    const s = x.settlement || {};
    const invested = Number(s.invested_yen || 0);
    const returned = Number(s.returned_yen || 0);
    const profit = returned - invested;
    const result = x.finished
      ? `${s.hit_points ? `${Number(s.hit_points)}点的中` : '不的中'}・払戻 ${yen(returned)}`
      : '結果待ち';
    const date = multipleDates && x.date
      ? `<span class="purchase-date">${esc(formatDate(x.date))}</span>` : '';
    const track = x.track_label ? `<span class="track-patch">${esc(x.track_label)}</span>` : '';
    const time = x.start_time ? `<span class="purchase-time num">${esc(x.start_time)}</span>` : '';
    const ai = x.config_name ? `<p class="registration-ai"><b>使用AI</b> ${esc(x.config_name)}</p>` : '';
    const factors = (x.factors || []).length
      ? `<p class="registration-ai"><b>選択項目</b> ${(x.factors || []).map(esc).join('・')}</p>` : '';
    return `<details class="registration-race-row"><summary>
      <span><span class="purchase-race-meta">${date}${track}${time}</span>
        <b>${Number(x.race_num)}R ${esc(x.race_name || '')}</b></span>
      <strong>${result}</strong></summary><div>
        <div class="settlement-breakdown">
          <div><span>登録額</span><b>${yen(invested)}</b></div>
          <div><span>払戻</span><b>${x.finished ? yen(returned) : '結果待ち'}</b></div>
          <div><span>収支</span><b class="${profit > 0 ? 'plus' : profit < 0 ? 'minus' : ''}">${x.finished ? `${profit > 0 ? '+' : ''}${yen(profit)}` : '—'}</b></div>
        </div>${ticketRowsHtml(x, '登録')}${ai}${factors}</div></details>`;
  }).join('');
  return `<details class="user-registration-details"><summary>発走前QR登録のレース別内訳（${details.length}件）</summary>
    <p>利用者別成績に採用した、各レース最後の発走前登録です。</p>${rows}</details>`;
}

function renderUserLeaderboard(d) {
  const box = $('#userLeaderboard');
  if (!box) return;
  const entries = d.entries || [];
  if (!entries.length) {
    box.innerHTML = `<section class="user-board-section">
      <div class="section-label">${esc(d.range_label || '今日')}の発走前登録成績</div>
      <div class="user-board-empty"><b>この期間の買い目はまだありません</b>
        <span>期間を切り替えると、過去の登録成績も確認できます。</span></div>
      <p class="user-board-note">${esc(d.note || '')}</p></section>`;
    box.dataset.ready = '1';
    return;
  }
  const showRanks = entries.length > 1;
  const cards = entries.map((e) => {
    const ranked = Number.isFinite(Number(e.rank)) && e.rank != null;
    const rank = showRanks ? (ranked ? `${Number(e.rank)}位` : '参考') : '';
    const profit = Number(e.profit_yen || 0);
    const profitText = `${profit > 0 ? '+' : ''}${yen(profit)}`;
    return `<article class="card user-score-card${showRanks && ranked && Number(e.rank) === 1 ? ' top' : ''}">
      <div class="user-score-head${showRanks ? '' : ' no-rank'}">
        ${showRanks ? `<span class="user-rank">${rank}</span>` : ''}
        <div><b>${esc(e.display_name || '利用者')}</b><small>${Number(e.settled_races || 0)}レース確定 / ${Number(e.registered_races || 0)}レース登録</small></div>
        <strong>${e.roi == null ? '結果待ち' : pct(e.roi)}</strong></div>
      <div class="user-score-stats">
        <div><span>登録額</span><b>${yen(e.registered_yen)}</b></div>
        <div><span>確定払戻</span><b>${yen(e.returned_yen)}</b></div>
        <div><span>確定収支</span><b class="${profit > 0 ? 'plus' : profit < 0 ? 'minus' : ''}">${profitText}</b></div>
        <div><span>的中率</span><b>${e.hit_rate == null ? '結果待ち' : pct(e.hit_rate)}</b></div>
      </div>
      ${showRanks && e.provisional ? `<p class="provisional">順位確定まであと${Math.max(0, Number(d.minimum_races_for_rank || 0) - Number(e.settled_races || 0))}レース</p>` : ''}
      ${registrationRaceDetailsHtml(e, d)}
      </article>`;
  }).join('');
  box.innerHTML = `<section class="user-board-section"><div class="section-label">${esc(d.range_label || '今日')}・利用者別（発走前QR登録）${showRanks ? `・${entries.length}人` : ''}</div>
    ${cards}<p class="user-board-note">${esc(d.note || '')}</p></section>`;
  box.dataset.ready = '1';
}

async function loadPurchaseBoard(rangeKey = state.performanceRange) {
  const purchaseBox = $('#purchaseSummary');
  const purchaseListBox = $('#purchaseList');
  if (purchaseBox && !purchaseBox.dataset.ready) {
    purchaseBox.innerHTML = '<div class="user-board-loading"><b>購入成績を読み込んでいます</b><span>集計済みの記録を表示します。</span></div>';
  }
  if (purchaseBox) purchaseBox.setAttribute('aria-busy', 'true');
  if (purchaseListBox) purchaseListBox.setAttribute('aria-busy', 'true');
  try {
    renderPurchaseSummary(await getJSON(`/api/purchases?range=${encodeURIComponent(rangeKey)}`));
    return true;
  } catch (err) {
    if (purchaseBox && !purchaseBox.dataset.ready) {
      purchaseBox.innerHTML = '<div class="user-board-empty">購入記録を取得できませんでした。時間をおいて再表示してください。</div>';
      if (purchaseListBox) purchaseListBox.innerHTML = '';
    }
    return false;
  } finally {
    if (purchaseBox) purchaseBox.setAttribute('aria-busy', 'false');
    if (purchaseListBox) purchaseListBox.setAttribute('aria-busy', 'false');
  }
}

async function loadUserBoard(rangeKey = state.performanceRange) {
  const userBox = $('#userLeaderboard');
  if (userBox && !userBox.dataset.ready) {
    userBox.innerHTML = '<div class="user-board-loading"><b>選択した期間を集計しています</b><span>購入記録は自動で更新されます。</span></div>';
  }
  if (userBox) userBox.setAttribute('aria-busy', 'true');
  try {
    renderUserLeaderboard(await getJSON(`/api/user-leaderboard?range=${encodeURIComponent(rangeKey)}`));
    return true;
  } catch (err) {
    if (userBox && !userBox.dataset.ready) {
      userBox.innerHTML = '<div class="user-board-empty">利用者別成績を取得できませんでした。購入記録は下で確認できます。</div>';
    }
    return false;
  } finally {
    if (userBox) userBox.setAttribute('aria-busy', 'false');
  }
}

async function loadLeaderboard() {
  if (state.boardLoading) return;
  state.boardLoading = true;
  // 購入サマリーを最初に表示してから、時間のかかるライブ更新と全利用者集計へ進む。
  const purchaseOk = await loadPurchaseBoard(state.performanceRange);
  const users = loadUserBoard(state.performanceRange);
  try {
    // 常駐更新済みの一覧を読む。画面を開くたびに強制更新すると、連続再読込時に
    // 古いリクエストが積み上がって成績サマリーまで待たせてしまう。
    const latest = await getJSON('/api/races/today');
    state.races = latest.races || state.races;
    if (!state.todayDate) state.todayDate = latest.date;
    if (latest.date === state.todayDate) {
      state.todayRaceCount = (latest.races || []).length;
      syncBuildAvailability();
    }
    const analysis = $('.ai-analysis');
    if (analysis && analysis.open) await loadLeaderboardContent();
  } catch (err) {
    // 当日一覧はタブ制御用の補助情報。購入成績の取得成否とは分けて扱う。
  } finally {
    const usersOk = await users;
    if (purchaseOk && usersOk) clearStale('#boardWarn');
    else staleChip('#boardWarn', '成績');
    state.boardLoading = false;
  }
}
async function loadLeaderboardContent() {
  if (state.aiBoardLoading) return;
  state.aiBoardLoading = true;
  try { await renderAiLeaderboardContent(); }
  finally { state.aiBoardLoading = false; }
}
async function renderAiLeaderboardContent() {
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
  $('#aiBoardSub').textContent = d.n_races_finished
    ? `◎的中率で並べています · ${d.n_races_finished}レース終了時点`
      + (d.scoped_to_applied ? ' · そのレースに使ったAIで集計' : '')
    : 'レース結果が確定すると、使用したAI・項目の分析を表示します';
  if (d.ranking_rule) $('#boardRule').textContent = d.ranking_rule;
  if (!d.n_races_finished) {
    $('#boardList').innerHTML = boardEmpty('waiting');
    $('#roiRanking').innerHTML = '';
    $('#boardRoiNote').textContent = '';
    bindGo();
    return;
  }
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
  const aiRow = (e) => {
    const top = e.rank === 1;
    const stat = e.is_baseline
      ? `いつも1番人気を◎にするAI · ${e.races}レース`
      : `${e.races}レース · ◎的中率 <b>${pct(e.win_rate)}</b>`
        + ` · ◎が3着以内 ${pct(e.show_rate)}`
        + ` · 人気を出し抜いた的中 ${e.upset_hits}回`;
    return `<div class="brow${top ? ' top' : ''}${e.is_baseline ? ' baseline' : ''}">
      <div class="rank">${e.is_baseline ? '—' : esc(String(e.rank))}</div>
      <div class="who"><div class="aname">${esc(e.name)}${e.is_baseline ? '(基準)' : ''}</div>
        <div class="astat">${stat}</div></div>
      <div class="hits"><div class="n num">${e.win_hits}</div><div class="l">◎的中</div></div>
    </div>
    ${roiRow(e)}`;
  };
  const mineTop = entries.filter((e) => !e.is_baseline).slice(0, 3);
  const baselines = entries.filter((e) => e.is_baseline);
  const remaining = entries.filter((e) => !e.is_baseline).slice(3);
  $('#boardList').innerHTML = `<div class="card board">${[...mineTop, ...baselines].map(aiRow).join('')}</div>
    ${remaining.length ? `<details class="ai-more"><summary>そのほかのAI ${remaining.length}件を表示</summary>
      <div class="card board">${remaining.map(aiRow).join('')}</div></details>` : ''}`;
}

function startScreenPolling(key) {
  stopScreenPolling();
  state.screenPolling = setInterval(async () => {
    if (document.hidden || parseHash().key !== key || state.screenRefreshing) return;
    state.screenRefreshing = true;
    try {
      if (key === 'board') await loadLeaderboard();
      else if (key === 'races' && (!state.listDate || state.listDate === state.todayDate)) {
        await loadRaces(false, state.todayDate);
      }
    } finally {
      state.screenRefreshing = false;
    }
  }, 30000);
}
function stopScreenPolling() {
  if (state.screenPolling) clearInterval(state.screenPolling);
  state.screenPolling = null;
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  const key = parseHash().key;
  if (key === 'board') loadLeaderboard();
  else if (key === 'races' && (!state.listDate || state.listDate === state.todayDate)) {
    loadRaces(false, state.todayDate);
  }
});

function boardEmpty(kind) {
  const m = {
    waiting: ['レースの結果待ちです', '確定しだい集計されます。'],
    nomyai: ['マイAIで予想した成績はまだありません',
             '着順と払戻は、マイAIがなくても終了レースから確認できます。'],
    notready: ['まだ集計できていません', '当日のデータ作成が終わると表示されます。'],
  }[kind] || ['—', ''];
  const cta = kind === 'nomyai'
    ? '<button class="cta" data-go="races">終了レースの結果を見る</button>'
      + '<button class="quiet-btn" data-go="build">マイAIをつくる</button>' : '';
  return `<div class="empty"><div class="t">${esc(m[0])}</div>
    <div class="d">${esc(m[1])}</div>${cta}</div>`;
}

async function selectPerformanceRange(rangeKey) {
  if (rangeKey === state.performanceRange || state.boardLoading) return;
  state.performanceRange = rangeKey;
  $$('[data-performance-range]').forEach((item) => {
    const on = item.dataset.performanceRange === rangeKey;
    item.classList.toggle('on', on);
    item.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
  const labels = {today:'今日', previous:'前回開催', '7d':'直近7日', all:'累計'};
  $('.board-hero .h').textContent = `${labels[rangeKey] || '選択期間'}の成績`;
  ['#purchaseSummary', '#userLeaderboard', '#purchaseList'].forEach((selector) => {
    const node = $(selector);
    if (node) delete node.dataset.ready;
  });
  await loadLeaderboard();
}

/* ------------------------------------------------------------- 起動 */
function init() {
  $$('nav.tabs button').forEach((b) => b.addEventListener('click', () => go(b.dataset.scr)));
  $$('[data-performance-range]').forEach((button) => button.addEventListener('click', () => {
    selectPerformanceRange(button.dataset.performanceRange);
  }));
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
  if (h.raceId) {
    state.selectedRaceId = h.raceId;
    state.selectedRaceDate = /^\d{8}/.test(h.raceId) ? h.raceId.slice(0, 8) : null;
    // 終了レースのURLをスマホで再読込しても、同じ開催日の条件・一覧へ戻れるようにする。
    state.listDate = state.selectedRaceDate;
  }
  history.replaceState({ scr: h.key, raceId: h.raceId }, '', location.hash || '#races');
  // 成績はマイAI設定の復元を待たずに開ける。購入サマリーを先に表示し、
  // 連続再読み込み時にヒーローだけの空白画面になるのを避ける。レース一覧の
  // ライブ更新も購入サマリーの後に成績ローダーが行うため、ここでは重ねない。
  let initialRaceLoad = Promise.resolve();
  if (h.key === 'board') go('board', false);
  else initialRaceLoad = loadRaces(false, state.listDate);
  if (h.key === 'races') startScreenPolling('races');
  checkVersion();
  maybeInviteFirstRun();
  syncHeaderHeight();
  loadFeatures().then(restoreConfig).then(() => initialRaceLoad).then(() => {
    if (h.key !== 'races' && h.key !== 'board') go(h.key, false);
  });
  const aiAnalysis = $('.ai-analysis');
  if (aiAnalysis) aiAnalysis.addEventListener('toggle', () => {
    if (aiAnalysis.open) loadLeaderboardContent();
  });
}

function purchaseTicketKey(item) {
  const unordered = ['wakuren', 'umaren', 'wide', 'sanrenpuku'].includes(item.type);
  const combo = (item.combo || []).map(Number);
  if (unordered) combo.sort((a, b) => a - b);
  return `${item.type || ''}:${combo.join('-')}`;
}

function ticketRowsHtml(x, basis = '購入') {
  const settlement = x.settlement || {};
  const hitMap = new Map((settlement.hits || []).map((hit) => [purchaseTicketKey(hit), hit]));
  const rows = (x.items || []).map((item) => {
    const hit = hitMap.get(purchaseTicketKey(item));
    let outcome = `${basis}結果待ち`;
    if (x.finished) outcome = hit ? `的中・払戻 ${yen(hit.return_yen)}` : '不的中';
    return `<div class="purchase-item${hit ? ' hit' : ''}">
      <span><b>${esc(item.label || '')}</b><small>${esc(item.text || '')}</small></span>
      <strong>${yen(item.amount_yen)}</strong>
      <em>${outcome}</em></div>`;
  }).join('');
  return `<div class="purchase-items">${rows || '<p>買い目はありません</p>'}</div>`;
}

function purchaseItemsHtml(x) {
  const settlement = x.settlement || {};
  const returned = Number(settlement.returned_yen || 0);
  const invested = Number(settlement.invested_yen || 0);
  const profit = returned - invested;
  const timing = x.registered_before_start === false
    ? '<p class="purchase-late-note">締切後に追加した記録です。利用者別の発走前登録成績には含めません。</p>' : '';
  const settled = x.status === 'purchased' && x.finished;
  return `<div class="settlement-breakdown">
      <div><span>購入額</span><b>${yen(invested)}</b></div>
      <div><span>払戻</span><b>${settled ? yen(returned) : '集計対象外'}</b></div>
      <div><span>収支</span><b class="${profit > 0 ? 'plus' : profit < 0 ? 'minus' : ''}">${settled ? `${profit > 0 ? '+' : ''}${yen(profit)}` : '—'}</b></div>
    </div>${ticketRowsHtml(x)}${timing}
    <div class="purchase-record-actions"><button type="button" data-purchase-delete="${esc(x.id || '')}">この購入記録を削除</button></div>`;
}

function applyPurchaseFilter(box, key) {
  const allowed = ['focus', 'all', 'hit', 'miss', 'unconfirmed'];
  state.purchaseFilter = allowed.includes(key) ? key : 'focus';
  const rows = Array.from(box.querySelectorAll('.purchase-row'));
  let visible = 0;
  rows.forEach((row) => {
    const status = row.dataset.status;
    const show = state.purchaseFilter === 'all'
      || (state.purchaseFilter === 'focus'
        && (status === 'hit' || status === 'unconfirmed' || status === 'waiting'
            || row.dataset.recent === '1'))
      || status === state.purchaseFilter;
    row.hidden = !show;
    if (show) visible += 1;
  });
  box.querySelectorAll('.purchase-filter button').forEach((button) => {
    const on = button.dataset.filter === state.purchaseFilter;
    button.classList.toggle('on', on);
    button.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
  const count = box.querySelector('.purchase-visible-count');
  if (count) count.textContent = `${visible}/${rows.length}件表示`;
}

function bindPurchaseFilters(box) {
  box.querySelectorAll('.purchase-filter button').forEach((button) => {
    button.addEventListener('click', () => applyPurchaseFilter(box, button.dataset.filter));
  });
  applyPurchaseFilter(box, state.purchaseFilter);
}

function renderPurchaseSummary(d) {
  const box = $('#purchaseSummary');
  const listBox = $('#purchaseList');
  if (!box || !listBox) return;
  if (d.range === 'today' && d.date) $('#hdrDate').textContent = formatDate(d.date);
  const confirmed = (d.entries || []).filter((x) => x.status === 'purchased');
  const drafts = (d.entries || []).filter((x) => x.status !== 'purchased');
  if (!confirmed.length && !drafts.length) {
    box.innerHTML = `<div class="purchase-hero"><div><b>${esc(d.range_label || '今日')}の購入記録はありません</b>
      <span>期間を切り替えると過去の記録を確認できます。QR作成後に「購入済みにする」と、買い目・金額・回収率を記録します。</span>
      ${d.range === 'today' ? '<button type="button" class="quiet-btn board-empty-action" data-performance-shortcut="previous">前回開催の成績を見る</button>' : ''}</div></div>`;
    listBox.innerHTML = '';
    box.dataset.ready = '1';
    listBox.dataset.ready = '1';
    const shortcut = box.querySelector('[data-performance-shortcut]');
    if (shortcut) shortcut.addEventListener('click', () => selectPerformanceRange(shortcut.dataset.performanceShortcut));
    return;
  }
  const settled = confirmed.filter((x) => x.finished);
  const hitRaces = settled.filter((x) => (x.settlement || {}).hit_points > 0).length;
  const roi = d.roi == null ? '結果待ち' : pct(d.roi);
  const profit = Number(d.profit_yen || 0);
  const profitText = `${profit > 0 ? '+' : ''}${yen(profit)}`;
  const unconfirmedYen = d.unconfirmed_yen == null
    ? drafts.reduce((sum, row) => sum + Number((row.settlement || {}).invested_yen || 0), 0)
    : Number(d.unconfirmed_yen);
  const rows = (d.entries || []).slice().reverse().map((x, index) => {
    const s = x.settlement || {};
    const status = x.status === 'purchased'
      ? (x.registered_before_start === false ? '購入済み・後から記録' : '購入済み')
      : 'QR作成済み・購入未確認';
    const rowStatus = x.status !== 'purchased' ? 'unconfirmed'
      : (!x.finished ? 'waiting' : (s.hit_points ? 'hit' : 'miss'));
    const result = x.status === 'purchased' && x.finished
      ? `払戻 ${yen(s.returned_yen)}${s.hit_points ? `・${s.hit_points}点的中` : '・不的中'}`
      : (x.finished ? '購入確認なし' : '結果待ち');
    const race = state.races.find((item) => item.race_id === x.race_id) || {};
    const trackLabel = x.track_label || race.track_label || '';
    const startTime = x.start_time || race.start_time || '';
    const track = trackLabel
      ? `<span class="track-patch">${esc(trackLabel)}</span>` : '';
    const time = startTime ? `<span class="purchase-time num">${esc(startTime)}</span>` : '';
    const raceDate = (d.dates || []).length > 1 && x.date
      ? `<span class="purchase-date">${esc(formatDate(x.date))}</span>` : '';
    return `<details class="purchase-row ${rowStatus}" data-status="${rowStatus}" data-recent="${index < 3 ? '1' : '0'}"><summary>
      <span><span class="purchase-race-meta">${raceDate}${track}${time}</span>
        <b>${Number(x.race_num)}R ${esc(x.race_name || '')}</b><small>${esc(status)}</small></span>
      <strong>${result}</strong></summary><div>${purchaseItemsHtml(x)}</div></details>`;
  }).join('');
  const updated = isoHM(d.updated_at);
  box.innerHTML = `<section class="purchase-summary">
    <div class="section-label">${esc(d.range_label || '今日')}・あなたの購入確認済み成績</div>
    <div class="card purchase-totals performance-totals">
      <div><span>購入額</span><b>${yen(d.purchased_yen)}</b></div>
      <div><span>確定払戻</span><b>${yen(d.returned_yen)}</b></div>
      <div><span>確定収支</span><b class="${profit > 0 ? 'plus' : profit < 0 ? 'minus' : ''}">${profitText}</b></div>
      <div><span>回収率</span><b>${roi}</b></div>
      <div><span>的中率</span><b>${settled.length ? pct(hitRaces / settled.length) : '結果待ち'}</b></div>
      <div><span>確定レース</span><b>${Number(d.settled_races || 0)}レース</b></div>
    </div>
    <p class="purchase-basis">購入確認済み ${Number(d.confirmed_races || 0)}件
      ${drafts.length ? `・未確認 ${Number(d.unconfirmed_races || drafts.length)}件（${yen(unconfirmedYen)}）を集計から除外` : ''}
      ${updated ? `<span class="purchase-updated">・${updated}更新</span>` : ''}</p></section>`;
  listBox.innerHTML = `<section class="purchase-list-section">
    <div class="purchase-list-head"><div class="section-label">あなたの購入記録（1記録ごとの内訳）</div>
      <span class="purchase-visible-count"></span></div>
    <p class="purchase-list-note">現在ログイン中の利用者の記録です。上の「利用者別」は、全利用者が発走前にQRへ登録した最後の買い目をレース単位で集計しています。</p>
    <div class="purchase-filter" role="group" aria-label="レース明細の絞り込み">
      <button type="button" data-filter="focus">最新・要確認</button>
      <button type="button" data-filter="all">すべて</button>
      <button type="button" data-filter="hit">的中</button>
      <button type="button" data-filter="miss">不的中</button>
      <button type="button" data-filter="unconfirmed">購入未確認</button>
    </div>${rows}</section>`;
  box.dataset.ready = '1';
  listBox.dataset.ready = '1';
  bindPurchaseFilters(listBox);
  listBox.querySelectorAll('[data-purchase-delete]').forEach((button) => {
    button.addEventListener('click', async () => {
      if (!window.confirm('この購入記録を削除しますか？ 購入額・払戻・回収率の集計からも外れます。')) return;
      button.disabled = true;
      try {
        await postJSON('/api/purchases/delete', {purchase_id: button.dataset.purchaseDelete});
        toast('購入記録を削除しました');
        await loadPurchaseBoard(state.performanceRange);
        await loadUserBoard(state.performanceRange);
      } catch (err) {
        button.disabled = false;
        toast('購入記録を削除できませんでした');
      }
    });
  });
}

function finishedResultRows(p) {
  const byNum = new Map((p.marks || []).map((m) => [String(m.horse_num), m]));
  const pickupByNum = ((p.result_pickup_analysis || {}).horses || {});
  return (p.result || []).map((r) => {
    const m = byNum.get(String(r.horse_num));
    const pickup = pickupByNum[String(r.horse_num)] || null;
    return `<div class="res-row"><span class="o">${esc(r.order)}着</span>
      <span class="n"><b>${esc(r.horse_num)} ${esc(r.horse_name || '')}</b>
        ${m ? `<span class="res-mark">予想時 ${esc(m.mark || '無印')}・評価${Number(m.rank)}位</span>` : ''}
        ${resultFactorSummary(m, pickup)}</span></div>`;
  }).join('');
}

function resultReviewAiBlock(ai) {
  if (!ai || !(ai.items || []).length) {
    return `<section class="result-ai-answer unavailable">
      <span class="result-ai-kicker">振り返り用マイAI</span>
      <h3>項目の組み合わせを特定できませんでした</h3>
      <p>発走前データだけでは、上位3頭を評価できる組み合わせがありませんでした。</p></section>`;
  }
  const items = ai.items.map((item, index) => `<li>
    <span class="result-ai-number">${index + 1}</span><span><small>${esc(item.section || '選択項目')}</small>
      <b>${esc(item.label || '')}</b></span></li>`).join('');
  const placed = (ai.placed || []).map((horse) => `<li>
    <span>${Number(horse.order)}着 ${esc(horse.horse_num || '')} ${esc(horse.horse_name || '')}</span>
    <b>${horse.ai_rank ? `評価${Number(horse.ai_rank)}位${horse.ai_mark ? `・${esc(horse.ai_mark)}` : '・無印'}` : '評価外'}</b></li>`).join('');
  return `<section class="result-ai-answer">
    <span class="result-ai-kicker">振り返り用・結果から逆算</span>
    <h3>このレースで選べばよかった項目</h3>
    <p>次の${ai.items.length}項目を組み合わせたマイAIなら、上位3頭のうち
      <strong>${Number(ai.placed_in_marks)}/${Number(ai.n_placed)}</strong>頭が印圏内でした。</p>
    <ol class="result-ai-items">${items}</ol>
    <ul class="result-ai-outcome">${placed}</ul>
    <p class="result-ai-caution">結果確定後に、発走前データだけを使って組み合わせを再評価した振り返りです。将来のレースで同じ結果を保証するものではありません。</p>
  </section>`;
}

function resultFactorLabel(c) {
  const value = c && c.value_text ? `（${esc(c.value_text)}）` : '';
  return `${esc(c && c.label || '項目名不明')}${value}`;
}

function pickupRankText(c) {
  const to = Number(c && c.to_rank);
  return c && c.from_rank
    ? `評価${Number(c.from_rank)}位→${to}位`
    : `評価${to}位`;
}

function pickupSummary(pickup) {
  if (!pickup) return '';
  if (pickup.status === 'already_marked') {
    return `<div class="res-why pickup"><b>拾えた状態:</b> 現在のマイAIですでに印圏内（評価${Number(pickup.base_rank)}位）</div>`;
  }
  const candidates = pickup.candidates || [];
  if (!candidates.length) {
    return '<div class="res-why neutral"><b>拾う候補:</b> 1項目の追加では印圏内へ上げられる候補がありませんでした</div>';
  }
  const reaches = candidates.filter((c) => c.reaches_marks);
  const shown = reaches.length ? reaches : candidates;
  const labels = shown.map((c) => `${esc(c.label)}（${pickupRankText(c)}${c.reaches_marks ? '' : '・印圏外'}）`).join('・');
  if (reaches.length) {
    const head = pickup.base_rank ? '印圏内へ上げる候補' : 'この項目だけなら印圏内';
    return `<div class="res-why pickup"><b>${head}:</b> ${labels}</div>`;
  }
  const head = pickup.base_rank ? '最も順位を上げる候補' : '単独で最も上位になる項目';
  return `<div class="res-why neutral"><b>${head}:</b> ${labels}</div>`;
}

function resultFactorSummary(mark, pickup) {
  if (!mark && !pickup) {
    return '<div class="res-why unavailable"><b>項目別評価:</b> マイAI未選択のため表示できません</div>';
  }
  const available = ((mark && mark.contributions) || []).filter((c) => c.available);
  const positive = available.filter((c) => Number(c.contribution) > 0)
    .sort((a, b) => Number(b.contribution) - Number(a.contribution)).slice(0, 3);
  const negative = available.filter((c) => Number(c.contribution) < 0)
    .sort((a, b) => Number(a.contribution) - Number(b.contribution)).slice(0, 2);
  const up = positive.length
    ? `<div class="res-why up"><b>現在のAIで評価を上げた項目:</b> ${positive.map(resultFactorLabel).join('・')}</div>`
    : '';
  const down = negative.length
    ? `<div class="res-why down"><b>評価を下げた主な項目:</b> ${negative.map(resultFactorLabel).join('・')}</div>`
    : '';
  return up + pickupSummary(pickup) + down;
}

function payoutsBlock(rows) {
  if (!rows.length) return '<p class="f-note">払戻はまだ取り込まれていません。</p>';
  return `<div class="result-payout"><div class="f-title">確定払戻（100円あたり）</div>
    ${rows.map((x) => `<div class="pay-row"><b>${esc(x.label)}</b>
      <span class="num">${esc(x.text)}</span><strong>${yen(x.payout_yen_per_100)}</strong>
      ${x.popularity ? `<small>${Number(x.popularity)}番人気</small>` : ''}</div>`).join('')}</div>`;
}

/* ------------------------------------------------------- 共有運用の招待認証 */
async function startApp() {
  let status;
  try { status = await getJSON('/api/auth/status'); }
  catch (err) {
    showAuthMessage('サーバーへ接続できません', '管理者へ稼働状況を確認してください。');
    return;
  }
  if (!status.enabled) { init(); return; }
  state.auth = status.user || null;
  const params = new URLSearchParams(location.search);
  if (status.authenticated) {
    scheduleAuthExpiry(status.server_now);
    if (params.get('admin') === '1' && status.user.role === 'admin') {
      renderAdmin();
      return;
    }
    if (params.has('invite')) {
      history.replaceState({}, '', `${location.pathname}${location.hash || '#races'}`);
    }
    enterApp();
    return;
  }
  if (params.get('admin') === '1') renderAdminLogin();
  else if (params.get('invite')) renderInvite(params.get('invite'));
  else showAuthMessage('招待QRが必要です',
    '管理者から受け取った招待QRを読み取ってください。', true);
}

function showAuth(html) {
  $('#authPanel').classList.remove('admin-card');
  $('#authPanel').innerHTML = html;
  $('#authGate').hidden = false;
}
function showAuthMessage(title, message, adminLink = false) {
  showAuth(`<div class="auth-logo">M<em>·AI·</em>Builder</div>
    <h2>${esc(title)}</h2><p>${esc(message)}</p>
    ${adminLink ? '<a class="quiet-btn auth-admin-link" href="?admin=1">管理者ログイン</a>' : ''}`);
}
function lockSharedAccess() {
  if (state.polling) { clearInterval(state.polling); state.polling = null; }
  stopScreenPolling();
  if (state.authExpiryTimer) { clearTimeout(state.authExpiryTimer); state.authExpiryTimer = null; }
  state.auth = null;
  const pill = $('#userPill');
  if (pill) pill.hidden = true;
  showAuthMessage('利用期限が終了しました',
    '利用を続けるには、管理者から新しい招待QRを受け取ってください。', true);
}
function scheduleAuthExpiry(serverNow) {
  if (state.authExpiryTimer) clearTimeout(state.authExpiryTimer);
  state.authExpiryTimer = null;
  if (!state.auth || !state.auth.expires_at || !serverNow) return;
  const remainingMs = (Number(state.auth.expires_at) - Number(serverNow)) * 1000;
  if (remainingMs <= 0) { lockSharedAccess(); return; }
  // setTimeout の上限を超える長期セッションは途中でサーバへ再確認する。
  const waitMs = Math.min(remainingMs + 250, 2147480000);
  state.authExpiryTimer = setTimeout(async () => {
    let status;
    try { status = await getJSON('/api/auth/status'); }
    catch (err) { lockSharedAccess(); return; }
    if (!status.authenticated) { lockSharedAccess(); return; }
    state.auth = status.user;
    scheduleAuthExpiry(status.server_now);
  }, waitMs);
}
function enterApp() {
  $('#authGate').hidden = true;
  const pill = $('#userPill');
  if (pill && state.auth) {
    pill.textContent = state.auth.display_name;
    pill.hidden = false;
    pill.title = state.auth.role === 'admin' ? '共有管理を開く' : 'タップでログアウト';
    pill.addEventListener('click', state.auth.role === 'admin'
      ? () => { location.href = `${location.pathname}?admin=1`; }
      : logoutAuth, {once: true});
  }
  init();
}
function renderInvite(token) {
  showAuth(`<div class="auth-logo">M<em>·AI·</em>Builder</div>
    <h2>招待を受け取りました</h2>
    <p>画面に表示する名前を入力すると利用を開始できます。</p>
    <form id="inviteForm" class="auth-form">
      <label>表示名<input id="inviteName" maxlength="40" autocomplete="name" required></label>
      <button class="cta" type="submit">利用を開始する</button>
      <p class="auth-error" id="authError"></p>
    </form>`);
  $('#inviteForm').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    try {
      const got = await postJSON('/api/auth/redeem',
        {token, display_name: $('#inviteName').value});
      state.auth = got.user;
      scheduleAuthExpiry(got.server_now);
      const clean = `${location.pathname}${location.hash || '#races'}`;
      history.replaceState({}, '', clean);
      enterApp();
    } catch (err) {
      $('#authError').textContent = (err.body && err.body.message) || '招待を確認できませんでした';
    }
  });
}
function renderAdminLogin() {
  showAuth(`<div class="auth-logo">M<em>·AI·</em>Builder</div>
    <h2>管理者ログイン</h2>
    <form id="adminLogin" class="auth-form">
      <label>管理者パスワード<input id="adminPassword" type="password" required></label>
      <button class="cta" type="submit">管理画面を開く</button>
      <p class="auth-error" id="authError"></p>
    </form>`);
  $('#adminLogin').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    try {
      const got = await postJSON('/api/auth/admin-login', {password: $('#adminPassword').value});
      state.auth = got.user; scheduleAuthExpiry(got.server_now); renderAdmin();
    } catch (err) {
      $('#authError').textContent = (err.body && err.body.message) || 'ログインできませんでした';
    }
  });
}
async function renderAdmin() {
  showAuth(`<div class="admin-head"><div><div class="auth-logo">M<em>·AI·</em>Builder</div>
      <h2>共有管理</h2></div><div class="admin-actions">
      <button id="openMainApp" class="quiet-btn">MAIBuilderを使う</button>
      <button id="adminLogout" class="quiet-btn">ログアウト</button></div></div>
    <p class="admin-guide">この画面をスマホで開いたまま、発行したQRをほかの利用者に読み取ってもらえます。</p>
    <form id="inviteCreate" class="admin-create">
      <label>有効時間<input id="inviteHours" class="num" type="number" min="1" max="720" value="24"> 時間</label>
      <label>利用可能人数<input id="inviteUses" class="num" type="number" min="1" max="1000" value="1"> 人</label>
      <button class="cta" type="submit">招待QRを発行</button>
    </form>
    <div id="inviteResult"></div><div id="adminLists"></div>`);
  $('#authPanel').classList.add('admin-card');
  $('#adminLogout').addEventListener('click', logoutAuth);
  $('#openMainApp').addEventListener('click', () => {
    history.replaceState({}, '', `${location.pathname}${location.hash || '#races'}`);
    enterApp();
  });
  $('#inviteCreate').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    try {
      const got = await postJSON('/api/admin/invites', {
        expires_hours: Number($('#inviteHours').value), max_uses: Number($('#inviteUses').value)});
      $('#inviteResult').innerHTML = `<div class="invite-result"><h3>招待QR</h3>
        <img src="${esc(got.qr_png)}" alt="MAIBuilder招待用QR">
        <p>利用期限 ${esc(adminDate(got.expires_at))}・最大${got.max_uses}人</p>
        <p class="note">この期限になると、このQRから参加した利用者は自動的に利用できなくなります。</p>
        <div class="invite-actions"><button class="quiet-btn" id="shareInvite">招待URLを共有</button>
        <button class="quiet-btn" id="copyInvite">招待URLをコピー</button></div></div>`;
      $('#copyInvite').addEventListener('click', async () => {
        try { await navigator.clipboard.writeText(got.url); toast('招待URLをコピーしました'); }
        catch (err) { toast('招待URLをコピーできませんでした'); }
      });
      $('#shareInvite').addEventListener('click', async () => {
        if (!navigator.share) {
          try { await navigator.clipboard.writeText(got.url); toast('招待URLをコピーしました'); }
          catch (err) { toast('この端末では共有できませんでした'); }
          return;
        }
        try { await navigator.share({title:'MAIBuilder 招待', url:got.url}); }
        catch (err) { /* 共有画面を閉じた場合は何もしない */ }
      });
      await loadAdminLists();
    } catch (err) { toast((err.body && err.body.message) || '招待QRを発行できませんでした'); }
  });
  await loadAdminLists();
}
async function loadAdminLists() {
  let invites, users;
  try {
    [invites, users] = await Promise.all([
      getJSON('/api/admin/invites'), getJSON('/api/admin/users')]);
  } catch (err) { return; }
  const allUsers = users.users || [];
  const activeUsers = allUsers.filter((user) => (user.effective_status || user.status) === 'active');
  const inactiveUsers = allUsers.filter((user) => (user.effective_status || user.status) !== 'active');
  const allInvites = invites.invites || [];
  const activeInvites = allInvites.filter((invite) => !invite.revoked && !invite.expired
    && Number(invite.uses || 0) < Number(invite.max_uses || 0));
  const inviteHistory = allInvites.filter((invite) => !activeInvites.includes(invite));
  const inviteRow = (x) => `<div class="admin-row"><span>${adminDate(x.created_at)}
      <small>${x.uses}/${x.max_uses}人・利用期限 ${adminDate(x.expires_at)}</small></span>
      ${x.revoked ? '<small>無効</small>' : x.expired ? '<small>期限切れ</small>'
        : Number(x.uses || 0) >= Number(x.max_uses || 0) ? '<small>利用済み</small>'
        : `<button class="quiet-btn" data-revoke="${esc(x.id)}">無効化</button>`}</div>`;
  $('#adminLists').innerHTML = `<div class="admin-summary" aria-label="共有状況">
      <div><b>${activeUsers.length}</b><span>利用中</span></div>
      <div><b>${inactiveUsers.length}</b><span>停止・期限切れ</span></div>
      <div><b>${activeInvites.length}</b><span>有効な招待</span></div>
    </div><div class="admin-lists-grid">
    <section class="admin-list"><h3>利用中 ${activeUsers.length}人</h3>
      ${activeUsers.map(adminUserRow).join('') || '<p>現在利用中のユーザーはいません。</p>'}
      ${inactiveUsers.length ? `<details class="admin-collapsible"><summary>停止・期限切れ ${inactiveUsers.length}人</summary>
        ${inactiveUsers.map(adminUserRow).join('')}</details>` : ''}
    </section><section class="admin-list"><h3>有効な招待 ${activeInvites.length}件</h3>
      ${activeInvites.map(inviteRow).join('') || '<p>現在有効な招待はありません。</p>'}
      ${inviteHistory.length ? `<details class="admin-collapsible"><summary>過去の発行履歴 ${inviteHistory.length}件</summary>
        ${inviteHistory.map(inviteRow).join('')}</details>` : ''}
    </section></div>`;
  $$('#adminLists [data-user]').forEach((b) => b.addEventListener('click', async () => {
    await postJSON('/api/admin/users/status', {user_id:b.dataset.user, status:b.dataset.status});
    loadAdminLists();
  }));
  $$('#adminLists [data-revoke]').forEach((b) => b.addEventListener('click', async () => {
    await postJSON('/api/admin/invites/revoke', {invite_id:b.dataset.revoke}); loadAdminLists();
  }));
}
function adminUserRow(u) {
  const effective = u.effective_status || u.status;
  let label;
  if (!u.access_expires_at) label = '再招待が必要です';
  else if (effective === 'expired') label = '期限切れ';
  else if (effective === 'revoked') label = '招待が無効化されました';
  else if (effective === 'disabled') label = `停止中・期限 ${adminDate(u.access_expires_at)}`;
  else label = `利用中・期限 ${adminDate(u.access_expires_at)}`;
  const canToggle = u.access_expires_at && !u.access_expired && !u.invite_revoked;
  const action = !canToggle ? '' : `<button class="quiet-btn" data-user="${esc(u.id)}"
    data-status="${u.status === 'active' ? 'disabled' : 'active'}">
    ${u.status === 'active' ? '停止' : '再開'}</button>`;
  return `<div class="admin-row"><span><b>${esc(u.display_name)}</b>
    <small>${esc(label)}</small><small>登録 ${adminDate(u.created_at)}・ID ${esc(String(u.id || '').slice(-6))}</small></span>${action}</div>`;
}
function adminDate(epoch) {
  if (!epoch) return '—';
  return new Date(Number(epoch) * 1000).toLocaleString('ja-JP',
    {month:'numeric', day:'numeric', hour:'2-digit', minute:'2-digit'});
}
async function logoutAuth() {
  const wasAdmin = state.auth && state.auth.role === 'admin';
  try { await postJSON('/api/auth/logout', {}); } catch (err) { /* セッション破棄を優先 */ }
  location.href = wasAdmin ? `${location.pathname}?admin=1` : location.pathname;
}

/* C-5: 初回導線が「レース一覧 → レース選択 → まだマイAIがありません → 作成」の
 * 3ホップだった。設定が1件も無いことは一覧を開いた時点で分かるので、
 * その場で作成へ誘導する (レースを選ばせてから空を告げるのをやめる)。 */
async function maybeInviteFirstRun() {
  let list;
  try { list = await getJSON('/api/configs'); } catch (e) { return; }
  const n = (list && list.configs || []).length;
  const box = $('#firstRun');
  if (!box) return;
  if (n > 0) { box.remove(); return; }
  box.innerHTML = `<div class="fr">
    <div class="fr-t">まずマイAIをつくります</div>
    <div class="fr-d">重視したい項目を選ぶと、レースごとに印(◎○▲△×)が出ます。所要2〜3分です。</div>
    <button class="cta" data-go="build">マイAIをつくる</button>
  </div>`;
  box.querySelector('[data-go]').addEventListener('click', () => go('build'));
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
    syncHeaderHeight();
  }
  if (!v.stale) return;
  const el = $('#racesWarn');
  if (el) el.innerHTML = `<p class="note"><span class="chip muted">一部更新待ち</span>
    項目別振り返りは現在利用できます。その他の更新は次回再起動時に反映されます。</p>`;
}

/* リロード後の復元。タブを閉じるとsessionStorageは消えるため、IDが無い場合は
 * サーバに保存済みの最新AIを選ぶ。設定本体の正本は引き続きサーバだけに置く。 */
async function restoreConfig() {
  let id = null;
  try { id = sessionStorage.getItem(CFG_ID_KEY); } catch (e) { id = null; }
  if (!state.features) return;
  if (!id) {
    try {
      const list = (await getJSON('/api/configs')).configs || [];
      id = list.length ? list[0].id : null;
    } catch (e) { id = null; }
  }
  if (!id) return;
  let got;
  try { got = await getJSON(`/api/configs/${encodeURIComponent(id)}`); }
  catch (e) {
    // 保管などで前回IDが無効なら、残っている最新AIへ一度だけフォールバック。
    try {
      const list = (await getJSON('/api/configs')).configs || [];
      if (!list.length || list[0].id === id) return;
      got = await getJSON(`/api/configs/${encodeURIComponent(list[0].id)}`);
    } catch (e2) { return; }
  }
  if (!got || !got.config) return;
  state.config = { id: got.id, name: got.name, version: got.version };
  try { sessionStorage.setItem(CFG_ID_KEY, got.id); } catch (e) { /* 無視 */ }
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
    // C-2: 期間のトグルも checkbox になったので、項目トグルを明示的に選ぶ
    item.querySelector('input[type=checkbox]:not([data-kind])').checked = on;
    syncCells(item.dataset.metric);
  });
  // C-1: 詳細設定を使っている設定を読み込んだら、閉じたままにせず開く
  const advBody = $('#advBody');
  if (advBody && sel.step2.size > 0 && advBody.hidden) {
    advBody.hidden = false;
    const head = $('#advHead');
    head.setAttribute('aria-expanded', 'true');
    head.querySelector('.adv-caret').textContent = '\u25b4';
  }
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

document.addEventListener('DOMContentLoaded', startApp);

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
  try { list = (await getJSON('/api/configs?include_archived=1')).configs || []; }
  catch (e) { list = []; }
  if (!list.length) {
    openSheet('マイAIの切替', '<p>保存済みのマイAIがありません。</p>');
    return;
  }
  const active = list.filter((c) => !c.archived);
  const archived = list.filter((c) => c.archived);
  const current = active.find((c) => state.config && c.id === state.config.id) || null;
  const others = active.filter((c) => !current || c.id !== current.id);
  const switchOptions = others.map((c) => `<option value="${esc(c.id)}">
    ${esc(c.name)}（${Number(c.n_items || 0)}項目・v${Number(c.version || 1)}）</option>`).join('');
  const currentCard = current ? `<div class="ai-current-card">
      <span>現在使用中</span><b>${esc(current.name)}</b>
      <small>${Number(current.n_items || 0)}項目・v${Number(current.version || 1)}</small></div>`
    : '<div class="ai-current-card"><span>現在使用中</span><b>未選択</b></div>';
  const switcher = switchOptions ? `<div class="ai-switcher">
      <label for="aiSwitchSelect">ほかのマイAIに切り替える</label>
      <select id="aiSwitchSelect"><option value="">マイAIを選択</option>${switchOptions}</select>
      <button class="cta" id="aiSwitchApply" type="button" disabled>このAIで予想する</button></div>`
    : '<p class="ai-switch-empty">切り替えられるほかのマイAIはありません。</p>';
  const activeRows = active.map((c) => {
    const on = state.config && c.id === state.config.id;
    return `<div class="ai-manage-row"><div class="ai-row${on ? ' on' : ''}">
      <span class="an">${esc(c.name)}</span>
      <span class="am">${c.n_items}項目 · v${c.version}</span>
      ${on ? '<span class="ac">適用中</span>' : ''}</div>
      <div class="ai-tools"><button class="quiet-btn" data-rename="${esc(c.id)}"
        data-name="${esc(c.name)}">名前変更</button>
      <button class="quiet-btn" data-archive="${esc(c.id)}">保管</button></div></div>`;
  }).join('');
  const archivedRows = archived.map((c) => `<div class="ai-manage-row archived">
    <div class="ai-row"><span class="an">${esc(c.name)}</span>
      <span class="am">${c.n_items}項目 · v${c.version}</span></div>
    <div class="ai-tools"><button class="quiet-btn" data-restore="${esc(c.id)}">戻す</button></div>
    </div>`).join('');
  const management = (activeRows || '<p>使用中のマイAIはありません。</p>')
    + (archivedRows ? `<div class="section-label">保管済み</div>${archivedRows}` : '');
  openSheet('このレースに使うマイAI', currentCard + switcher
    + `<details class="ai-manage-details"><summary>名前変更・保管</summary>${management}</details>`
    + '<button class="quiet-btn ai-build-link" id="openAiBuilder" type="button">マイAI画面で作成・編集</button>'
    + '<p class="sheet-foot">レースごとに使用するAIを選べます。選択したAIはこのレースに記憶されます。</p>');
  const switchSelect = $('#aiSwitchSelect');
  const switchApply = $('#aiSwitchApply');
  if (switchSelect && switchApply) {
    switchSelect.addEventListener('change', () => { switchApply.disabled = !switchSelect.value; });
    switchApply.addEventListener('click', async () => {
      if (!switchSelect.value) return;
      $('#sheet').classList.remove('show');
      await applyConfigId(switchSelect.value, true);
    });
  }
  $('#openAiBuilder').addEventListener('click', () => {
    $('#sheet').classList.remove('show');
    go('build');
  });
  $$('#sheetBody [data-rename]').forEach((b) => b.addEventListener('click', () =>
    openRenameAi(b.dataset.rename, b.dataset.name)));
  $$('#sheetBody [data-archive]').forEach((b) => b.addEventListener('click', async () => {
    await postJSON('/api/configs/archive', {config_id:b.dataset.archive, archived:true});
    if (state.config && state.config.id === b.dataset.archive) {
      state.config = null;
      try { sessionStorage.removeItem(CFG_ID_KEY); } catch (e) { /* 無視 */ }
    }
    toast('マイAIを保管しました');
    openAiSheet();
  }));
  $$('#sheetBody [data-restore]').forEach((b) => b.addEventListener('click', async () => {
    await postJSON('/api/configs/archive', {config_id:b.dataset.restore, archived:false});
    toast('マイAIを一覧へ戻しました');
    openAiSheet();
  }));
}

function openRenameAi(configId, currentName) {
  openSheet('マイAIの名前を変更', `<form id="renameAiForm" class="auth-form">
    <label>名前<input id="renameAiName" maxlength="40" value="${esc(currentName)}" required></label>
    <button class="cta" type="submit">名前を変更</button></form>`);
  $('#renameAiForm').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const got = await postJSON('/api/configs/rename', {
      config_id:configId, name:$('#renameAiName').value,
    });
    if (state.config && state.config.id === configId) state.config.name = got.name;
    toast('名前を変更しました');
    openAiSheet();
  });
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
    return '';
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

/* ---------------------------------------- 買い目・JRAスマッピーQR */
/* ============================ 買い目 (参加者が自分で組む) =================
 *
 * 金額を含む買い目を MAIBuilder のサーバからJRA公式スマッピーへ送り、
 * JRAが返した正式な投票用データだけをQR画像にする。投票データは推測しない。
 * 調査・安全条件: docs/SMAPPY_QR_PLAN.md
 *
 * 流れは公式サイトとマークカードと同じ順序:
 *   券種を選ぶ → 買い方を選ぶ → 馬(枠)を選ぶ → 買い目に追加
 * 追加した買い目は溜まっていく (1レースで複数券種を買うのが普通)。
 *
 * **段数・見出し・組み合わせ・点数はすべてサーバが決める。** UI は
 * `/api/features` の券種仕様と `/api/betslip` の結果を出すだけ。
 * 馬単の「11→10」を UI 側で組み立て直して方向を落とした事故があり、
 * それは公式サイトへ手入力する経路そのものだった。数え間違いも同じ経路。
 * 金額は1点あたり100円単位。 */

/* 予想を描くたびに呼ぶ。同じレースなら参加者が組んだものを維持する。 */
function initBet(p, options = {}) {
  const recordOnly = Boolean(options.recordOnly);
  if (bet.raceId === p.race_id) {
    bet.recordOnly = recordOnly;
    return;      // オッズ更新では作り直さない
  }
  bet.raceId = p.race_id;
  bet.recordOnly = recordOnly;
  // 印から作れる買い目は候補として保持するが、初期表示には入れない。
  // 予想一覧を開いた直後から20点の買い目が続くと一覧性を損なうため、明示操作で追加する。
  bet.suggested = JSON.parse(JSON.stringify(p.bet_selection || []));
  bet.entries = [];
  bet.dirty = false;
  bet.slip = [];
  bet.skipped = [];
  bet.total = 0; bet.totalYen = 0; bet.text = '';
  bet.budgetYen = null; bet.oddsNote = '';
  bet.qr = null; bet.qrKey = null; bet.qrError = ''; bet.qrPending = false;
  bet.recordPending = false; bet.savedPurchaseId = null; bet.savedKey = null;
  bet.openSlipDetails.clear();
  resetDraft();
}

function betTypeSpec(key) {
  return ((state.features || {}).bet_types || []).find((x) => x.key === key) || null;
}

/* AI印が無いレースでは、評価順を装わずサーバの出走馬一覧を使う。 */
function runnerRows(p) {
  return (p.marks || []).length ? (p.marks || []) : (p.runners || []);
}
function draftModeSpec() {
  const t = betTypeSpec(bet.draft.type);
  if (!t) return null;
  return t.modes.find((m) => m.key === bet.draft.mode) || t.modes[0];
}

/* 券種を選び直したら買い方と選択を作り直す (段数が変わるので持ち越せない) */
function resetDraft(typeKey) {
  const types = (state.features || {}).bet_types || [];
  const t = typeKey ? betTypeSpec(typeKey) : (betTypeSpec(bet.draft.type) || types[0]);
  if (!t) { bet.draft = { type: null, mode: null, groups: [] }; return; }
  setDraftMode(t, t.modes[0].key);
}
function setDraftMode(t, modeKey) {
  const m = t.modes.find((x) => x.key === modeKey) || t.modes[0];
  bet.draft = { type: t.key, mode: m.key, groups: m.groups.map(() => []) };
}

function betSlipBlock(p) {
  if (!runnerRows(p).length) return '';
  if (!((state.features || {}).bet_types || []).length) return '';
  if (bet.recordOnly) return `<details class="late-purchase">
    <summary>購入した買い目を後から記録</summary>
    <div class="late-purchase-note">実際に購入した内容だけを入力してください。締切後のためスマッピーQRは作成せず、個人の購入額・払戻・回収率へ記録します。</div>
    <div id="bsDraft"></div><div id="bsResult"></div></details>`;
  return `<div class="section-label">買い目</div>
    <div id="bsDraft"></div>
    <div id="bsResult"></div>`;
}

/* ---- 組み立て中の1件 ------------------------------------------------- */
function draftBlock(p) {
  const f = state.features || {};
  const types = f.bet_types || [];
  const t = betTypeSpec(bet.draft.type);
  if (!t) return '';
  const m = draftModeSpec();
  const tabs = types.map((x) => `<button class="btype${x.key === t.key ? ' on' : ''}"
      data-btype="${esc(x.key)}" aria-pressed="${x.key === t.key}">${esc(x.label)}</button>`).join('');
  const modes = t.modes.map((x) => `<button class="bmode${x.key === m.key ? ' on' : ''}"
      data-bmode="${esc(x.key)}" aria-pressed="${x.key === m.key}">${esc(x.label)}</button>`).join('');
  const groups = m.groups.map((g, i) => groupBlock(p, t, g, i)).join('');
  const n = bet.draft.groups.reduce((a, g) => a + g.length, 0);
  return `<div class="card bed">
    <div class="bed-sec">
      <div class="bed-h">券種</div>
      <div class="btype-row">${tabs}</div>
    </div>
    <div class="bed-sec">
      <div class="bed-h">買い方${m.term ? `<button class="pinfo" data-term="${esc(m.term)}"
        aria-label="${esc(m.label)}の説明">ⓘ</button>` : ''}</div>
      <div class="bmode-row">${modes}</div>
      <p class="bed-note">${esc(m.desc || '')}
        ${t.unit === 'frame' ? esc(f.bet_zoro_note || '') : ''}</p>
    </div>
    ${groups}
    <button class="bed-add" id="bedAdd"${n ? '' : ' disabled'}>この買い目を追加</button>
  </div>`;
}

/* 段ごとの選択欄。**馬番(枠番)順に並べる** — 出馬表も公式の入力も番号順。 */
function groupBlock(p, t, g, i) {
  const picked = bet.draft.groups[i] || [];
  const chips = t.unit === 'frame' ? frameChips(p, picked) : horseChips(p, picked);
  const need = g.exact ? `<span class="bed-n">${g.exact}${t.unit === 'frame' ? '枠' : '頭'}</span>`
    : `<span class="bed-n">${picked.length}${t.unit === 'frame' ? '枠' : '頭'}</span>`;
  return `<div class="bed-sec">
    <div class="bed-h">${esc(g.label)}${need}
      ${g.exact ? '' : `<span class="bed-quick">
        <button type="button" data-quick="marks" data-group="${i}">印</button>
        <button type="button" data-quick="all" data-group="${i}">全</button></span>`}</div>
    <div class="bed-chips" data-group="${i}">${chips}</div>
  </div>`;
}

function quickPicks(p, t, kind) {
  const marked = (p.marks || []).filter((m) => m.mark);
  const rows = kind === 'marks' ? marked : runnerRows(p);
  if (t.unit === 'frame') {
    return [...new Set(rows.filter((m) => m.waku).map((m) => String(m.waku)))]
      .sort((a, b) => Number(a) - Number(b));
  }
  return rows.map((m) => String(m.horse_num))
    .sort((a, b) => Number(a) - Number(b));
}

/* 馬番順に並べる。`p.marks` は **評価順** (◎○▲△×…) なので、そのまま出すと
 * 「3 2 13 1 5 12 11 …」という並びになり、探すのに目で追う必要があった。
 * 公式サイトの入力も出馬表も馬番順なので、そこに合わせる。印は数字の横に残す。 */
function horseChips(p, picked) {
  const rows = runnerRows(p).slice()
    .sort((a, b) => Number(a.horse_num) - Number(b.horse_num));
  return rows.map((m) => {
    const on = picked.includes(m.horse_num);
    const mk = m.mark ? `<span class="bc-m">${esc(m.mark)}</span>` : '';
    return `<button class="bchip${on ? ' on' : ''}" data-num="${esc(m.horse_num)}"
      aria-pressed="${on}"
      aria-label="${esc(m.horse_name || '')} ${Number(m.horse_num)}番">
      <b class="num">${Number(m.horse_num)}</b>${mk}</button>`;
  }).join('');
}

/* 枠連は枠で選ぶ。枠ごとの頭数を添える (2頭以上ならゾロ目が成立する)。 */
function frameChips(p, picked) {
  const counts = new Map();
  runnerRows(p).forEach((m) => {
    if (!m.waku) return;
    counts.set(String(m.waku), (counts.get(String(m.waku)) || 0) + 1);
  });
  return Array.from(counts.keys()).sort((a, b) => Number(a) - Number(b)).map((w) => {
    const on = picked.includes(w);
    return `<button class="bchip${on ? ' on' : ''}" data-num="${esc(w)}"
      aria-pressed="${on}" aria-label="${Number(w)}枠 ${counts.get(w)}頭">
      <b class="num">${Number(w)}</b><span class="bc-m">${counts.get(w)}頭</span></button>`;
  }).join('');
}

/* ---- 追加済みの買い目 ------------------------------------------------ */
function betResultBlock(p) {
  const f = state.features || {};
  const slip = bet.slip || [];
  const skipped = (bet.skipped || []).map((x) =>
    `<div class="bs-skip">${esc(x.label)}は組めませんでした — ${esc(x.reason)}
      <button class="bs-del" data-drop="${x.index}">取り消す</button></div>`).join('');
  if (!slip.length) {
    const suggest = bet.suggested.length
      ? '<button class="bs-suggest" id="bsSuggest">印から買い目を作る</button>' : '';
    return `${skipped}<div class="card bs-start">
      <p class="bs-empty-note">買い目はまだありません。上で券種と馬を選んで追加してください。</p>
      ${suggest}</div>
      <p class="note bs-note">${esc(f.bet_slip_note || '')}</p>`;
  }
  const rows = slip.map((t, i) => `<div class="bs-row">
      <div class="bs-head">
        <span class="bs-k">${esc(t.label)}</span>
        <span class="bs-mode">${esc(t.mode_label)}</span>
        <span class="bs-n">${t.n}点</span>
        <button class="bs-del" data-remove="${i}"
          aria-label="${esc(t.label)}の買い目を削除">✕</button>
      </div>
      ${picksBlock(t)}
      <details class="bs-pts" data-slip="${i}"${bet.openSlipDetails.has(i) ? ' open' : ''}>
        <summary>${t.n}点の内訳 ${oddsSummary(t)}</summary>
        <div class="bs-v">${pointOddsRows(t, i)}</div>
      </details>
      <label class="bs-money">全点を一括変更
        <input class="bs-amount num" data-slip="${i}" type="number" inputmode="numeric"
          min="100" max="999900" step="100" value="${t.amount_yen == null ? '' : Number(t.amount_yen)}"
          placeholder="${t.amount_yen == null ? '点別配分' : ''}"
          aria-label="${esc(t.label)}の全点を同じ金額へ変更"> 円
        <span class="bs-sub">小計 ${yen(t.subtotal_yen)}</span>
      </label>
    </div>`).join('');
  const saveOnly = bet.recordOnly ? `<button class="bs-record" id="bsRecord"
      ${bet.recordPending || (bet.savedPurchaseId && bet.savedKey === betSelectionKey()) ? ' disabled' : ''}>
      ${bet.recordPending ? '保存中…' : (bet.savedPurchaseId && bet.savedKey === betSelectionKey()
        ? '購入済みとして保存しました' : '購入済みの買い目として保存')}</button>` : '';
  return `${skipped}
    <div class="card bs">${rows}
      ${bet.oddsNote ? `<p class="bs-odds-note">${esc(bet.oddsNote)}</p>` : ''}
      ${fundsBlock()}
      <div class="bs-total">合計 <b>${bet.total}点・${yen(bet.totalYen)}</b>
        <button class="bs-clear" id="bsClear">すべて削除</button>
        <button class="bs-reset" id="bsReset">印から作り直す</button></div></div>
    ${handoffBlock(p, slip, bet.total)}
    <div class="bs-actions">
      <button class="bs-copy" id="bsCopy">買い目をコピー</button>
      ${bet.recordOnly ? saveOnly : `<button class="bs-qr" id="bsQr"${bet.qrPending ? ' disabled' : ''}>
        ${bet.qrPending ? 'JRAに送信中…' : 'スマッピーQRを作成'}</button>
      <a class="bs-link" href="https://qrcode.jra.go.jp/" target="_blank"
         rel="noopener noreferrer">JRA公式サイトを開く</a>`}
    </div>
    ${bet.recordOnly ? '' : smappyQrBlock(p)}
    <p class="note bs-note">${esc(f.bet_slip_note || '')}</p>`;
}

function yen(v) {
  return `${Number(v || 0).toLocaleString('ja-JP')}円`;
}

function oddsSummary(t) {
  const all = t.odds || [];
  const available = all.filter((o) => o && o.available);
  if (!available.length) return '<span class="bo-state pending">オッズ未発表</span>';
  if (all.length === 1) return `<span class="bo-state">${esc(oddsText(available[0]))}</span>`;
  return `<span class="bo-state${available.length < all.length ? ' pending' : ''}">
    ${available.length < all.length ? '一部未発表' : 'オッズ表示あり'}</span>`;
}

function oddsText(o) {
  if (!o || !o.available) return 'オッズ未発表';
  const lo = Number(o.low).toFixed(1);
  const hi = o.high != null && Number(o.high) > Number(o.low)
    ? `〜${Number(o.high).toFixed(1)}` : '';
  return `${lo}${hi}倍`;
}

function payoutText(amount, o) {
  if (!o || !o.available) return '';
  const lo = Math.floor(Number(amount) * Number(o.low));
  const hi = o.high != null && Number(o.high) > Number(o.low)
    ? `〜${yen(Math.floor(Number(amount) * Number(o.high)))}` : '';
  return `的中時の概算払戻 ${yen(lo)}${hi}`;
}

function pointAmount(t, i) {
  const amounts = t.amounts_yen || [];
  return Number(amounts[i] != null ? amounts[i] : (t.amount_yen || 100));
}

function pointOddsRows(t, slipIndex) {
  return (t.texts || []).map((txt, i) => {
    const o = (t.odds || [])[i] || {available: false};
    const pop = o.popularity ? `・${Number(o.popularity)}番人気` : '';
    const amount = pointAmount(t, i);
    const payout = payoutText(amount, o);
    return `<div class="bo-row"><b class="num">${esc(txt)}</b>
      <span class="bo-odds${o.available ? '' : ' pending'}">${esc(oddsText(o))}${esc(pop)}</span>
      <label class="bo-stake">賭け金
        <input class="bo-amount num" data-slip="${Number(slipIndex)}" data-point="${i}"
          type="number" inputmode="numeric" min="100" max="999900" step="100"
          value="${amount}" aria-label="${esc(txt)}の賭け金"> 円</label>
      ${payout ? `<span class="bo-pay">${esc(payout)}</span>` : ''}</div>`;
  }).join('');
}

function applyPointAmountEdit(slipIndex, pointIndex, amount) {
  if (!Number.isInteger(amount) || amount < 100 || amount > 999900 || amount % 100) {
    return false;
  }
  const live = liveIndexes();
  const entryIndex = live[Number(slipIndex)];
  const slipEntry = (bet.slip || [])[Number(slipIndex)];
  const entry = (bet.entries || [])[entryIndex];
  if (entryIndex == null || !slipEntry || !entry || pointIndex < 0 ||
      pointIndex >= Number(slipEntry.n)) return false;
  // 一括金額の買い目も、ここで全点分へ展開してから対象の1点だけを変える。
  // これによりオッズ配分後の他点を維持したまま微調整できる。
  const amounts = Array.from({length:Number(slipEntry.n)}, (_, i) => pointAmount(slipEntry, i));
  amounts[Number(pointIndex)] = amount;
  entry.amounts_yen = amounts;
  delete entry.amount_yen;
  bet.dirty = true;
  return true;
}

function slipOddsReady(slip) {
  return !!slip.length && slip.every((t) => {
    const odds = t.odds || [];
    return odds.length === Number(t.n) && odds.every((o) =>
      o && o.available && Number.isFinite(Number(o.low)) && Number(o.low) > 0);
  });
}

/* オッズの逆数で予算を配り、各買い目の概算払戻を近づける。
 * ワイドのような範囲オッズは、過大な見積りを避けるため low を使う。 */
function oddsBudgetAllocation(budget, slip) {
  const points = [];
  const sizes = [];
  slip.forEach((t) => {
    const odds = t.odds || [];
    sizes.push(Number(t.n));
    odds.forEach((o) => points.push(o));
  });
  if (!points.length || points.length !== sizes.reduce((a, b) => a + b, 0)
      || !slipOddsReady(slip)) {
    throw new Error('odds_unavailable');
  }
  const budgetUnits = Math.floor(Number(budget) / 100);
  const targetUnits = Math.min(budgetUnits, points.length * 9999);
  if (targetUnits < points.length) throw new Error('budget_too_small');

  const weights = points.map((o) => 1 / Number(o.low));
  const weightSum = weights.reduce((a, b) => a + b, 0);
  const raw = weights.map((w) => targetUnits * w / weightSum);
  const units = raw.map((x) => Math.max(1, Math.min(9999, Math.floor(x))));
  let used = units.reduce((a, b) => a + b, 0);
  while (used < targetUnits) {
    let best = -1;
    for (let i = 0; i < units.length; i += 1) {
      if (units[i] >= 9999) continue;
      if (best < 0 || raw[i] - units[i] > raw[best] - units[best]) best = i;
    }
    if (best < 0) break;
    units[best] += 1; used += 1;
  }
  while (used > targetUnits) {
    let best = -1;
    for (let i = 0; i < units.length; i += 1) {
      if (units[i] <= 1) continue;
      if (best < 0 || units[i] - raw[i] > units[best] - raw[best]) best = i;
    }
    if (best < 0) break;
    units[best] -= 1; used -= 1;
  }

  const amounts = units.map((x) => x * 100);
  const byEntry = [];
  let at = 0;
  sizes.forEach((n) => { byEntry.push(amounts.slice(at, at + n)); at += n; });
  return {byEntry, usedYen: used * 100, remainingYen: Number(budget) - used * 100};
}

/* オッズがまだ無い時間帯でも、予算を推測値で配らず全点へ均等に割り当てる。 */
function equalBudgetAllocation(budget, slip) {
  const sizes = slip.map((t) => Number(t.n));
  const pointCount = sizes.reduce((a, b) => a + b, 0);
  if (!pointCount) throw new Error('empty_selection');
  const budgetUnits = Math.floor(Number(budget) / 100);
  const targetUnits = Math.min(budgetUnits, pointCount * 9999);
  if (targetUnits < pointCount) throw new Error('budget_too_small');
  const base = Math.floor(targetUnits / pointCount);
  const extra = targetUnits % pointCount;
  const amounts = Array.from({length: pointCount}, (_, i) => (base + (i < extra ? 1 : 0)) * 100);
  const byEntry = [];
  let at = 0;
  sizes.forEach((n) => { byEntry.push(amounts.slice(at, at + n)); at += n; });
  return {byEntry, usedYen: targetUnits * 100,
          remainingYen: Number(budget) - targetUnits * 100};
}

function fundsBlock() {
  const budget = bet.budgetYen == null ? '' : String(bet.budgetYen);
  const ready = slipOddsReady(bet.slip || []);
  const budgetDiff = bet.budgetYen == null ? null : bet.budgetYen - bet.totalYen;
  const left = budgetDiff == null ? '' : (budgetDiff >= 0
    ? `<span class="bf-left">配分後の残り ${yen(budgetDiff)}</span>`
    : `<span class="bf-left over">予算を ${yen(Math.abs(budgetDiff))} 超過</span>`);
  return `<div class="bs-funds">
    <div class="bf-title">予算から資金配分</div>
    <label>予算 <input class="bf-input num" id="bsBudget" type="number" inputmode="numeric"
      min="100" max="1000000" step="100" value="${esc(budget)}" placeholder="例 10000"> 円</label>
    <div class="bf-actions">
      <button class="bf-apply" id="bsAllocate"${ready ? '' : ' disabled'}>オッズで資金配分</button>
      <button class="bf-apply secondary" id="bsAllocateEqual">均等に配分</button>
    </div>
    <p>${ready
      ? 'オッズ配分後は「点数の内訳」を開くと、各買い目の金額を100円単位で調整できます。'
      : 'オッズ発表前は均等配分を利用できます。発表後はオッズ配分へ切り替えられます。'}${left}</p>
  </div>`;
}

function smappyQrBlock(p) {
  if (bet.qrError) return `<div class="smq-error" role="alert">${esc(bet.qrError)}</div>`;
  const q = bet.qr;
  if (!q) return '';
  const made = q.created_at ? isoDateTime(q.created_at) : '作成時刻不明';
  const rows = (q.items || []).map((x) => `<li>
    <b>${esc(x.label)}</b> <span class="num">${esc(x.text)}</span>
    <span>${yen(x.amount_yen)}</span></li>`).join('');
  return `<section class="smq" aria-label="スマッピー投票用QR">
    <h3>JRAスマッピー投票用QR</h3>
    <p class="smq-ok">JRA受付内容と送信内容の一致を確認済み</p>
    <img class="smq-img" src="${esc(q.qr_png)}" alt="JRAスマッピー投票用QRコード">
    <p class="smq-meta">${esc(raceIdentity(p))}・${q.points}点・合計${yen(q.total_yen)}・${esc(made)}</p>
    <details><summary>QRに入れた買い目を確認</summary><ul>${rows}</ul></details>
    <div class="smq-actions">
      <button type="button" id="smqSave">買い目とQRを画像保存</button>
      <button type="button" id="smqConfirm"${q.purchase_status === 'purchased' ? ' disabled' : ''}>
        ${q.purchase_status === 'purchased' ? '購入済みとして記録しました' : '発売機で購入後、購入済みにする'}</button>
    </div>
    <p class="smq-keep">このQRは買い目を変更するまで画面に残ります。</p>
    <p class="smq-warn"><b>発売機で購入内容と合計金額をもう一度確認してください。</b>
      発売機で確定するまで購入は行われません。</p>
  </section>`;
}

function isoDateTime(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(String(s || ''));
  return m ? `${Number(m[1])}/${Number(m[2])}/${Number(m[3])} ${m[4]}:${m[5]}` : String(s || '');
}

async function saveQrWithBets(p) {
  const q = bet.qr;
  if (!q || !q.qr_png) return;
  try {
    const img = new Image();
    img.src = q.qr_png;
    await new Promise((resolve, reject) => {
      img.onload = resolve; img.onerror = reject;
    });
    const items = q.items || [];
    const canvas = document.createElement('canvas');
    canvas.width = 1080;
    canvas.height = Math.max(1320, 1120 + items.length * 54);
    const c = canvas.getContext('2d');
    c.fillStyle = '#fff'; c.fillRect(0, 0, canvas.width, canvas.height);
    c.fillStyle = '#0b4f3c'; c.font = '700 52px sans-serif';
    c.fillText('M·AI·Builder  スマッピーQR', 60, 82);
    c.fillStyle = '#17231f'; c.font = '700 40px sans-serif';
    c.fillText(raceIdentity(p), 60, 145);
    c.drawImage(img, 230, 185, 620, 620);
    c.font = '700 32px sans-serif'; c.fillText(`合計 ${yen(q.total_yen)}・${q.points}点`, 60, 860);
    c.font = '28px sans-serif';
    let y = 920;
    items.forEach((x) => {
      c.fillText(`${x.label}  ${x.text}  ${yen(x.amount_yen)}`, 70, y);
      y += 50;
    });
    c.fillStyle = '#5b675f'; c.font = '24px sans-serif';
    c.fillText('発売機で券種・馬番・金額・レースを確認してください。', 60, canvas.height - 70);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/png'));
    const fileName = `MAIBuilder_${p.date || ''}_${p.race_num || ''}R.png`;
    const file = new File([blob], fileName, {type:'image/png'});
    if (navigator.share && navigator.canShare && navigator.canShare({files:[file]})) {
      await navigator.share({files:[file], title:'スマッピーQRと買い目'});
    } else {
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = fileName; a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
    toast('買い目とQRを画像として保存しました');
  } catch (e) {
    toast('画像を保存できませんでした。QRを長押しして保存してください。');
  }
}

async function loadContextTrend(p) {
  const box = $('#contextTrend');
  if (!box || !p || !p.race_id) return;
  box.innerHTML = `<section class="context-trend loading-card">
    <div class="section-label">この条件で重視された項目</div>
    <p>対象レースより前のデータを確認しています…</p></section>`;
  let d;
  try { d = await getJSON(`/api/race-context/${encodeURIComponent(p.race_id)}`); }
  catch (err) {
    box.innerHTML = err.status === 409 ? '' : '<p class="note">条件別傾向を取得できませんでした。</p>';
    return;
  }
  state.contextTrend = d;
  if (!d.available || !(d.items || []).length) {
    box.innerHTML = `<section class="context-trend card">
      <div class="section-label">この条件で重視された項目</div>
      <p>${esc(d.message || '比較できる過去データがありません')}</p></section>`;
    return;
  }
  const trendRows = (items) => items.map((item, index) => {
    const lift = Number(item.popularity_adjusted_lift || 0) * 100;
    return `<div class="trend-row"><span class="trend-rank">${index + 1}</span>
      <span class="trend-name"><b>${esc(item.label)}</b>
        <small>${Number(item.n_races)}レース・項目上位馬の馬券内率 ${pct(item.place_rate)}</small></span>
      <span class="trend-lift"><b>+${lift.toFixed(1)}pt</b><small>同人気帯比</small></span>
      ${item.reliable ? '<span class="trend-signal">傾向明瞭</span>' : '<span class="trend-signal muted">参考</span>'}
    </div>`;
  }).join('');
  const recent = d.recent || {};
  const recentBlock = recent.available && (recent.items || []).length
    ? `<div class="trend-recent"><div class="trend-scope"><b>前日の傾向</b>
        <span>${esc(formatDate(recent.date))}・${esc(recent.label)}・${Number(recent.n_races)}レース</span></div>
        ${trendRows(recent.items)}<p class="trend-note">${esc(recent.message || '')}</p></div>`
    : `<div class="trend-recent empty-recent"><b>前日の傾向</b>
        <span>${esc(recent.message || '前日の比較対象レースがありません')}</span></div>`;
  const historicalRows = trendRows(d.items);
  box.innerHTML = `<section class="context-trend">
    <div class="section-label">このレースで参考にする傾向</div>
    <div class="card trend-card">${recentBlock}
      <details class="trend-history"${recent.available ? '' : ' open'}>
        <summary><b>中長期の傾向</b><span>${esc(d.scope.label)}・過去${Number(d.scope.n_races)}レース</span></summary>
        ${historicalRows}</details>
      <button class="cta trend-apply" id="applyTrend">${recent.available
        ? '前日＋中長期傾向でこのレースを予想' : '中長期の推奨項目でこのレースを予想'}</button>
      <p class="trend-note">${esc(d.message || '')}。傾向は的中や利益を保証するものではありません。</p>
    </div></section>`;
  const apply = $('#applyTrend');
  apply.addEventListener('click', async () => {
    apply.disabled = true;
    try {
      const saved = await postJSON('/api/configs', {config: d.recommended_config});
      state.config = {id:saved.id, name:saved.name, version:saved.version};
      try { sessionStorage.setItem(CFG_ID_KEY, saved.id); } catch (e) { /* 無視 */ }
      applyConfigToForm(saved.config, saved.name);
      await loadPredict();
      toast('この条件の推奨項目を適用しました');
    } catch (err) {
      apply.disabled = false;
      toast('推奨項目を適用できませんでした');
    }
  });
}

/* ---- 描画と結線 ------------------------------------------------------ */
function renderBet(p) {
  const d = $('#bsDraft');
  if (d) { d.innerHTML = draftBlock(p); bindDraft(p); }
  renderBetResult(p);
}
function renderBetResult(p) {
  const box = $('#bsResult');
  if (!box) return;
  box.innerHTML = betResultBlock(p);
  bindBetActions(p);
  bindTerms();
}

function bindDraft(p) {
  $$('#bsDraft .btype').forEach((el) => el.addEventListener('click', () => {
    resetDraft(el.dataset.btype);
    renderBet(p);
  }));
  $$('#bsDraft .bmode').forEach((el) => el.addEventListener('click', () => {
    setDraftMode(betTypeSpec(bet.draft.type), el.dataset.bmode);
    renderBet(p);
  }));
  $$('#bsDraft .bed-chips').forEach((box) => {
    const i = Number(box.dataset.group);
    box.querySelectorAll('.bchip').forEach((el) => el.addEventListener('click', () => {
      const g = bet.draft.groups[i];
      const n = el.dataset.num;
      const at = g.indexOf(n);
      if (at >= 0) g.splice(at, 1); else g.push(n);
      renderBet(p);
    }));
  });
  const add = $('#bedAdd');
  if (add) add.addEventListener('click', () => {
    bet.entries.push({ type: bet.draft.type, mode: bet.draft.mode,
                       groups: bet.draft.groups.map((g) => g.slice()) });
    bet.dirty = true;
    resetDraft(bet.draft.type);       // 同じ券種で続けて組めるように選択だけ空にする
    renderBet(p);
    refreshBet(p);
  });
  bindTerms();
}

function bindBetActions(p) {
  const suggest = $('#bsSuggest');
  if (suggest) suggest.addEventListener('click', () => {
    bet.entries = JSON.parse(JSON.stringify(bet.suggested || []));
    bet.dirty = true;
    refreshBet(p);
  });
  $$('#bsDraft [data-quick]').forEach((el) => el.addEventListener('click', () => {
    const i = Number(el.dataset.group);
    bet.draft.groups[i] = quickPicks(p, betTypeSpec(bet.draft.type), el.dataset.quick);
    renderBet(p);
  }));
  const applyBudget = (allocator) => {
    const input = $('#bsBudget');
    const budget = Number(input && input.value);
    const minBudget = bet.total * 100;
    if (!Number.isInteger(budget) || budget < minBudget || budget > 1000000 || budget % 100) {
      toast(`予算は${yen(minBudget)}以上・100円単位・100万円以下で入力してください`);
      return;
    }
    let allocation;
    try {
      allocation = allocator(budget, bet.slip || []);
    } catch (err) {
      toast('全点のオッズが発表されてから資金配分してください');
      return;
    }
    bet.budgetYen = budget;
    liveIndexes().forEach((idx, slipIndex) => {
      bet.entries[idx].amounts_yen = allocation.byEntry[slipIndex];
      delete bet.entries[idx].amount_yen;
    });
    bet.dirty = true;
    refreshBet(p);
  };
  const allocate = $('#bsAllocate');
  if (allocate) allocate.addEventListener('click', () => applyBudget(oddsBudgetAllocation));
  const allocateEqual = $('#bsAllocateEqual');
  if (allocateEqual) allocateEqual.addEventListener('click', () => applyBudget(equalBudgetAllocation));
  $$('#bsResult details.bs-pts').forEach((el) => el.addEventListener('toggle', () => {
    const slipIndex = Number(el.dataset.slip);
    if (el.open) bet.openSlipDetails.add(slipIndex);
    else bet.openSlipDetails.delete(slipIndex);
  }));
  $$('#bsResult .bs-amount').forEach((el) => el.addEventListener('change', () => {
    const amount = Number(el.value);
    if (!Number.isInteger(amount) || amount < 100 || amount > 999900 || amount % 100) {
      toast('金額は100円単位で入力してください');
      renderBetResult(p);
      return;
    }
    const idx = liveIndexes()[Number(el.dataset.slip)];
    if (idx == null) return;
    bet.entries[idx].amount_yen = amount;
    delete bet.entries[idx].amounts_yen;
    bet.dirty = true;
    refreshBet(p);
  }));
  $$('#bsResult .bo-amount').forEach((el) => el.addEventListener('change', () => {
    const amount = Number(el.value);
    if (!applyPointAmountEdit(Number(el.dataset.slip), Number(el.dataset.point), amount)) {
      toast('金額は100円単位で入力してください');
      renderBetResult(p);
      return;
    }
    bet.openSlipDetails.add(Number(el.dataset.slip));
    refreshBet(p);
  }));
  $$('#bsResult [data-remove]').forEach((el) => el.addEventListener('click', () => {
    // 表示は「組めた買い目」の並び。指定の並びと対応づけて消す
    const idx = liveIndexes()[Number(el.dataset.remove)];
    if (idx == null) return;
    bet.entries.splice(idx, 1);
    bet.openSlipDetails.clear();
    bet.dirty = true;
    refreshBet(p);
  }));
  $$('#bsResult [data-drop]').forEach((el) => el.addEventListener('click', () => {
    bet.entries.splice(Number(el.dataset.drop), 1);
    bet.openSlipDetails.clear();
    bet.dirty = true;
    refreshBet(p);
  }));
  const clear = $('#bsClear');
  if (clear) clear.addEventListener('click', () => {
    bet.entries = []; bet.openSlipDetails.clear(); bet.dirty = true; refreshBet(p);
  });
  const reset = $('#bsReset');
  if (reset) reset.addEventListener('click', () => {
    bet.entries = JSON.parse(JSON.stringify(bet.suggested || []));
    bet.openSlipDetails.clear();
    bet.dirty = true;
    refreshBet(p);
    toast('印から買い目を作り直しました');
  });
  const copy = $('#bsCopy');
  if (copy) copy.addEventListener('click', async () => {
    // C-3: 表記の正本はサーバ (betslip.combo_text)。ここで結合し直すと
    // 馬単が馬連と同じ「11-10」になる。**公式サイトへ手入力する経路そのもの**なので、
    // 方向が消えると誤った馬券を買うことになる。UI は文字列を組み立て直さない。
    const text = (bet.slip || []).flatMap((t) => (t.texts || []).map((x, i) =>
      `${t.label} ${x} ${yen(pointAmount(t, i))}`)).join('\n');
    try {
      await navigator.clipboard.writeText(text);
      toast('買い目と金額をコピーしました');
    } catch (e) {
      toast('コピーできませんでした。画面の一覧をご利用ください。');
    }
  });
  const qr = $('#bsQr');
  if (qr) qr.addEventListener('click', () => createSmappyQr(p));
  const record = $('#bsRecord');
  if (record) record.addEventListener('click', () => saveManualPurchase(p));
  const saveQr = $('#smqSave');
  if (saveQr) saveQr.addEventListener('click', () => saveQrWithBets(p));
  const confirm = $('#smqConfirm');
  if (confirm) confirm.addEventListener('click', async () => {
    if (!bet.qr || !bet.qr.purchase_id) return;
    await postJSON('/api/purchases/confirm', {
      purchase_id: bet.qr.purchase_id, confirmed: true,
    });
    bet.qr.purchase_status = 'purchased';
    renderBetResult(p);
    toast('購入内容を本日の成績へ保存しました');
  });
}

async function saveManualPurchase(p) {
  if (!bet.slip.length || bet.recordPending) return;
  bet.recordPending = true;
  renderBetResult(p);
  try {
    const got = await postJSON('/api/purchases/record', {
      race_id: p.race_id, date: p.date || state.selectedRaceDate,
      selection: bet.entries, config_id: state.config && state.config.id,
    });
    bet.savedPurchaseId = got.purchase_id;
    bet.savedKey = betSelectionKey();
    toast('購入済みの買い目として成績へ保存しました');
  } catch (err) {
    toast((err.body && err.body.message) || '買い目を保存できませんでした');
  } finally {
    bet.recordPending = false;
    renderBetResult(p);
  }
}

function betSelectionKey() {
  return JSON.stringify((bet.entries || []).map((e) => ({
    type:e.type, mode:e.mode, groups:e.groups, amount_yen:e.amount_yen,
    amounts_yen:e.amounts_yen,
  })));
}

async function createSmappyQr(p) {
  bet.qrPending = true; bet.qrError = '';
  renderBetResult(p);
  try {
    bet.qr = await postJSON('/api/smappy/qr',
                            { race_id: p.race_id, selection: bet.entries,
                              config_id: state.config && state.config.id });
    bet.qrKey = betSelectionKey();
    toast('JRAスマッピーのQRを作成しました');
  } catch (err) {
    bet.qrError = (err.body && err.body.message)
      || 'スマッピーQRを作成できませんでした';
  } finally {
    bet.qrPending = false;
    renderBetResult(p);
  }
}

/* 組めた買い目の表示位置 → 指定の位置。組めなかった分だけずれる。 */
function liveIndexes() {
  const bad = new Set((bet.skipped || []).map((x) => x.index));
  const out = [];
  bet.entries.forEach((_, i) => { if (!bad.has(i)) out.push(i); });
  return out;
}

/* 選択が変わったらサーバに組ませ直す。UI は組み合わせを作らない。 */
async function refreshBet(p) {
  let got;
  // 30秒ごとのオッズ更新では買い目が同じなのでQRを消さない。金額・馬番が
  // 変わった場合だけ、古い投票内容を残さないため破棄する。
  if (bet.qr && bet.qrKey !== betSelectionKey()) { bet.qr = null; bet.qrKey = null; }
  if (bet.savedPurchaseId && bet.savedKey !== betSelectionKey()) {
    bet.savedPurchaseId = null; bet.savedKey = null;
  }
  bet.qrError = '';
  try {
    got = await postJSON('/api/betslip',
                         { race_id: p.race_id, date: p.date || state.selectedRaceDate,
                           selection: bet.entries });
  } catch (err) {
    // 組めなかった理由は捏造しない。取れなかったことを出す
    bet.slip = []; bet.skipped = [];
    bet.total = 0; bet.text = '';
    bet.totalYen = 0; bet.oddsNote = '';
    renderBetResult(p);
    toast('買い目を組み直せませんでした');
    return;
  }
  bet.slip = got.slip || [];
  bet.skipped = got.skipped || [];
  bet.total = got.total || 0;
  bet.totalYen = got.total_yen || 0;
  bet.text = got.text || '';
  bet.oddsNote = got.odds_note || '';
  renderBetResult(p);
}

/* 段ごとに **何を選んだのか** を見せる。
 * 点の一覧だけだと、フォーメーションで「1着に誰を入れたか」が追えない
 * (11→3→2 と 11→10→6 が並んでいても、2着候補が何だったのか読み取れない)。
 * 見出しと番号の並べ方はサーバが決める (`picks`)。 */
function picksBlock(t) {
  const picks = t.picks || [];
  if (!picks.length) return '';
  return `<dl class="bs-picks">${picks.map((g) => `
    <dt>${esc(g.label)}</dt><dd class="num">${esc(g.text)}</dd>`).join('')}</dl>`;
}

/* JRA受付内容との読み合わせ。**誤登録の最後の防波堤**なのでQR作成後も残す。
 * 公式と同じ順序 (レース → 式別 → 馬番 → 金額) で並べ、送った内容を
 * 人が読み合わせられるようにする。 */
function handoffBlock(p, slip, total) {
  const rows = slip.map((t) => `<li class="hb-item">
      <div class="hb-top">
        <span class="hb-t">${esc(t.label)}</span>
        <span class="hb-mode">${esc(t.mode_label)}</span>
        <span class="hb-n">${t.n}点</span>
      </div>
      ${picksBlock(t)}
      <div class="hb-c num">${(t.texts || []).map((x, i) =>
        `${esc(x)} ${yen(pointAmount(t, i))}`).join(' / ')}</div>
      <div class="hb-money">小計 ${yen(t.subtotal_yen)}</div>
    </li>`).join('');
  return `<details class="hb">
    <summary class="hb-head">公式サイトへの入れ方と読み合わせ
      <span class="hb-total">全${total}点</span></summary>
    <div class="hb-body">
      <p class="hb-step">① レースを選ぶ — <b>${esc(raceIdentity(p))}</b></p>
      <p class="hb-step">② 式別と馬番を、下の順に入れる
        (<b>矢印の向きは順序の指定</b>です。馬単・三連単は着順)</p>
      <ol class="hb-list">${rows}</ol>
      <p class="hb-step">③ 「スマッピーQRを作成」でJRAへ送信する</p>
      <p class="hb-step hb-check">④ QRの買い目一覧と上の表を読み合わせ、
        式別・馬番・点数・金額・レースが一致しているか確認する。
        <b>全${total}点</b>あります。1つでも違えば作り直してください。</p>
    </div>
  </details>`;
}

/* 引き渡しに使うレースの同定。競馬場名は予想レスポンスではなく一覧側にあるので、
 * そこから引く。**取れないときは書かない** (誤った会場名を出すより無い方が安全)。 */
function raceIdentity(p) {
  const r0 = state.races.find((x) => x.race_id === p.race_id) || {};
  const num = Number(p.race_num || r0.race_num || 0);
  const parts = [];
  if (r0.track_label) parts.push(r0.track_label);
  if (num) parts.push(`${num}R`);
  if (p.start_time) parts.push(`発走 ${p.start_time}`);
  return parts.join(' ') || 'このレース';
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
  const renderRoi = (e) => {
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
  };
  const allEntries = d.entries || [];
  const roiTop = allEntries.filter((e) => !e.is_baseline).slice(0, 3);
  const roiBaseline = allEntries.filter((e) => e.is_baseline);
  const roiRemaining = allEntries.filter((e) => !e.is_baseline).slice(3);
  const rows = [...roiTop, ...roiBaseline].map(renderRoi).join('');
  box.innerHTML = `<div class="section-label">回収率ランキング</div>
    ${allTied ? `<div class="rr-tied">どのマイAIも回収率の差は誤差の範囲です`
      + `(信頼区間が重なっています)。順位は付けられません。</div>` : ''}
    <div class="card rr">${rows || '<p class="roi-row muted">マイAIがありません</p>'}</div>
    ${roiRemaining.length ? `<details class="ai-more"><summary>そのほかのAI ${roiRemaining.length}件を表示</summary>
      <div class="card rr">${roiRemaining.map(renderRoi).join('')}</div></details>` : ''}
    <p class="note" style="margin-top:6px">
      ${esc(ymd((d.period || [])[0] || ''))}以降の全レースに同じ設定を当てはめた集計です。
      ${esc(d.roi_note || '')}</p>`;
}

/* B-1: 的中率では1番人気をほぼ上回れないので、作成直後の成績にも
 * **回収率(人気薄を当てた価値が入る指標)** を出す。信頼区間つき。 */
function btRoiBlock(bt) {
  const s = bt.roi_stats, b = bt.baseline_roi_stats;
  if (!s) return '';
  const line = s.enough
    ? `<span class="bt2-you num">${pct(s.roi)}</span>
       <span class="bt2-sep">対</span>
       <span class="bt2-base num">${b && b.enough ? pct(b.roi) : '—'}</span>`
    : `<span class="bt2-base">判定できません(${s.races}/${s.min_races}レース)</span>`;
  const ci = s.enough && s.ci
    ? `<div class="bt2-foot">回収率の幅 ${pct(s.ci[0])}〜${pct(s.ci[1])}。`
      + `${esc(bt.roi_note || '')}</div>` : '';
  return `<div class="card bt2" style="margin-top:9px">
    <div class="bt2-row"><div class="bt2-k">回収率(単勝)</div>
      <div class="bt2-pair">${line}</div><div class="bt2-diff even"></div></div>
    ${ci}</div>`;
}
