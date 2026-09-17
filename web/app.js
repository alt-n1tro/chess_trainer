// Chess Trainer -- the one screen.
//
// The server owns the rules. This file owns the board, the panel and the
// animation between one position and the next.
import {
  Chessboard, INPUT_EVENT_TYPE, COLOR, BORDER_TYPE, FEN,
} from "./vendor/cm-chessboard/src/Chessboard.js";
import { Markers, MARKER_TYPE }
  from "./vendor/cm-chessboard/src/extensions/markers/Markers.js";
import { Arrows, ARROW_TYPE }
  from "./vendor/cm-chessboard/src/extensions/arrows/Arrows.js";
import { PromotionDialog, PROMOTION_DIALOG_RESULT_TYPE }
  from "./vendor/cm-chessboard/src/extensions/promotion-dialog/PromotionDialog.js";
import { RightClickAnnotator }
  from "./vendor/cm-chessboard/src/extensions/right-click-annotator/RightClickAnnotator.js";

const $ = (id) => document.getElementById(id);
const el = {
  board: $("board"), overlay: $("board-overlay"), status: $("status-text"),
  context: $("context"), progress: $("progress"), verdict: $("verdict"),
  actions: $("actions"), tree: $("tree"), treeBox: $("tree-box"),
  menu: $("menu"), help: $("help"), editor: $("editor"), toast: $("toast"),
  modeBtn: $("btn-mode"), modeList: $("mode-list"),
  chainBtn: $("btn-chain"), chainList: $("chain-list"),
  cards: $("cards"), library: $("library"), warmBtn: $("btn-warming"),
  version: $("version"),
};

const MODE_LABEL = { openings: "Openings", middlegame: "Middlegame", endgame: "Endgame" };
const MODE_NOUN = { openings: "opening", middlegame: "middlegame", endgame: "endgame" };
const MARKER_MOVE = { class: "marker-square-move", slice: "markerSquare" };
const MARKER_BEST = { class: "marker-square-best", slice: "markerSquare" };
const MARKER_PLAYED = { class: "marker-square-played", slice: "markerSquare" };
// The verdict's own colours, so the square you landed on says how good it was.
const MARKER_TONE = {
  best: { class: "marker-square-best", slice: "markerSquare" },
  good: { class: "marker-square-good", slice: "markerSquare" },
  inaccuracy: { class: "marker-square-inaccuracy", slice: "markerSquare" },
  mistake: { class: "marker-square-mistake", slice: "markerSquare" },
  blunder: { class: "marker-square-blunder", slice: "markerSquare" },
  shown: { class: "marker-square-shown", slice: "markerSquare" },
};
const MARKER_SELECTED = { class: "marker-square-selected", slice: "markerSquare" };
const ARROW_BEST = { class: "arrow-best" };
// What the right-click annotator draws, as opposed to what the app draws.
const ANNOTATION_CLASS = /^(arrow|marker-circle)-(success|warning|info|danger)$/;

let state = null;
let shownKey = null;      // which round the board is currently showing
let shownFen = null;
let busy = 0;
let pvCursor = null;      // {kind, index} while walking a principal variation
let submitting = false;   // one move per turn, however it was entered
let selected = null;      // the square whose piece is picked up
let pickedUpByPress = false;  // did the press now finishing pick that piece up?

const board = new Chessboard(el.board, {
  position: FEN.start,
  assetsUrl: "./vendor/cm-chessboard/assets/",
  style: {
    cssClass: "ct",
    showCoordinates: true,
    borderType: BORDER_TYPE.none,
    pieces: { file: "pieces/standard.svg" },
    animationDuration: 240,
  },
  extensions: [
    { class: Markers, props: { autoMarkers: undefined } },
    { class: Arrows },
    { class: PromotionDialog },
    { class: RightClickAnnotator },
  ],
});

// --- transport -------------------------------------------------------------

async function call(path, body) {
  busy += 1;
  if (busy === 1) el.overlay.hidden = false;
  try {
    const res = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body || {}),
    });
    const data = await res.json();
    if (data.error) { toast(data.error); return null; }
    if (data.kind === "state") await apply(data);
    if (data.message) toast(data.message);
    return data;
  } catch (err) {
    toast("The server is not answering. Is ./run web still running?");
    return null;
  } finally {
    busy -= 1;
    if (busy === 0) el.overlay.hidden = true;
  }
}

let toastTimer = null;
function toast(text) {
  if (!text) { el.toast.hidden = true; return; }
  el.toast.textContent = text;
  el.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.toast.hidden = true; }, 6000);
}

// --- rendering -------------------------------------------------------------

async function apply(data) {
  state = data;
  pvCursor = null;
  document.body.dataset.mode = data.mode;
  el.modeBtn.textContent = `${MODE_LABEL[data.mode]} ▾`;
  el.chainBtn.textContent = data.chain === 1
    ? "1 move \u25be" : `${data.chain} moves \u25be`;
  el.chainList.innerHTML = Array.from({ length: data.max_chain || 5 }, (_, i) => {
    const n = i + 1;
    const note = n === 1 ? "find the move"
      : `find the move, then the next ${n - 1}`;
    return `<li data-set-chain="${n}" class="${n === data.chain ? "on" : ""}">` +
      `<span>${n} move${n === 1 ? "" : "s"}</span><span class="n">${note}</span></li>`;
  }).join("");
  el.modeList.innerHTML = (data.modes || []).map((m) =>
    `<li data-set-mode="${m.mode}" class="${m.active ? "on" : ""}">` +
    `<span>${MODE_LABEL[m.mode]}</span>` +
    `<span class="n">${m.count} position${m.count === 1 ? "" : "s"}</span></li>`
  ).join("");
  if (data.version) el.version.textContent = `v${data.version}`;
  renderWarming(data.warming !== false);
  renderCards(data);
  // A state arriving while you are setting up (a finished analysis, say) must
  // not redraw the board you are building.
  if (editor.open) return;
  if (data.game) await renderGame(data.game);
  else await renderDrill(data.drill);
}

/** The idle switch: what it says is what the engines are doing. */
function renderWarming(on) {
  el.warmBtn.setAttribute("aria-pressed", on ? "true" : "false");
  el.warmBtn.innerHTML = `<span class="dot"></span>` +
    (on ? "Engines warming" : "Engines idle");
}

/** The lock on one game, and the analysis running, as standing cards at the
    top of the panel. Neither goes away on its own: the lock has an x, and a
    finished analysis is dismissed when you have read it. */
function renderCards(data) {
  const job = data.job;
  const focus = data.focus;
  let html = "";
  if (job && !jobDismissed(job)) html += jobCard(job);
  if (focus) html += focusCard(focus, data);
  el.cards.innerHTML = html;
  pollJob(job);
}

let dismissedJob = null;          // the finished analysis you have read
function jobDismissed(job) {
  return !job.active && dismissedJob === jobKey(job);
}
function jobKey(job) {
  return `${job.source}|${job.game_id}|${job.stage}`;
}

function jobCard(job) {
  const pct = job.moves_total
    ? Math.round(100 * job.moves_done / job.moves_total) : 0;
  if (job.stage === "error") {
    return `<div class="card-note"><button class="x" data-act="job-dismiss"` +
      ` title="Dismiss">✕</button><div class="kicker">Analysis failed</div>` +
      `<div class="title">${esc(job.error || "It did not say why.")}</div></div>`;
  }
  if (job.active) {
    const what = job.stage === "importing" ? "Fetching the game"
      : `Move ${job.moves_done} of ${job.moves_total}` +
        (job.games > 1 ? ` · game ${job.game_index} of ${job.games}` : "");
    return `<div class="card-note"><div class="kicker">Analysing` +
      ` · ${job.seconds}s</div>` +
      `<div class="title">${esc(job.game || job.source)}</div>` +
      `<div class="meta">${esc(what)} · ${job.positions} position` +
      `${job.positions === 1 ? "" : "s"} so far</div>` +
      `<div class="track"><div class="fill" style="width:${pct}%"></div></div></div>`;
  }
  return `<div class="card-note"><button class="x" data-act="job-dismiss"` +
    ` title="Dismiss">✕</button><div class="kicker">Analysed` +
    ` · ${job.seconds}s</div>` +
    `<div class="title">${esc(job.game || job.source)}</div>` +
    `<div class="meta">${job.positions} position` +
    `${job.positions === 1 ? "" : "s"} to drill` +
    (job.accuracy !== null && job.accuracy !== undefined
      ? ` · you played ${job.accuracy}%` : "") + `</div>` +
    (job.game_id
      ? `<div class="row"><button class="primary" data-act="drill-game"` +
        ` data-id="${job.game_id}">Drill this game</button>` +
        `<button data-act="open-game" data-id="${job.game_id}">Open it</button></div>`
      : "") + `</div>`;
}

function focusCard(focus, data) {
  const counts = focus.counts || {};
  const parts = Object.keys(counts).filter((m) => counts[m])
    .map((m) => `${counts[m]} ${MODE_NOUN[m]}`);
  const who = `${esc(focus.white || "?")} vs ${esc(focus.black || "?")}`;
  return `<div class="card-note locked">` +
    `<button class="x" data-act="unlock" title="Drill every game again">✕</button>` +
    `<div class="kicker">🔒 Drilling this game only</div>` +
    `<div class="title">${who}</div>` +
    `<div class="meta">${parts.join(" · ") || "no positions"}` +
    (focus.accuracy !== null && focus.accuracy !== undefined
      ? ` · you played ${focus.accuracy}%` : "") + `</div>` +
    `<div class="row"><button class="primary" data-act="random">Random from it</button>` +
    `<button data-act="open-game" data-id="${focus.id}">Open it</button></div></div>`;
}

/** While an analysis runs, ask how it is going. Nothing polls otherwise. */
let jobTimer = null;
function pollJob(job) {
  if (!job || !job.active) { clearTimeout(jobTimer); jobTimer = null; return; }
  if (jobTimer) return;
  jobTimer = setTimeout(async () => {
    jobTimer = null;
    const res = await fetch("/api/analyse/status",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })
      .then((r) => r.json()).catch(() => null);
    if (!res) return;
    if (state) {
      state.job = res.job;
      state.focus = res.focus;
      if (res.modes) state.modes = res.modes;
      renderCards(state);
      // A finished analysis changes what the pools hold.
      if (res.job && !res.job.active) call("/api/state", {});
    }
  }, 600);
}

async function renderDrill(d) {
  const mine = ++renderSeq;
  el.treeBox.hidden = false;
  if (!d) {
    moveInput(null);
    await setBoard(FEN.start, false);
    document.body.dataset.turn = "white";
    el.status.textContent = "";
    el.context.innerHTML = `<h1>Nothing in this pool yet</h1>` +
      `<div class="meta">Import your games, then build the pools.</div>` +
      `<div class="tail">./run import --player &lt;name&gt;<br>./run review</div>`;
    el.progress.innerHTML = "";
    el.verdict.innerHTML = "";
    el.actions.innerHTML =
      rowOf([btnHtml("library", "Games played", false, "primary")]) +
      rowOf([btnHtml("menu", "Browse positions"),
             btnHtml("editor", "Set up a position")], "small");
    el.tree.innerHTML = "";
    return;
  }

  const a = d.answer;
  selected = null;
  // Once you ask for the engine's move, the board goes back to the position
  // you were asked about and shows that move alone. An arrow drawn over the
  // position your own move already made starts from a square its piece has
  // left, which is worse than no arrow at all.
  const showingBest = !!a && d.done && a.verdict !== "best" && a.best_move
                      && bestIsOut(d);
  const hist = d.history || [];
  const at = cursorAt(d);
  const stepping = at < hist.length;      // standing somewhere earlier
  const key = `${d.node_id_current}|${d.opp_move}|${d.round}` +
              `|${d.depth_level}|${d.step || 1}|${showingBest ? "best" : ""}` +
              `|${stepping ? at : ""}`;
  const target = stepping
    ? (at === 0 ? d.base_fen : hist[at - 1].fen)
    : (showingBest ? d.fen : (d.done && a ? a.fen_after : d.fen));

  moveInput(null);
  await orient(d.my_colour === "black" ? COLOR.black : COLOR.white);
  if (mine !== renderSeq) return;      // a newer render has taken over
  if (stepping) {
    // Walking the round's own moves: no animation, just the position asked
    // for. The replays below would drag the board back to the latest one.
    await setBoard(target, false);
  } else if (!a && key !== shownKey && d.base_fen) {
    // A new round: show the position they moved from, then play their move.
    await setBoard(d.base_fen, false);
    markers([[d.opp_move.slice(0, 2), MARKER_MOVE]], true);
    await pause(180);
    await setBoard(d.fen, true);
  } else if (a && !d.done && d.opp_reply && key !== shownKey) {
    // Mid-chain: your move lands, then their reply, then it is your turn.
    await setBoard(a.fen_after, shownFen !== a.fen_after);
    markers(markersFor(d, true), true);
    await pause(340);
    await setBoard(d.fen, true);
  } else {
    await setBoard(target, shownFen !== null && shownFen !== target);
  }
  shownKey = key;
  if (mine !== renderSeq) return;

  board.removeArrows();
  if (stepping) {
    const last = at > 0 ? hist[at - 1] : null;
    if (last) {
      markers([[last.uci.slice(0, 2), MARKER_MOVE],
               [last.uci.slice(2, 4), MARKER_MOVE]], true);
    } else {
      markers([], true);
    }
  } else if (showingBest) {
    // One thing on the board: the move you were looking for, from the square
    // it starts on, in the position where it had to be found. Your own move
    // is not drawn here — it is not on this board.
    markers([[d.opp_move.slice(0, 2), MARKER_MOVE],
             [d.opp_move.slice(2, 4), MARKER_MOVE]], true);
    board.addArrow(ARROW_BEST, a.best_move.slice(0, 2), a.best_move.slice(2, 4));
    markers([[a.best_move.slice(2, 4), MARKER_BEST]], false);
  } else {
    markers(markersFor(d), true);
  }

  document.body.dataset.turn = d.my_colour;
  if (d.can_answer && !stepping) {
    moveInput(d.my_colour === "black" ? COLOR.black : COLOR.white);
  }

  const reply = d.opp_reply && !d.done ? d.opp_reply.san : null;
  const more = d.chain > 1 ? ` Move ${d.step} of ${d.chain}.` : "";
  const asPlayed = d.opp_played_in_game ? " — as in the game" : "";
  if (stepping) {
    const last = at > 0 ? hist[at - 1] : null;
    const who = last ? (last.by === "you" ? "You played" : "They played") : "";
    el.status.textContent = last
      ? `${who} ${last.san}. Step ${at} of ${hist.length} — → to come back.`
      : `The position before their move. → to step forward.`;
    renderContext(d);
    renderProgress(d);
    renderVerdict(d);
    renderActions(d);
    renderTree();
    return;
  }
  el.status.textContent = d.can_answer
    ? (reply ? `They answered ${reply}. Your move.${more}`
             : `They played ${d.opp_san}${asPlayed}. Your move.${more}`)
    : showingBest
      ? `Back at the position you had to solve: ${a.best_san} was the move.`
      : (a ? `You played ${a.my_san || "—"}.`
           : `They played ${d.opp_san}${asPlayed}.`);

  renderContext(d);
  renderProgress(d);
  renderVerdict(d);
  renderActions(d);
  renderTree();
}

async function orient(colour) {
  if (board.getOrientation() === colour) return;
  await board.setOrientation(colour);
}

// Board updates run one after another. Two overlapping setPosition calls --
// an animation still in flight when the next one starts -- can land pieces
// from the older position on top of the newer one.
let boardQueue = Promise.resolve();
// Which render is the current one. A render that has been overtaken stops
// rather than finishing on top of the newer one.
let renderSeq = 0;
// Move input is enabled once, for one colour: enabling it twice makes the
// board library complain, and disabling what is not on loses a click.
let inputColour = null;

function moveInput(colour) {
  if (inputColour === colour) return;
  if (inputColour !== null) board.disableMoveInput();
  inputColour = colour;
  if (colour !== null) board.enableMoveInput(inputHandler, colour);
}

function setBoard(fen, animated) {
  boardQueue = boardQueue.then(async () => {
    if (fen === shownFen && animated) return;
    shownFen = fen;
    await board.setPosition(fen, !!animated);
  }).catch(() => {});
  return boardQueue;
}

function pause(ms) { return new Promise((r) => setTimeout(r, ms)); }

function markers(list, clear) {
  if (clear) board.removeMarkers();
  list.forEach(([square, type]) => board.addMarker(type, square));
}

function markersFor(d, midMove) {
  const out = [];
  const a = d.answer;
  const last = !midMove && a && !d.done && d.opp_reply
    ? d.opp_reply.uci : d.opp_move;
  if (last) {
    out.push([last.slice(0, 2), MARKER_MOVE]);
    out.push([last.slice(2, 4), MARKER_MOVE]);
  }
  if (a && a.my_move) {
    const tone = MARKER_TONE[a.tone] || MARKER_PLAYED;
    out.push([a.my_move.slice(0, 2), tone]);
    out.push([a.my_move.slice(2, 4), tone]);
  }
  return out;
}

function renderContext(d) {
  const src = d.source || {};
  const g = src.game;
  let html = "";
  if (g) html = `<div class="players">${playersLine(g)}</div>`;
  html += `<h1>${esc(d.name || MODE_LABEL[d.mode])}</h1>`;
  const bits = [d.phase];
  if (d.depth_level) bits.push(`level ${d.depth_level}`);
  html += `<div class="meta">${bits.join(" · ")} · you are ${d.my_colour}` +
    ` <button class="link" data-act="fen" title="Copy this position as FEN,` +
    ` to check it anywhere else">FEN</button></div>`;
  if (src.tail) html += `<div class="tail">${esc(src.tail)}</div>`;
  el.context.innerHTML = html;
}

function playersLine(g) {
  const w = `${esc(g.white || "?")}${g.white_elo ? ` (${g.white_elo})` : ""}`;
  const b = `${esc(g.black || "?")}${g.black_elo ? ` (${g.black_elo})` : ""}`;
  const res = g.result ? ` \u00b7 ${esc(g.result)}` : "";
  let text;
  if (g.my_colour === "white")
    text = `<span class="you">You</span> (White${g.white_elo ? `, ${g.white_elo}` : ""}) vs ${b}${res}`;
  else if (g.my_colour === "black")
    text = `${w} vs <span class="you">you</span> (Black${g.black_elo ? `, ${g.black_elo}` : ""})${res}`;
  else
    text = `${w} vs ${b}${res} \u00b7 not your game`;
  // The line is the game: click it to open the game on chess.com.
  return g.url && /^https?:\/\//.test(g.url)
    ? `<a class="game-link" href="${esc(g.url)}" target="_blank" rel="noopener noreferrer"` +
      ` title="Open this game on chess.com">${text} \u2197</a>`
    : text;
}

function renderProgress(d) {
  const done = d.round_results || [];
  const dots = [];
  for (let i = 0; i < d.rounds; i += 1) {
    const tone = done[i] || "";
    const now = i + 1 === d.round && !d.answer ? " now" : "";
    dots.push(`<span class="dot ${tone}${now}"></span>`);
  }
  let html = `<span>Position ${d.round} of ${d.rounds}</span>` +
    `<span class="dots">${dots.join("")}</span>`;
  if (d.chain > 1) {
    const steps = [];
    for (let i = 0; i < d.chain; i += 1) {
      const tone = (d.steps || [])[i] || "";
      const now = !tone && i + 1 === d.step && !d.done ? " now" : "";
      steps.push(`<span class="step ${tone}${now}"></span>`);
    }
    html += `<span class="sep">\u00b7</span><span class="chain">` +
      `<span>move ${Math.min(d.step, d.chain)} of ${d.chain}</span>` +
      `<span class="steps">${steps.join("")}</span></span>`;
  }
  el.progress.innerHTML = html;
}

// Walking back through the moves played in this round. null means "at the
// latest position", which is where everything starts.
let stepCursor = { key: null, at: null };

function cursorAt(d) {
  const key = revealKey(d);
  if (stepCursor.key !== key) stepCursor = { key, at: null };
  const hist = d.history || [];
  return stepCursor.at === null ? hist.length : stepCursor.at;
}

function stepTo(d, index) {
  const hist = d.history || [];
  const at = Math.max(0, Math.min(index, hist.length));
  stepCursor = { key: revealKey(d), at: at >= hist.length ? null : at };
  shownKey = null;
  renderDrill(d);
}

// Nothing is given away before you ask for it: the reasons are behind a
// button, and the engine's move is behind a second one.
let revealed = { key: null, cost: false, best: false };

function revealKey(d) {
  const a = d.answer || {};
  return `${d.node_id_current}|${d.round}|${d.step || 1}|${a.my_move || ""}`;
}

function revealState(d) {
  const key = revealKey(d);
  if (revealed.key !== key) {
    const a = d.answer || {};
    // Asking to be shown the move is asking for all of it. Finding the move
    // yourself leaves nothing to spoil either. The lines stay folded even
    // then: reading the continuation is the drill, not the answer to it.
    const open = a.verdict === "shown" || a.verdict === "best";
    revealed = { key, cost: open, best: open, lines: {} };
  }
  if (!revealed.lines) revealed.lines = {};
  return revealed;
}

function bestIsOut(d) {
  const a = d.answer;
  return !!a && (a.verdict === "shown" || a.verdict === "best"
                 || revealState(d).best);
}

function renderVerdict(d) {
  const a = d.answer;
  if (!a) {
    const what = d.chain > 1
      ? `Find the best ${d.chain} moves in a row for ${d.my_colour}.`
      : `Find the best reply for ${d.my_colour}.`;
    const tag = d.opp_played_in_game
      ? ` <span class="meta">(what they really played)</span>` : "";
    el.verdict.innerHTML =
      `<div class="ask">They played <b>${esc(d.opp_san)}</b>${tag}. ${what}</div>`;
    return;
  }
  const ex = a.explanation || {};
  const open = revealState(d);
  const mine = a.verdict !== "best" && a.verdict !== "shown";
  const lost = a.delta_wp === null || a.delta_wp === undefined ? ""
    : `${a.delta_wp.toFixed(1)} points lost`;
  const head = a.verdict === "shown"
    ? `<span class="move">${esc(a.best_san)}</span><span class="label">Shown</span>`
    : `<span class="move">${esc(a.my_san)}</span><span class="label">${esc(a.label)}</span>`;

  // 1. Your move: what it gave up. Behind a button, because the whole point
  //    is to think first.
  let yours = "";
  if (mine) {
    const detail = (ex.cost && ex.cost.length)
      ? ex.cost.map((i) => `<p class="${esc(i.kind || "")}">${esc(i.text)}</p>`).join("")
      : `<p>The engine keeps ${a.wp_best !== null && a.wp_best !== undefined
          ? `${a.wp_best.toFixed(0)}%` : "more"} where your move leaves` +
        ` ${a.wp_mine !== null && a.wp_mine !== undefined
          ? `${a.wp_mine.toFixed(0)}%` : "less"}. Nothing structural went` +
        ` wrong — it is simply not the best move here.</p>`;
    yours = open.cost
      ? `<div class="section"><div class="tag">Your move</div>` +
        `<div class="explain">${detail}</div>` +
        pvRow("Line", ex.my_pv, "mine") + `</div>`
      : `<div class="row small"><button data-act="reveal-cost">` +
        `Why was ${esc(a.my_san)} ${esc((a.label || "").toLowerCase())}?` +
        `</button></div>`;
  }

  // 2. The engine's move: named only once you ask.
  let theirs = "";
  const whyHtml = (ex.why && ex.why.length)
    ? ex.why.map((i) => `<p class="${esc(i.kind || "")}">${esc(i.text)}</p>`).join("")
    : (ex.text ? `<p>${esc(ex.text)}</p>` : "");
  if (!mine) {
    theirs = `<div class="section"><div class="explain">${whyHtml}</div>` +
             pvRow("Line", ex.best_pv, "best") + `</div>`;
  } else if (open.best) {
    theirs = oppSection(d) +
      `<div class="section"><div class="tag">The engine's move</div>` +
      `<div class="detail">Best was <b>${esc(a.best_san)}</b>` +
      (a.wp_best !== null && a.wp_best !== undefined
        ? ` · ${a.wp_best.toFixed(0)}% vs ${(a.wp_mine ?? 0).toFixed(0)}%` : "") +
      `</div><div class="explain">${whyHtml}</div>` +
      pvRow("Line", ex.best_pv, "best") + `</div>`;
  } else if (open.cost) {
    theirs = `<div class="row small"><button class="primary"` +
      ` data-act="reveal-best">Explain the best move</button></div>`;
  }

  el.verdict.innerHTML =
    `<div class="card ${a.tone}">` +
    `<div class="head">${head}<span class="cost">${lost}</span></div>` +
    (a.detail && !mine ? `<div class="detail">${esc(a.detail)}</div>` : "") +
    (a.detail && mine && open.cost ? `<div class="detail">${esc(a.detail)}</div>` : "") +
    yours + theirs +
    `</div>` +
    (d.done ? "" : `<div class="ask">Keep going: find move ${d.step} of ` +
      `${d.chain}.</div>`);
}

/** The reasoning, a sentence to a line: what the move does, what the line
    forces, what it stops, and what the move most people would play costs. */
function explainHtml(ex) {
  const items = (ex.items || []).slice(0, 4);
  if (!items.length) {
    return ex.text ? `<div class="explain"><p>${esc(ex.text)}</p></div>` : "";
  }
  return `<div class="explain">` + items.map((i) =>
    `<p class="${esc(i.kind || "")}">${esc(i.text)}</p>`).join("") + `</div>`;
}

/** Their move, judged among the moves they had. Shown with the answer,
    because a bad move of theirs points straight at what to play. */
function oppSection(d) {
  const o = d.opp_explanation;
  if (!o) return "";
  const rank = o.rank && o.options
    ? `${ordinal(o.rank)} of ${o.options} they had` : "";
  const claims = (o.claims || []).slice(0, 3);
  return `<div class="section"><div class="tag">Their move</div>` +
    `<div class="detail"><b>${esc(o.san)}</b> — ` +
    `<span class="${esc(o.tone || "")}">${esc(o.label || "")}</span>` +
    (rank ? ` · ${esc(rank)}` : "") + `</div>` +
    (claims.length
      ? `<div class="explain">` +
        claims.map((c) => `<p>${esc(c.text)}</p>`).join("") + `</div>`
      : "") + `</div>`;
}

function ordinal(n) {
  return ["", "first", "second", "third", "fourth", "fifth", "sixth",
          "seventh", "eighth"][n] || `${n}th`;
}

function pvRow(label, pv, kind) {
  if (!pv || !pv.length) return "";
  // The continuation gives the game away as surely as the move does, so it
  // is folded until you ask for it, separately from the reasoning.
  if (!(revealed.lines || {})[kind]) {
    return `<div class="row small"><button data-act="reveal-line"` +
      ` data-kind="${kind}">Show the line (${pv.length} moves)</button></div>`;
  }
  const tag = kind === "best" ? "tag best-tag" : "tag";
  return `<div class="pv"><span class="${tag}">${label}</span>` + pv.map((m, i) =>
    `<button data-act="pv" data-kind="${kind}" data-i="${i}"` +
    `${pvCursor && pvCursor.kind === kind && pvCursor.index === i ? ' class="on"' : ""}>` +
    `${esc(m.san)}</button>`).join("") + `</div>`;
}

/** The panel in three groups: what to do with this position, how to get
    another, and the tools you reach for rarely. A button that cannot do
    anything here is not drawn at all -- a disabled row reads as a puzzle. */
function renderActions(d) {
  const rows = [group("This position")];
  if (!d.done) {
    rows.push(rowOf([btnHtml("show", "Show me the move", false, "primary")]));
  } else {
    // With rounds left the same position asks the next opponent move; with
    // none left this draws a fresh one, which is what "Random" does too, so
    // only one of the two is ever offered.
    const next = d.has_next_round ? "Next position" : "A new position";
    rows.push(rowOf([btnHtml("next", next, false, "primary")]));
    if (d.can_deeper) {
      rows.push(rowOf([btnHtml("deeper", "Drill on from here",
                               false, "wide")]));
    }
  }
  // Stepping through what has been played here, and starting over at two
  // different sizes: this move again, or the whole drill from its first
  // position.
  const hist = d.history || [];
  if (hist.length) {
    rows.push(rowOf([
      btnHtml("step-back", "←", cursorAt(d) === 0),
      btnHtml("step-fwd", "→", cursorAt(d) >= hist.length),
    ], "small steps"));
  }
  const small = [btnHtml("reset", "Replay move"),
                 btnHtml("restart", "Replay drill")];
  if (state.stack_depth > 1 || d.depth_level) {
    small.push(btnHtml("back", "← Back a level"));
  }
  rows.push(rowOf(small, "small"));
  rows.push(group("Another position"));
  const another = [];
  if (!d.done || d.has_next_round) {
    another.push(btnHtml("random", `Random ${MODE_NOUN[d.mode]}`));
  }
  another.push(btnHtml("menu", "Browse…"));
  rows.push(rowOf(another, "small"));
  rows.push(tools());
  el.actions.innerHTML = rows.join("");
}

function group(label) {
  return `<div class="sec-row">${esc(label)}</div>`;
}

/** Saving and hand-setting are occasional: reachable, never in the way. */
let toolsOpen = false;
function tools() {
  return `<details class="more"${toolsOpen ? " open" : ""}>` +
    `<summary>Tools</summary>` +
    rowOf([btnHtml("save", "Save this position"),
           btnHtml("editor", "Set up a position")], "small") + `</details>`;
}

document.addEventListener("toggle", (event) => {
  if (event.target.classList && event.target.classList.contains("more")) {
    toolsOpen = event.target.open;
  }
}, true);

function rowOf(buttons, cls) {
  return `<div class="row ${cls || ""}">${buttons.join("")}</div>`;
}

function btnHtml(act, label, disabled, cls) {
  return `<button class="${cls || ""}" data-act="${act}"` +
    `${disabled ? " disabled" : ""}>${label}</button>`;
}

function renderTree() {
  const rows = state.tree || [];
  const d = state.drill;
  const here = d && (d.node_id_current || d.node_id);
  el.tree.innerHTML = rows.map((r) => {
    const guide = r.depth
      ? `<span class="guide">${"│ ".repeat(Math.max(0, r.depth - 1))}└</span>` : "";
    const dot = r.tone ? `<span class="v ${r.tone}"></span>` : "";
    const visits = r.visits > 1 ? `<sup>${r.visits}</sup>` : "";
    return `<div class="tr ${r.id === here ? "here" : ""}" data-node="${r.id}">` +
      `${guide}<span>${esc(r.san || "start")}${visits}</span>${dot}</div>`;
  }).join("");
}

// --- move input ------------------------------------------------------------

function inputHandler(event) {
  const d = state && state.drill;
  if (!d || !d.can_answer) return false;
  const legal = d.legal || {};
  switch (event.type) {
    case INPUT_EVENT_TYPE.moveInputStarted: {
      const moves = legal[event.squareFrom];
      if (!moves || !moves.length) return false;
      // The library picks a piece up on the press, before the click arrives.
      // Remember whether this press is what picked it up, so the click that
      // follows knows a second click on the same piece puts it down again.
      pickedUpByPress = selected !== event.squareFrom;
      select(event.squareFrom);
      return true;
    }
    case INPUT_EVENT_TYPE.validateMoveInput: {
      if (submitting) return false;
      const moves = legal[event.squareFrom] || [];
      const hit = moves.find((m) => m.to === event.squareTo);
      if (!hit) return false;
      select(null);
      if (hit.promotion) { play(event.squareFrom, event.squareTo, true); return false; }
      submit(event.squareFrom, event.squareTo, null);
      return true;
    }
    case INPUT_EVENT_TYPE.moveInputCanceled:
      // A click leaves the piece selected; only a real cancel puts it down.
      return true;
    case INPUT_EVENT_TYPE.moveInputFinished:
      if (submitting) select(null);
      return true;
    default:
      return true;
  }
}

function submit(from, to, promotion) {
  submitting = true;
  // Let the library finish its own pointer sequence before standing it down,
  // or it complains about the square that is no longer under the cursor.
  setTimeout(() => moveInput(null), 0);
  shownFen = null;                 // the piece has already moved on screen
  call("/api/answer", { from, to, promotion })
    .finally(() => { submitting = false; });
  return true;
}

// --- walking a principal variation -----------------------------------------

async function walkPv(kind, index) {
  const live = state && state.drill;
  if (live && cursorAt(live) < (live.history || []).length) {
    stepCursor = { key: revealKey(live), at: null };
  }
  const a = state.drill && state.drill.answer;
  if (!a) return;
  const pv = kind === "best" ? a.explanation.best_pv : a.explanation.my_pv;
  const step = pv && pv[index];
  if (!step) return;
  pvCursor = { kind, index, step };
  moveInput(null);
  await setBoard(step.fen_after, true);
  markers([[step.uci.slice(0, 2), MARKER_MOVE], [step.uci.slice(2, 4), MARKER_MOVE]], true);
  board.removeArrows();
  el.status.textContent =
    `${step.san} — ${kind === "best" ? "the engine's line" : "your line"}.` +
    ` Drill from here starts at this position. Esc comes back.`;
  renderVerdict(state.drill);
}

async function leavePv() {
  if (!pvCursor) return;
  pvCursor = null;
  await renderDrill(state.drill);
}

// --- game walk -------------------------------------------------------------

async function renderGame(g) {
  moveInput(null);
  board.removeArrows();
  await orient(g.colour === "black" ? COLOR.black : COLOR.white);
  await setBoard(g.fen, true);
  const last = g.ply > 0 ? g.moves[g.ply - 1] : null;
  markers(last ? [[last.uci.slice(0, 2), MARKER_MOVE], [last.uci.slice(2, 4), MARKER_MOVE]] : [], true);
  document.body.dataset.turn = g.ply % 2 === 0 ? "white" : "black";
  let status = last ? `${last.number}${last.white ? "." : "..."} ${last.san}` : "Start of the game";
  if (last && last.verdict) {
    const who = last.is_me ? "you" : "they";
    status += ` \u2014 ${who} played ${esc(last.san)}: ${verdictLabel(last.verdict)}`;
    if (last.best_san && last.best_san !== last.san) status += `, best was ${last.best_san}`;
    if (last.mate_in) status += ` (mate in ${last.mate_in} ${last.kept_mate ? "kept" : "missed"})`;
  }
  el.status.textContent = status;

  el.context.innerHTML =
    `<div class="players">${playersLine(g)}</div>` +
    `<h1>${esc(g.white || "?")} – ${esc(g.black || "?")}</h1>` +
    `<div class="meta">${esc(g.result || "")} · ${g.moves.length} plies` +
    ` · working from ${g.colour}</div>`;
  el.progress.innerHTML = "";
  // What the review worked out about the engine's move here, if this was one
  // of yours and you did not find it.
  const why = last && last.why && last.why.length
    ? `<div class="card ${last.tone || ""}"><div class="explain">` +
      last.why.map((t) => `<p>${esc(t)}</p>`).join("") + `</div></div>` : "";
  el.verdict.innerHTML = why +
    (g.reviewed ? `<div class="hint">Reviewed at depth ${g.review_depth}` +
      (g.accuracy !== null ? ` \u00b7 your accuracy ${g.accuracy}%` : "") +
      `. Coloured marks are verdicts; click a move to see it.</div>` : "") +
    `<div class="movelist">` + g.moves.map((m, i) => {
    const num = m.white ? `<span class="num">${m.number}.</span>` : "";
    const mark = m.tone ? `<i class="v ${m.tone}"></i>` : "";
    return `${num}<span class="mv ${i + 1 === g.ply ? "on" : ""}" data-ply="${i + 1}">` +
      `${esc(m.san)}${mark}</span>`;
  }).join(" ") + `</div>`;
  // Picking a move of theirs means "let me answer that"; picking one of
  // yours means the same about the move they had just played. Either way the
  // drill is the reply, so both are offered.
  const drillable = last && last.review_id
    ? [`<button data-act="gdrill" data-game="${g.id}" data-ply="${g.ply}">` +
       (last.is_me ? "Drill this moment" : "Drill my reply to this") +
       `</button>`] : [];
  el.actions.innerHTML =
    group("This moment") +
    rowOf([btnHtml("gplay", "Play on from here", false, "primary")]) +
    (drillable.length ? rowOf(drillable) : "") +
    group("Move through the game") +
    rowOf([btnHtml("gstart", "⇤"), btnHtml("gprev", "←"),
           btnHtml("gnext", "→"), btnHtml("gend", "⇥"),
           btnHtml("gcolour", g.colour === "white" ? "As White" : "As Black")], "small") +
    rowOf([btnHtml("gclose", "← Leave this game")], "small") +
    `<details class="more"${toolsOpen ? " open" : ""}><summary>Tools</summary>` +
    rowOf([btnHtml("gsetup", "Set up from here")], "small") + `</details>`;
  el.tree.innerHTML = "";
  el.treeBox.hidden = true;
}

// --- menu, help, editor ----------------------------------------------------

async function openMenu() {
  const [groups, saved, games] = await Promise.all([
    call("/api/groups", {}), call("/api/saved", {}), call("/api/games", {})]);
  if (!groups) return;
  const gameRows = ((games && games.games) || []).slice(0, 60).map((g) => {
    const opp = g.my_colour === "white" ? g.black : g.white;
    const res = g.result === "1/2-1/2" ? "\u00bd"
      : ((g.my_colour === "white") === (g.result === "1-0") ? "won" : "lost");
    const acc = g.accuracy !== null && g.accuracy !== undefined ? `${g.accuracy}%` : "not reviewed";
    return `<div class="row-item find" data-act="open-game" data-id="${g.id}">` +
      `<span class="t">${esc(opp || "?")} <span class="meta">as ${g.my_colour}` +
      ` \u00b7 ${res}${g.opening ? " \u00b7 " + esc(g.opening) : ""}</span></span>` +
      `<span class="meta">${acc}</span></div>`;
  }).join("");
  const rows = (groups.groups || []).map((g) => {
    const sides = Object.entries(g.colours).filter(([c]) => c).map(([c, n]) =>
      `<button data-act="group" data-name="${esc(g.name)}" data-colour="${c}">` +
      `${c === "white" ? "White" : "Black"} <span class="meta">${n}</span></button>`).join("");
    return `<div class="row-item find"><span class="t">${esc(g.name)}</span>` +
      `<span class="sides">${sides}</span></div>`;
  }).join("");
  show(el.menu,
    `<button class="close ghost" data-act="close">✕</button>` +
    `<h2>${MODE_LABEL[state.mode]}</h2>` +
    `<input id="search" placeholder="Search" autocomplete="off">` +
    `<div class="rows">` +
    `<div class="row-item" data-act="random"><span class="t">Random position</span>` +
    `<span class="meta">weighted by what you miss</span></div>` +
    `<div class="row-item" data-act="editor"><span class="t">Set up a position</span>` +
    `<span class="meta">E</span></div></div>` +
    (saved && saved.saved.length
      ? `<div class="sec">Your positions</div><div class="rows">` +
        saved.saved.map((s) => `<div class="row-item find" data-act="saved" data-id="${s.id}">` +
          `<span class="t">${esc(s.name || "Saved position")}</span></div>`).join("") + `</div>`
      : "") +
    `<div class="sec">${state.mode === "openings" ? "Openings" : MODE_LABEL[state.mode]}</div>` +
    `<div class="rows">${rows || `<div class="row-item"><span class="meta">` +
      `Nothing here yet — ./run review</span></div>`}</div>` +
    (gameRows ? `<div class="sec">Your games</div><div class="rows">${gameRows}</div>` : ""));
  const search = $("search");
  search.focus();
  search.addEventListener("input", () => {
    const q = search.value.toLowerCase();
    el.menu.querySelectorAll(".row-item.find").forEach((row) => {
      row.hidden = !row.textContent.toLowerCase().includes(q);
    });
  });
}

/** Every game in the database, searchable, each one drillable on its own. */
let library = { games: [], q: "", elo: "", from: "", to: "" };

async function openLibrary() {
  const res = await call("/api/games", {});
  if (!res) return;
  library.games = res.games || [];
  show(el.library,
    `<button class="close ghost" data-act="close">✕</button>` +
    `<h2>Games played</h2>` +
    `<div class="filters"><div class="line">` +
    `<input id="lib-q" placeholder="Name, opening or result" autocomplete="off">` +
    `<input id="lib-elo" class="narrow" placeholder="Elo 1500-1700" autocomplete="off">` +
    `</div><div class="line">` +
    `<input id="lib-from" type="date" title="Played from">` +
    `<input id="lib-to" type="date" title="Played up to">` +
    `<button data-act="lib-clear">Clear</button></div>` +
    `<div class="line"><input id="lib-src" placeholder="Paste a chess.com link or PGN to analyse">` +
    `<button class="primary" data-act="analyse">Analyse</button></div></div>` +
    `<div class="count" id="lib-count"></div><div class="glist" id="lib-list"></div>`);
  ["lib-q", "lib-elo", "lib-from", "lib-to"].forEach((id) => {
    const box = $(id);
    box.value = { "lib-q": library.q, "lib-elo": library.elo,
                  "lib-from": library.from, "lib-to": library.to }[id];
    box.addEventListener("input", () => {
      library.q = $("lib-q").value; library.elo = $("lib-elo").value;
      library.from = $("lib-from").value; library.to = $("lib-to").value;
      drawLibrary();
    });
  });
  $("lib-q").focus();
  drawLibrary();
}

/** Two numbers anywhere in the box are read as a range; one as a floor. */
function eloRange(text) {
  const nums = (text.match(/\d+/g) || []).map(Number);
  if (!nums.length) return null;
  if (nums.length === 1) return [nums[0], 9999];
  return [Math.min(nums[0], nums[1]), Math.max(nums[0], nums[1])];
}

function libraryRows() {
  const q = library.q.trim().toLowerCase();
  const elo = eloRange(library.elo);
  const from = library.from ? Date.parse(library.from + "T00:00:00") / 1000 : null;
  const to = library.to ? Date.parse(library.to + "T23:59:59") / 1000 : null;
  return library.games.filter((g) => {
    const mine = g.my_colour === "white" ? g.white_elo : g.black_elo;
    const theirs = g.my_colour === "white" ? g.black_elo : g.white_elo;
    if (q) {
      const hay = [g.white, g.black, g.opening, g.result, g.time_class,
                   g.my_colour].join(" ").toLowerCase();
      if (!hay.includes(q)) return false;
    }
    if (elo) {
      const seen = [mine, theirs].filter((n) => n);
      if (!seen.some((n) => n >= elo[0] && n <= elo[1])) return false;
    }
    if (from && (g.played_at || 0) < from) return false;
    if (to && (g.played_at || 0) > to) return false;
    return true;
  });
}

function drawLibrary() {
  const rows = libraryRows();
  const focus = state && state.focus ? state.focus.id : null;
  $("lib-count").textContent =
    `${rows.length} of ${library.games.length} game${library.games.length === 1 ? "" : "s"}`;
  $("lib-list").innerHTML = rows.map((g) => {
    const opp = g.my_colour === "white" ? g.black : g.white;
    const oppElo = g.my_colour === "white" ? g.black_elo : g.white_elo;
    const mine = g.my_colour === "white" ? g.white_elo : g.black_elo;
    const res = g.result === "1/2-1/2" ? "drew"
      : ((g.my_colour === "white") === (g.result === "1-0") ? "won" : "lost");
    const date = g.played_at
      ? new Date(g.played_at * 1000).toISOString().slice(0, 10) : "";
    const reviewed = g.plies !== null && g.plies !== undefined;
    const acc = g.accuracy === null || g.accuracy === undefined
      ? (reviewed ? "analysed" : "not analysed") : `${g.accuracy}%`;
    const bits = [date, `as ${g.my_colour}`, res, acc,
                  `${g.positions} position${g.positions === 1 ? "" : "s"}`];
    if (g.blunders) bits.push(`${g.blunders} blunder${g.blunders === 1 ? "" : "s"}`);
    return `<div class="grow${focus === g.id ? " on" : ""}">` +
      `<div><div class="who">${esc(opp || "?")}` +
      `${oppElo ? ` <span class="meta">(${oppElo})</span>` : ""}` +
      `${mine ? ` <span class="meta">· you ${mine}</span>` : ""}</div>` +
      `<div class="sub">${esc(bits.join(" · "))}` +
      `${g.opening ? " · " + esc(g.opening) : ""}</div></div>` +
      `<div class="acts">` +
      (g.positions
        ? `<button class="primary" data-act="drill-game" data-id="${g.id}">Drill</button>`
        : "") +
      (reviewed ? "" :
        `<button data-act="analyse-game" data-id="${g.id}">Analyse</button>`) +
      `<button data-act="open-game" data-id="${g.id}">Open</button></div></div>`;
  }).join("") || `<div class="meta">No game matches that.</div>`;
}

/** Import a game and review it, then lock drilling onto it. */
async function startAnalysis(source, gameId) {
  const box = $("lib-src");
  const text = source || (box ? box.value.trim() : "");
  if (!gameId && !text) { toast("Paste a chess.com game link, or a PGN."); return; }
  const res = await call("/api/analyse",
    gameId ? { game_id: gameId } : { source: text });
  if (!res) return;
  if (box) box.value = "";
  closeOverlays();
  dismissedJob = null;
  if (state) { state.job = res; renderCards(state); }
}

async function openHelp() {
  const [stats, leaks] = await Promise.all([call("/api/stats", {}), call("/api/leaks", {})]);
  const a = (stats && stats.answers) || {};
  const rows = (leaks && leaks.positions) || [];
  show(el.help,
    `<button class="close ghost" data-act="close">✕</button>` +
    `<h2>Chess Trainer</h2>` +
    `<div class="sec">How it works</div>` +
    `<div class="meta">A position is set with the opponent to move. The engine` +
    ` plays one of the moves a real opponent might, in a random order that` +
    ` uses them all before repeating, and you find the best reply. Ask for` +
    ` more than one move and the opponent answers back, so you have to find a` +
    ` plan and not just a move. Right-click the board to draw arrows and` +
    ` circles, as on chess.com; any left click on the board wipes them.` +
    ` Clicking a piece you have already picked up puts it down.</div>` +
    `<div class="sec">Setting up a position</div>` +
    `<div class="meta">Press <b>E</b>. Nothing is in hand to begin with, so a` +
    ` click never drops a piece you did not ask for: take one from the` +
    ` palette, click squares to place it, and click it again (or the ✕ tile)` +
    ` to put it down. A click on an occupied square clears it, <b>Undo</b>` +
    ` (Ctrl+Z) walks back every change, and <b>Leave set-up</b> or Esc` +
    ` returns to the drill.</div>` +
    `<div class="sec">After you answer</div>` +
    `<div class="meta">The verdict alone at first: no move named, no arrow,` +
    ` no lines. One button explains what your move did, a second explains the` +
    ` engine's move and what your opponent's move was worth, and the` +
    ` continuations stay folded until you ask for them. The square your move` +
    ` landed on is coloured by the verdict: green for the best move, blue for` +
    ` a good one, then yellow, orange and red. <b>←</b> and <b>→</b> step` +
    ` back and forth through the moves played in the round.</div>` +
    `<div class="sec">One game at a time</div>` +
    `<div class="meta"><b>Games played</b> lists everything in the database,` +
    ` searchable by name, Elo range and date. Analyse a chess.com link or a` +
    ` pasted PGN there and it is reviewed, its positions extracted, and` +
    ` drilling locked to that game until you lift the lock with the ✕ on its` +
    ` card. <b>Engines warming</b>, top left, stops the background analysis` +
    ` at once when you would rather have the processor back.</div>` +
    `<div class="sec">Keys</div><table class="stats">` + [
      ["Enter", "Next position"], ["X", "Explain: your move, then theirs"],
      ["← →", "Step back and forward through this round"],
      ["O / B", "Browse positions"],
      ["G", "Games played"],
      ["W", "Engines warming on/off"], ["M", "Cycle mode"],
      ["E", "Set up a position"], ["S", "Save position"], ["Backspace", "Back"],
      ["R", "Replay this position"], ["D", "Drill from here"],
      ["Space", "Show me the move"], ["1 \u2013 5", "Pick the opponent's nth option"],
      ["Shift+1 \u2013 5", "Moves per position"],
      ["T", "Statistics"],
      ["? / H", "Help"], ["Esc", "Close"],
      ["← →", "In a game: step a move"],
      ["Home / End", "In a game: jump to start / end"],
    ].map(([k, v]) => `<tr><th><kbd>${k}</kbd></th><td>${v}</td></tr>`).join("") +
    `</table>` +
    `<div class="sec">Answers</div><table class="stats">` +
    [["answered", a.answers], ["best move", a.best], ["other top-five", a.good],
     ["inaccuracies", a.inaccuracy], ["mistakes", a.mistake], ["blunders", a.blunder],
     ["shown", a.shown], ["positions cached", stats && stats.cached_positions],
    ].map(([k, v]) => `<tr><th>${k}</th><td>${v || 0}</td></tr>`).join("") +
    `<tr><th>engine</th><td>${esc((stats && stats.engine) || "?")}</td></tr></table>` +
    `<div class="sec">Which move do I keep getting wrong</div>` +
    (rows.length
      ? `<table class="stats"><tr><th>group</th><th>you play</th><th>best</th>` +
        `<th>missed</th></tr>` + rows.map((r) =>
        `<tr><td>${esc(r.name || "—")}</td><td>${esc(r.you_play || "—")}</td>` +
        `<td>${esc(r.refuted_by || "—")}</td><td>${r.misses}/${r.attempts}</td></tr>`
      ).join("") + `</table>`
      : `<div class="meta">Nothing yet.</div>`) +
    `<div class="sec">How deep does it start</div>` +
    `<table class="stats"><tr><th>level</th><th>answers</th><th>best</th></tr>` +
    ((leaks && leaks.by_depth) || []).map((d) =>
      `<tr><td>${d.depth_level}</td><td>${d.attempts}</td>` +
      `<td>${d.accuracy === null ? 0 : d.accuracy}%</td></tr>`).join("") + `</table>`);
}

const editor = { pieces: {}, turn: "black", castling: "", hand: null,
                 open: false, history: [] };
const PIECE_ORDER = ["K", "Q", "R", "B", "N", "P", "k", "q", "r", "b", "n", "p"];
const PIECE_NAME = { k: "king", q: "queen", r: "rook", b: "bishop",
                     n: "knight", p: "pawn" };

function openEditor(fen) {
  editor.open = true;
  editor.pieces = {};
  editor.castling = "";
  editor.turn = "black";
  // Nothing in hand to begin with, so a stray click cannot scatter pawns
  // across the board. You pick a piece, then place it.
  editor.hand = null;
  editor.history = [];
  if (fen) seedFromFen(fen);
  // The drill's own buttons would still be sitting there otherwise, doing
  // something else entirely. While you are setting up, the panel is the
  // set-up.
  document.body.dataset.editing = "1";
  drawEditor();
}

/** Remember the board before a change, so it can be taken back. */
function editorRemember() {
  editor.history.push(JSON.stringify({
    pieces: editor.pieces, turn: editor.turn, castling: editor.castling,
  }));
  if (editor.history.length > 60) editor.history.shift();
}

function editorUndo() {
  const last = editor.history.pop();
  if (!last) return;
  const was = JSON.parse(last);
  editor.pieces = was.pieces;
  editor.turn = was.turn;
  editor.castling = was.castling;
  drawEditor();
}

function seedFromFen(fen) {
  // Reading a placement string is rendering, not rules: the server still makes
  // every judgement about whether the position is legal.
  const [placement, turn, castling] = fen.split(" ");
  let rank = 7, file = 0;
  for (const ch of placement) {
    if (ch === "/") { rank -= 1; file = 0; }
    else if (/\d/.test(ch)) file += Number(ch);
    else { editor.pieces["abcdefgh"[file] + (rank + 1)] = ch; file += 1; }
  }
  editor.turn = turn === "b" ? "black" : "white";
  editor.castling = castling && castling !== "-" ? castling : "";
}

function editorFen() {
  const rows = [];
  for (let r = 7; r >= 0; r -= 1) {
    let row = "", empty = 0;
    for (let f = 0; f < 8; f += 1) {
      const p = editor.pieces["abcdefgh"[f] + (r + 1)];
      if (p) { if (empty) { row += empty; empty = 0; } row += p; } else empty += 1;
    }
    if (empty) row += empty;
    rows.push(row);
  }
  return `${rows.join("/")} ${editor.turn === "black" ? "b" : "w"} ` +
    `${editor.castling || "-"} - 0 1`;
}

async function drawEditor() {
  el.editor.hidden = false;
  const holding = editor.hand
    ? `Placing ${editor.hand === editor.hand.toUpperCase() ? "white" : "black"}` +
      ` ${PIECE_NAME[editor.hand.toLowerCase()]}s — click a square. Click the` +
      ` piece again to put it down.`
    : `Nothing in hand: click a piece below to place it, or click a square` +
      ` on the board to clear it.`;
  el.editor.innerHTML =
    `<div class="sec-row">Setting up a position</div>` +
    `<div class="hint">${holding}</div>` +
    `<div class="palette">` +
    `<button data-act="hand" data-p="" class="erase ${editor.hand ? "" : "on"}"` +
    ` title="Nothing in hand: clicks clear a square">✕</button>` +
    PIECE_ORDER.map((p, i) =>
      (i === 6 ? `<span class="gap"></span>` : "") +
      `<button data-act="hand" data-p="${p}" class="${editor.hand === p ? "on" : ""}">` +
      `<svg viewBox="0 0 40 40"><use href="vendor/cm-chessboard/assets/pieces/standard.svg#` +
      `${spriteName(p)}"></use></svg></button>`).join("") +
    `</div><div class="hint"><b>To move</b> is the side your <b>opponent</b>` +
    ` takes: to move ` +
    `${editor.turn === "black" ? "Black means you play White" : "White means you play Black"}.` +
    `</div><div class="opts">` +
    `<button data-act="turn">To move: ${editor.turn === "black" ? "Black" : "White"}</button>` +
    ["K", "Q", "k", "q"].map((c) => `<button data-act="castle" data-c="${c}" ` +
      `class="${editor.castling.includes(c) ? "on" : ""}">${c}</button>`).join("") +
    `</div><div class="opts">` +
    `<button data-act="ed-undo"${editor.history.length ? "" : " disabled"}>` +
    `↶ Undo</button>` +
    `<button data-act="ed-empty">Clear the board</button>` +
    `<button data-act="ed-start">Start position</button></div>` +
    `<input id="ed-name" placeholder="Name (optional)">` +
    `<div class="row"><button class="primary" data-act="ed-go">Drill this position</button></div>` +
    `<div class="row small"><button data-act="ed-cancel">` +
    `← Leave set-up</button></div>`;
  moveInput(null);
  board.removeMarkers();
  board.removeArrows();
  el.status.textContent = "Setting up a position";
  // Await in order and read the position after the await: two quick clicks
  // then leave the board showing the later of the two, not the earlier.
  await orient(editor.turn === "white" ? COLOR.black : COLOR.white);
  shownFen = editorFen();
  await board.setPosition(shownFen, false);
}

function closeEditor() {
  editor.open = false;
  editor.hand = null;
  editor.history = [];
  el.editor.hidden = true;
  delete document.body.dataset.editing;
  board.disableSquareSelect();
  shownFen = null;
  if (state) apply(state);
}

async function editorGo() {
  const name = ($("ed-name") || {}).value || "";
  // Close first, then ask: closing after the round-trip would undo anything
  // opened in the meantime.
  editor.open = false;
  el.editor.hidden = true;
  delete document.body.dataset.editing;
  const data = await call("/api/editor/set", {
    pieces: editor.pieces, turn: editor.turn,
    castling: editor.castling || "-", name,
  });
  if (!data) {              // refused: stay in the editor with the reason
    editor.open = true;
    el.editor.hidden = false;
    document.body.dataset.editing = "1";
  } else {
    editor.hand = null;
    editor.history = [];
  }
}

function show(node, html) {
  closeOverlays();
  node.innerHTML = html;
  node.hidden = false;
}

function closeOverlays() {
  [el.menu, el.help, $("stats"), el.library].forEach((n) => { n.hidden = true; });
  el.modeList.hidden = true;
  el.chainList.hidden = true;
}

function anyOverlayOpen() {
  return !el.menu.hidden || !el.help.hidden || !$("stats").hidden
    || !el.library.hidden;
}

// --- events ----------------------------------------------------------------

// The library handles dragging. Clicking a piece and then clicking a square is
// handled here, from the same list of legal moves the server sent, so the two
// paths cannot race each other over one move.
el.board.addEventListener("click", (event) => {
  const square = squareFromEvent(event);
  const fresh = pickedUpByPress;
  pickedUpByPress = false;
  if (!square) return;
  // Your own arrows and circles are notes about the position in front of you,
  // so a left click on the board wipes them, the way lichess and chess.com do.
  // It happens here rather than on mousedown because clearing rebuilds the
  // marker layer: do that between press and release and the click that
  // follows lands on nothing, and the piece is never picked up.
  clearAnnotations();
  if (editor.open) return editorClick(square);
  clickToMove(square, fresh);
});

/** Drop every right-click arrow and circle. Ours -- the best-move arrow and
    the square markers -- are drawn by other code and stay. */
function clearAnnotations() {
  if (!board.setAnnotations) return;
  const drawn = board.getAnnotations();
  const mine = (item) => ANNOTATION_CLASS.test((item.type && item.type.class) || "");
  if (!drawn.arrows.some(mine) && !drawn.markers.some(mine)) return;
  board.setAnnotations({});
}

/** Place or clear one square. The board is not rebuilt: a full redraw would
    drop any click that lands while it is running. */
function editorClick(square) {
  const had = editor.pieces[square];
  // An empty square with nothing in hand is not a change, and must not eat
  // an undo step.
  if (!had && !editor.hand) return;
  editorRemember();
  if (had) {
    delete editor.pieces[square];
    board.setPiece(square, null);
  } else {
    editor.pieces[square] = editor.hand;
    board.setPiece(square, spriteName(editor.hand));
  }
  // Only the Undo button's state changes, so the board is left alone: a full
  // redraw here would drop a click that lands while it runs.
  const undo = el.editor.querySelector('[data-act="ed-undo"]');
  if (undo) undo.disabled = false;
}

/** "Q" -> "wq", "n" -> "bn": the board library's name for a piece. */
function spriteName(symbol) {
  return (symbol === symbol.toUpperCase() ? "w" : "b") + symbol.toLowerCase();
}

/** Which square the pointer landed on, or null for the space around it. */
function squareFromEvent(event) {
  const node = event.target.closest("[data-square]");
  return node ? node.getAttribute("data-square") : null;
}

function clickToMove(square, justPickedUp) {
  const d = state && state.drill;
  if (!d) return;
  // Standing in a past position: a click here would pick a piece up off a
  // board that is not the live one and play it there. Come back to the
  // present instead, which is what the click is really asking for.
  if (cursorAt(d) < (d.history || []).length) return stepTo(d, (d.history || []).length);
  if (!d.can_answer || submitting) return;
  const legal = d.legal || {};
  // Clicking the piece you already had up puts it down again.
  if (selected === square && !justPickedUp) return select(null);
  if (selected && selected !== square) {
    const move = (legal[selected] || []).find((m) => m.to === square);
    if (move) {
      const from = selected;
      // The library is mid-click on the same move; stand it down completely so
      // the two paths cannot both try to play it.
      moveInput(null);
      select(null);
      if (!move.promotion) board.movePiece(from, square, true);
      return void play(from, square, move.promotion);
    }
  }
  select(legal[square] && legal[square].length ? square : null);
}

/** Pick a piece up, or put it down. Redraws the dots either way. */
function select(square) {
  selected = square;
  board.removeMarkers(MARKER_SELECTED);
  board.removeLegalMovesMarkers();
  if (!square) return;
  const legal = (state.drill && state.drill.legal) || {};
  board.addMarker(MARKER_SELECTED, square);
  board.addLegalMovesMarkers((legal[square] || []).map((m) => ({ from: square, to: m.to })));
}

/** Play a move: ask for the promotion piece first when there is a choice. */
function play(from, to, promotion) {
  const d = state.drill;
  if (!promotion) return submit(from, to, null);
  board.showPromotionDialog(to, d.my_colour === "black" ? COLOR.black : COLOR.white,
    (result) => {
      if (result && result.type === PROMOTION_DIALOG_RESULT_TYPE.pieceSelected) {
        submit(from, to, result.piece.charAt(1));
      } else {
        setBoard(d.fen, false);
      }
    });
  return false;
}

document.addEventListener("click", async (event) => {
  const hit = event.target.closest(
    "[data-act], [data-node], [data-ply], [data-set-mode], [data-set-chain]");
  if (!hit) {
    if (anyOverlayOpen() && !event.target.closest(".overlay") &&
        !event.target.closest("#top")) closeOverlays();
    return;
  }
  if (hit.dataset.act === "bar-help") {
    const why = hit.parentElement.querySelector(".bar-help");
    if (why) why.hidden = !why.hidden;
    return;
  }
  if (hit.dataset.setChain) {
    closeOverlays();
    shownKey = null;
    return void call("/api/chain", { moves: Number(hit.dataset.setChain) });
  }
  if (hit.dataset.setMode) {
    closeOverlays();
    shownKey = null;
    return void call("/api/mode", { mode: hit.dataset.setMode });
  }
  if (hit.dataset.node) { shownKey = null; return void call("/api/goto", { node_id: Number(hit.dataset.node) }); }
  if (hit.dataset.ply !== undefined && hit.dataset.act === undefined) {
    return void call("/api/game/goto", { ply: Number(hit.dataset.ply) });
  }
  const g = state && state.game;
  switch (hit.dataset.act) {
    case "close": return closeOverlays();
    case "menu": return openMenu();
    case "editor": closeOverlays(); return openEditor(state.drill ? state.drill.fen : null);
    case "random": closeOverlays(); shownKey = null; return void call("/api/random", {});
    case "saved": closeOverlays(); shownKey = null;
      return void call("/api/drill", { saved_id: Number(hit.dataset.id) });
    case "group": closeOverlays(); shownKey = null;
      return void call("/api/drill", { name: hit.dataset.name, colour: hit.dataset.colour });
    case "show": return void call("/api/show", {});
    case "next": shownKey = null; return void call("/api/next", {});
    case "deeper": {
      shownKey = null;
      const step = pvCursor && pvCursor.step;
      // Drill the position that is on the board -- which, after walking into
      // a line, is not the position you answered from.
      return void call("/api/deeper", step
        ? { fen: step.fen_after, prev_fen: step.fen_before, last_move: step.uci }
        : {});
    }
    case "reset": shownKey = null; return void call("/api/replay_move", {});
    case "back": shownKey = null; return void call("/api/back", {});
    case "save": {
      const name = window.prompt("Name this position",
        (state.drill && state.drill.name) || "");
      if (name === null) return;
      if (await call("/api/save", { name })) toast("Saved.");
      return;
    }
    case "fen": {
      const d = state.drill;
      const fen = (d && (d.answer ? d.answer.fen_after : d.fen)) || "";
      try {
        await navigator.clipboard.writeText(fen);
        toast(`Copied: ${fen}`);
      } catch (err) {
        window.prompt("Copy this position", fen);
      }
      return;
    }
    case "stats": return openStats();
    case "stats-tab": return openStats(hit.dataset.tab);
    case "drill": closeOverlays(); shownKey = null;
      return void call("/api/drill/review", { id: Number(hit.dataset.id) });
    case "playout": closeOverlays(); shownKey = null;
      return void call("/api/drill/review", { id: Number(hit.dataset.id), play_out: true });
    case "pv": return walkPv(hit.dataset.kind, Number(hit.dataset.i));
    case "hand": {
      const want = hit.dataset.p || null;
      // Clicking the piece you are holding puts it down again.
      editor.hand = editor.hand === want ? null : want;
      return drawEditor();
    }
    case "ed-undo": return editorUndo();
    case "turn": editorRemember();
      editor.turn = editor.turn === "black" ? "white" : "black";
      return drawEditor();
    case "castle": {
      editorRemember();
      const c = hit.dataset.c;
      editor.castling = editor.castling.includes(c)
        ? editor.castling.replace(c, "") : editor.castling + c;
      return drawEditor();
    }
    case "ed-empty": editorRemember(); editor.pieces = {}; editor.castling = "";
      return drawEditor();
    case "ed-start": editorRemember();
      seedFromFen("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1");
      editor.turn = "black"; return drawEditor();
    case "ed-cancel": return closeEditor();
    case "ed-go": return editorGo();
    case "gplay": shownKey = null; return void call("/api/game/play", {});
    case "gprev": return void call("/api/game/goto", { ply: g.ply - 1 });
    case "gnext": return void call("/api/game/goto", { ply: g.ply + 1 });
    case "gstart": return void call("/api/game/goto", { ply: 0 });
    case "gend": return void call("/api/game/goto", { ply: g.moves.length });
    case "gcolour": return void call("/api/game/colour",
      { colour: g.colour === "white" ? "black" : "white" });
    case "gsetup": {
      const data = await call("/api/game/setup", {});
      if (data) openEditor(data.fen);
      return;
    }
    case "gclose": shownKey = null; return void call("/api/random", {});
    case "open-game": closeOverlays(); shownKey = null;
      return void call("/api/game/open", { id: Number(hit.dataset.id) });
    case "gdrill": closeOverlays(); shownKey = null;
      return void call("/api/drill/review", hit.dataset.game
        ? { game_id: Number(hit.dataset.game), ply: Number(hit.dataset.ply) }
        : { id: Number(hit.dataset.id) });
    case "drill-game": closeOverlays(); shownKey = null;
      dismissedJob = state.job ? jobKey(state.job) : null;
      call("/api/analyse/dismiss", {});
      return void call("/api/focus", { game_id: Number(hit.dataset.id), draw: true });
    case "unlock": shownKey = null;
      return void call("/api/focus", { game_id: null });
    case "job-dismiss":
      dismissedJob = state.job ? jobKey(state.job) : null;
      renderCards(state);
      return void call("/api/analyse/dismiss", {});
    case "step-back": return stepTo(state.drill, cursorAt(state.drill) - 1);
    case "step-fwd": return stepTo(state.drill, cursorAt(state.drill) + 1);
    case "reveal-line": {
      const open = revealState(state.drill);
      open.lines[hit.dataset.kind] = true;
      return renderVerdict(state.drill);
    }
    case "restart": shownKey = null; return void call("/api/restart", {});
    case "reveal-cost":
      revealState(state.drill).cost = true;
      revealed.cost = true;
      return renderVerdict(state.drill);
    case "reveal-best":
      revealState(state.drill).best = true;
      revealed.best = true;
      shownKey = null;
      return void renderDrill(state.drill);
    case "analyse": return startAnalysis();
    case "analyse-game":
      return void startAnalysis(null, Number(hit.dataset.id));
    case "lib-clear":
      library = { games: library.games, q: "", elo: "", from: "", to: "" };
      return openLibrary();
    case "library": return openLibrary();
    default: return;
  }
});

$("btn-help").addEventListener("click", openHelp);
$("btn-library").addEventListener("click", openLibrary);
el.warmBtn.addEventListener("click", () => {
  const on = el.warmBtn.getAttribute("aria-pressed") === "true";
  renderWarming(!on);                     // the switch answers at once
  call("/api/warming", { on: !on });
});
$("btn-stats").addEventListener("click", () => openStats());
$("btn-game").addEventListener("click", async () => {
  const url = window.prompt("chess.com game link, or paste a PGN");
  if (!url) return;
  shownKey = null;
  await call("/api/game/load", { url });
});
el.modeBtn.addEventListener("click", (event) => {
  event.stopPropagation();
  el.chainList.hidden = true;
  el.modeList.hidden = !el.modeList.hidden;
});
el.chainBtn.addEventListener("click", (event) => {
  event.stopPropagation();
  el.modeList.hidden = true;
  el.chainList.hidden = !el.chainList.hidden;
});

document.addEventListener("keydown", (event) => {
  const key = event.key;
  // Escape works from anywhere, the search box included: it is how you
  // leave whatever is open.
  if (key === "Escape") {
    if (anyOverlayOpen()) { event.preventDefault(); return closeOverlays(); }
    if (editor.open) { event.preventDefault(); return closeEditor(); }
    if (pvCursor) { event.preventDefault(); return void leavePv(); }
    return;
  }
  if (key.toLowerCase() === "z" && (event.ctrlKey || event.metaKey) && editor.open) {
    event.preventDefault();
    return editorUndo();
  }
  if (/^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)) return;
  if (editor.open) return;
  const g = state && state.game;
  const d = state && state.drill;
  if (!g && d && (key === "ArrowLeft" || key === "ArrowRight")) {
    event.preventDefault();
    return stepTo(d, cursorAt(d) + (key === "ArrowLeft" ? -1 : 1));
  }
  if (g) {
    const map = { ArrowLeft: g.ply - 1, ArrowRight: g.ply + 1, Home: 0, End: g.moves.length };
    if (key in map) { event.preventDefault(); return void call("/api/game/goto", { ply: map[key] }); }
  }
  const shifted = { "!": 1, "@": 2, "#": 3, "$": 4, "%": 5 }[key];
  if (shifted) { event.preventDefault(); return void call("/api/chain", { moves: shifted }); }
  switch (key.toLowerCase()) {
    case "enter": event.preventDefault(); shownKey = null;
      return void call(g ? "/api/game/play" : "/api/next", {});
    case " ": if (d && d.can_answer) { event.preventDefault(); return void call("/api/show", {}); }
      return;
    case "o": case "b": event.preventDefault(); return openMenu();
    case "1": case "2": case "3": case "4": case "5":
      event.preventDefault();
      // Plain digits pick the opponent's nth option; with Shift they set how
      // many moves in a row a position asks for.
      if (event.shiftKey) return void call("/api/chain", { moves: Number(key) });
      if (d) { shownKey = null; return void call("/api/select", { index: Number(key) - 1 }); }
      return;
    case "m": {
      event.preventDefault();
      const order = ["openings", "middlegame", "endgame"];
      shownKey = null;
      return void call("/api/mode",
        { mode: order[(order.indexOf(state.mode) + 1) % order.length] });
    }
    case "e": event.preventDefault(); return openEditor(d ? d.fen : null);
    case "s": event.preventDefault();
      return void (document.querySelector('[data-act="save"]') || {}).click?.();
    case "d": {
      if (!d || !d.can_deeper) return;
      event.preventDefault();
      shownKey = null;
      const step = pvCursor && pvCursor.step;
      return void call("/api/deeper", step
        ? { fen: step.fen_after, prev_fen: step.fen_before, last_move: step.uci }
        : {});
    }
    case "backspace": event.preventDefault(); shownKey = null;
      return void call("/api/back", {});
    case "r": event.preventDefault(); shownKey = null; return void call("/api/reset", {});
    case "?": case "h": event.preventDefault(); return openHelp();
    case "x": {
      event.preventDefault();
      if (!d || !d.answer) return;
      const open = revealState(d);
      if (!open.cost) { open.cost = true; return renderVerdict(d); }
      if (!open.best) { open.best = true; shownKey = null; return void renderDrill(d); }
      return;
    }
    case "t": event.preventDefault(); return openStats();
    case "g": event.preventDefault(); return openLibrary();
    case "w": event.preventDefault(); return void el.warmBtn.click();
    default: break;
  }
});


// --- statistics ---------------------------------------------------------------

const PHASE_LABEL = { opening: "Opening", middlegame: "Middlegame", endgame: "Endgame" };

async function openStats(tab) {
  tab = tab || openStats.last || "games";
  openStats.last = tab;
  const data = await call(tab === "games" ? "/api/stats/games" : "/api/stats/gym", {});
  if (!data) return;
  const el2 = $("stats");
  const tabs = `<div class="tabs">` +
    `<button data-act="stats-tab" data-tab="games" class="${tab === "games" ? "on" : ""}">Your games</button>` +
    `<button data-act="stats-tab" data-tab="gym" class="${tab === "gym" ? "on" : ""}">Gym</button>` +
    `<button class="close ghost" data-act="close">✕</button></div>`;
  show(el2, tabs + `<div class="body">` +
    (tab === "games" ? gamesHtml(data) : gymHtml(data)) + `</div>`);
}

function tile(v, k) {
  return `<div class="tile"><div class="v">${v === null || v === undefined ? "—" : v}</div>` +
    `<div class="k">${k}</div></div>`;
}

function bar(label, value, n, bad, help) {
  const v = value === null || value === undefined ? 0 : value;
  const name = help
    ? `<button class="bar-label" data-act="bar-help" title="What this counts">${esc(label)}</button>`
    : `<span>${esc(label)}</span>`;
  const why = help ? `<div class="bar-help" hidden>${esc(help)}</div>` : "";
  return `<div class="bar">${name}` +
    `<div class="track"><div class="fill ${bad ? "bad" : ""}" style="width:${Math.max(0, Math.min(100, v))}%"></div></div>` +
    `<span class="n">${value === null || value === undefined ? "—" : v + "%"}` +
    `${n !== undefined ? ` · ${n}` : ""}</span>${why}</div>`;
}

/** A radar of hit rates per theme. Axes with few samples are drawn thin. */
function radar(rows, valueKey) {
  rows = (rows || []).filter((r) => r.n > 0);
  if (rows.length < 3) return "";
  // Wide enough that the longest label sits inside the drawing on either side.
  const W = 560, H = 420, cx = W / 2, cy = H / 2, R = 130;
  const n = rows.length;
  const angle = (i) => -Math.PI / 2 + (2 * Math.PI * i) / n;
  const pt = (i, v) => [cx + R * (v / 100) * Math.cos(angle(i)),
                        cy + R * (v / 100) * Math.sin(angle(i))];
  let svg = `<svg class="radar" viewBox="0 0 ${W} ${H}" role="img" aria-label="radar">`;
  for (const ring of [25, 50, 75, 100]) {
    const pts = rows.map((_, i) => pt(i, ring).join(",")).join(" ");
    svg += `<polygon class="ring" points="${pts}"></polygon>`;
    svg += `<text class="tick" x="${cx + 3}" y="${cy - R * ring / 100 - 2}">${ring}</text>`;
  }
  rows.forEach((_, i) => {
    const [x, y] = pt(i, 100);
    svg += `<line class="axis" x1="${cx}" y1="${cy}" x2="${x}" y2="${y}"></line>`;
  });
  const poly = rows.map((r, i) => pt(i, r[valueKey] || 0).join(",")).join(" ");
  svg += `<polygon class="area" points="${poly}"></polygon>`;
  rows.forEach((r, i) => {
    const [x, y] = pt(i, r[valueKey] || 0);
    svg += `<circle class="pt" cx="${x}" cy="${y}" r="3.5"></circle>`;
    const [lx, ly] = pt(i, 118);
    const anchor = Math.abs(Math.cos(angle(i))) < 0.2 ? "middle"
      : (Math.cos(angle(i)) > 0 ? "start" : "end");
    svg += `<text class="lab ${r.n < 5 ? "thin" : ""}" x="${lx}" y="${ly + 4}" text-anchor="${anchor}">` +
      `${esc(r.label)} <tspan class="tick">${r.n}</tspan></text>`;
  });
  return svg + `</svg>`;
}

function spark(values) {
  values = (values || []).filter((v) => v !== null && v !== undefined);
  if (values.length < 2) return "";
  const w = 600, h = 60, pad = 4;
  const x = (i) => pad + (i * (w - 2 * pad)) / (values.length - 1);
  const y = (v) => h - pad - ((v / 100) * (h - 2 * pad));
  const d = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">` +
    `<line class="base" x1="0" x2="${w}" y1="${y(50)}" y2="${y(50)}"></line><path d="${d}"></path></svg>`;
}

function moment(m, act, extra) {
  const who = m.my_colour === "white"
    ? `you vs ${esc(m.black)}` : `${esc(m.white)} vs you`;
  return `<div class="moment"><div><span class="t">${esc(m.san || "?")}</span>` +
    (m.best_san && m.best_san !== m.san ? ` <span class="m">best ${esc(m.best_san)}</span>` : "") +
    `<div class="m">move ${Math.ceil(m.ply / 2)} · ${who}${extra ? " · " + extra : ""}</div></div>` +
    `<button data-act="${act}" data-id="${m.id}">${act === "playout" ? "Play it out" : "Drill"}</button></div>`;
}

function gamesHtml(d) {
  const cov = d.coverage || {};
  let html = "";
  if (!cov.reviewed) {
    return `<div><h3>Your games</h3><div class="hint">None of your ${cov.games || 0} games` +
      ` are reviewed yet. Run <code>./run review</code>; it is resumable and each` +
      ` game takes under a minute.</div></div>`;
  }
  const o = d.overall || {};
  html += `<div><div class="hint">${cov.reviewed} of ${cov.games} games reviewed` +
    (cov.reviewed < cov.games ? ` — <code>./run review</code> for the rest` : "") +
    `.</div></div>`;
  html += `<div class="tiles">${tile(o.accuracy, "accuracy")}` +
    `${tile(o.blunders_per_game, "blunders / game")}${tile(o.mistakes_per_game, "mistakes / game")}` +
    `${tile(o.inaccuracies_per_game, "inaccuracies / game")}</div>`;
  if ((d.by_colour || []).length) {
    html += `<div><h3>By colour</h3><div class="bars">` + d.by_colour.map((c) =>
      bar(`${c.colour} · score`, c.score, `${c.games} games`) +
      bar(`${c.colour} · accuracy`, c.accuracy)).join("") + `</div></div>`;
  }
  html += `<div><h3>By phase</h3><div class="bars">` + (d.by_phase || []).map((p) =>
    bar(`${PHASE_LABEL[p.phase]} · accuracy`, p.accuracy, `${p.moves} moves`) +
    bar(`${PHASE_LABEL[p.phase]} · blunders`, p.blunder_rate, undefined, true)).join("") +
    `</div></div>`;
  html += `<div><h3>What you find, and what you miss</h3>` +
    `<div class="hint">When the engine's move was about a theme, how often you played it.` +
    ` Thin labels have fewer than five samples. Click a name to see what it counts.</div>` +
    `<div class="radar-wrap">${radar(d.themes, "hit_rate")}<div class="bars">` +
    (d.themes || []).map((t) => bar(t.label, t.hit_rate, `${t.n}`, false, t.help)).join("") + `</div></div></div>`;
  if ((d.allowed || []).length) {
    html += `<div><h3>What your mistakes allowed</h3>` +
      `<div class="hint">After a mistake or blunder of yours, what the engine's reply for your opponent was about.` +
      ` A hanging piece here is one you left for them to take.</div><div class="bars">` +
      d.allowed.slice(0, 8).map((a) => bar(a.label, Math.round(Math.min(100, a.n * 100 / d.allowed[0].n)), `${a.n} times`, true)).join("") +
      `</div></div>`;
  }
  const ms = d.mates_summary || {};
  if (ms.had) {
    html += `<div><h3>Mates you had</h3><div class="hint">${ms.found || 0} of ${ms.had} forced mates` +
      ` (within 11) were played. Play one out: you must find every move.</div><div class="moments">` +
      (d.mates || []).slice(0, 12).map((m) => moment(m, "playout",
        `mate in ${m.mate_in}${m.kept_mate ? ", found" : ", missed"}`)).join("") + `</div></div>`;
  }
  if ((d.worst || []).length) {
    html += `<div><h3>Your worst moments</h3><div class="moments">` +
      d.worst.slice(0, 10).map((w) => moment(w, "drill",
        `${w.delta_wp.toFixed(0)} points · ${(w.themes || []).join(", ") || w.phase}`)).join("") +
      `</div></div>`;
  }
  if ((d.openings || []).length) {
    html += `<div><h3>Openings</h3><div class="hint">Score, accuracy, and your winning chances` +
      ` at move 12 — where the opening leaves you.</div><table class="stats">` +
      `<tr><th>opening</th><th>as</th><th>games</th><th>score</th><th>accuracy</th><th>at move 12</th></tr>` +
      d.openings.map((o) => `<tr><td>${esc(o.name || "?")}</td><td>${o.colour}</td><td>${o.games}</td>` +
        `<td>${o.score === null ? "—" : o.score + "%"}</td><td>${o.accuracy ?? "—"}</td>` +
        `<td>${o.wp_at_12 === null || o.wp_at_12 === undefined ? "—" : o.wp_at_12 + "%"}</td></tr>`).join("") +
      `</table></div>`;
  }
  if ((d.trend || []).length > 1) {
    html += `<div><h3>Accuracy over your last ${d.trend.length} games</h3>${spark(d.trend.map((t) => t.accuracy))}</div>`;
  }
  return html;
}

function gymHtml(d) {
  const o = d.overall || {};
  if (!o.answers) return `<div class="hint">No answers yet.</div>`;
  let html = `<div class="tiles">${tile(o.answers, "answers")}` +
    `${tile(Math.round(100 * (o.best || 0) / o.answers) + "%", "best move")}` +
    `${tile(o.blunder || 0, "blunders")}${tile(o.shown || 0, "shown")}</div>`;
  const section = (title, rows, labeller, hint) => {
    if (!(rows || []).length) return "";
    return `<div><h3>${title}</h3>${hint ? `<div class="hint">${hint}</div>` : ""}<div class="bars">` +
      rows.map((r) => bar(labeller(r), r.best_rate, `${r.n}`)).join("") + `</div></div>`;
  };
  html += section("Best move by phase", d.by_phase, (r) => PHASE_LABEL[r.key] || r.key);
  html += section("By move of a chain", d.by_step, (r) => `move ${r.key}`,
    "Does your accuracy hold up once you have to follow a plan through?");
  html += section("By drill-from-here level", d.by_level, (r) => `level ${r.key}`,
    "Where you leave memory and start calculating.");
  html += `<div><h3>By theme</h3><div class="hint">How often you found the move when the` +
    ` position was about a theme.</div><div class="radar-wrap">${radar(d.themes, "hit_rate")}` +
    `<div class="bars">` + (d.themes || []).map((t) => bar(t.label, t.hit_rate, `${t.n}`, false, t.help)).join("") +
    `</div></div></div>`;
  html += section("By opening", d.by_opening, (r) => r.key || "?");
  if ((d.recent || []).length) {
    html += `<div><h3>Last ${d.recent.length} answers</h3><div class="form">` +
      d.recent.map((v) => `<span class="${v}"></span>`).join("") + `</div></div>`;
  }
  return html;
}

function verdictLabel(v) {
  return ({ best: "best move", second: "second best", third: "third best",
    fourth: "fourth best", fifth: "fifth best", good: "good move",
    inaccuracy: "inaccuracy", mistake: "mistake", blunder: "blunder",
    missed_mate: "missed mate", shown: "shown" })[v] || v;
}

function esc(text) {
  return String(text === null || text === undefined ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Handy from the browser console when something looks wrong.
window.__trainer = { board, editor, get state() { return state; },
                     get selected() { return selected; },
                     get shownFen() { return shownFen; } };

// A drill is already running when the page loads.
call("/api/state");
