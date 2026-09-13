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
  modeBtn: $("btn-mode"), modeList: $("mode-list"), pick: $("pick"),
};

const MODE_LABEL = { openings: "Openings", middlegame: "Middlegame", endgame: "Endgame" };
const MODE_NOUN = { openings: "opening", middlegame: "middlegame", endgame: "endgame" };
const MARKER_MOVE = { class: "marker-square-move", slice: "markerSquare" };
const MARKER_BEST = { class: "marker-square-best", slice: "markerSquare" };
const MARKER_PLAYED = { class: "marker-square-played", slice: "markerSquare" };
const MARKER_SELECTED = { class: "marker-square-selected", slice: "markerSquare" };
const ARROW_BEST = { class: "arrow-best" };

let state = null;
let shownKey = null;      // which round the board is currently showing
let shownFen = null;
let busy = 0;
let pvCursor = null;      // {kind, index} while walking a principal variation
let submitting = false;   // one move per turn, however it was entered
let selected = null;      // the square whose piece is picked up

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
  el.modeList.innerHTML = (data.modes || []).map((m) =>
    `<li data-set-mode="${m.mode}" class="${m.active ? "on" : ""}">` +
    `<span>${MODE_LABEL[m.mode]}</span>` +
    `<span class="n">${m.count} position${m.count === 1 ? "" : "s"}</span></li>`
  ).join("");
  if (data.game) await renderGame(data.game);
  else await renderDrill(data.drill);
}

async function renderDrill(d) {
  el.treeBox.hidden = false;
  if (!d) {
    board.disableMoveInput();
    await setBoard(FEN.start, false);
    document.body.dataset.turn = "white";
    el.status.textContent = "";
    el.context.innerHTML = `<h1>Nothing in this pool yet</h1>` +
      `<div class="meta">Import your games, then build the pools.</div>` +
      `<div class="tail">./run import --player &lt;name&gt;<br>./run phases --build</div>`;
    el.progress.innerHTML = "";
    el.verdict.innerHTML = "";
    el.actions.innerHTML = rowOf([btnHtml("menu", "Open the menu"),
                                  btnHtml("editor", "Set up a position")]);
    el.tree.innerHTML = "";
    return;
  }

  const a = d.answer;
  selected = null;
  const key = `${d.node_id_current}|${d.opp_move}|${d.round}|${d.depth_level}`;
  const target = a ? a.fen_after : d.fen;

  board.disableMoveInput();
  await orient(d.my_colour === "black" ? COLOR.black : COLOR.white);
  if (!a && key !== shownKey && d.base_fen) {
    // A new round: show the position they moved from, then play their move.
    await setBoard(d.base_fen, false);
    markers([[d.opp_move.slice(0, 2), MARKER_MOVE]], true);
    await pause(180);
    await setBoard(d.fen, true);
  } else {
    await setBoard(target, shownFen !== null && shownFen !== target);
  }
  shownKey = key;

  markers(markersFor(d), true);
  board.removeArrows();
  if (a && a.verdict !== "best" && a.best_move) {
    board.addArrow(ARROW_BEST, a.best_move.slice(0, 2), a.best_move.slice(2, 4));
    markers([[a.best_move.slice(0, 2), MARKER_BEST],
             [a.best_move.slice(2, 4), MARKER_BEST]], false);
  }

  document.body.dataset.turn = d.my_colour;
  if (d.can_answer) {
    board.enableMoveInput(inputHandler, d.my_colour === "black" ? COLOR.black : COLOR.white);
  }

  el.status.textContent = d.can_answer
    ? `They played ${d.opp_san}. Your move.`
    : (a ? `You played ${a.my_san || "—"}.` : `They played ${d.opp_san}.`);

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

async function setBoard(fen, animated) {
  if (fen === shownFen && animated) return;
  shownFen = fen;
  await board.setPosition(fen, !!animated);
}

function pause(ms) { return new Promise((r) => setTimeout(r, ms)); }

function markers(list, clear) {
  if (clear) board.removeMarkers();
  list.forEach(([square, type]) => board.addMarker(type, square));
}

function markersFor(d) {
  const out = [];
  const a = d.answer;
  if (d.opp_move) {
    out.push([d.opp_move.slice(0, 2), MARKER_MOVE]);
    out.push([d.opp_move.slice(2, 4), MARKER_MOVE]);
  }
  if (a && a.my_move) {
    const tone = a.verdict === "best" ? MARKER_BEST : MARKER_PLAYED;
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
  const res = g.result ? ` · ${esc(g.result)}` : "";
  if (g.my_colour === "white")
    return `<span class="you">You</span> (White${g.white_elo ? `, ${g.white_elo}` : ""}) vs ${b}${res}`;
  if (g.my_colour === "black")
    return `${w} vs <span class="you">you</span> (Black${g.black_elo ? `, ${g.black_elo}` : ""})${res}`;
  return `${w} vs ${b}${res} · not your game`;
}

function renderProgress(d) {
  const done = d.round_results || [];
  const dots = [];
  for (let i = 0; i < d.rounds; i += 1) {
    const tone = done[i] || "";
    const now = i + 1 === d.round && !d.answer ? " now" : "";
    dots.push(`<span class="dot ${tone}${now}"></span>`);
  }
  el.progress.innerHTML =
    `<span>Position ${d.round} of ${d.rounds}</span>` +
    `<span class="dots">${dots.join("")}</span>`;
}

function renderVerdict(d) {
  const a = d.answer;
  if (!a) {
    el.verdict.innerHTML =
      `<div class="ask">They played <b>${esc(d.opp_san)}</b>. ` +
      `Find the best reply for ${d.my_colour}.</div>`;
    return;
  }
  const ex = a.explanation || {};
  const cost = a.delta_wp === null || a.delta_wp === undefined ? ""
    : `${a.delta_wp.toFixed(1)} points lost`;
  const head = a.verdict === "shown"
    ? `<span class="move">${esc(a.best_san)}</span><span class="label">Shown</span>`
    : `<span class="move">${esc(a.my_san)}</span><span class="label">${esc(a.label)}</span>`;
  el.verdict.innerHTML =
    `<div class="card ${a.tone}">` +
    `<div class="head">${head}<span class="cost">${cost}</span></div>` +
    (a.detail ? `<div class="detail">${esc(a.detail)}</div>` : "") +
    (a.verdict !== "best" && a.verdict !== "shown" && a.best_san
      ? `<div class="detail">Best was <b>${esc(a.best_san)}</b>` +
        (a.wp_best !== null && a.wp_best !== undefined
          ? ` · ${a.wp_best.toFixed(0)}% vs ${(a.wp_mine ?? 0).toFixed(0)}%` : "") +
        `</div>`
      : "") +
    (ex.text ? `<div class="explain">${esc(ex.text)}</div>` : "") +
    `<div class="lines">${pvRow("Yours", ex.my_pv, "mine")}` +
    `${pvRow("Best", ex.best_pv, "best")}</div></div>`;
}

function pvRow(label, pv, kind) {
  if (!pv || !pv.length) return "";
  const tag = kind === "best" ? "tag best-tag" : "tag";
  return `<div class="pv"><span class="${tag}">${label}</span>` + pv.map((m, i) =>
    `<button data-act="pv" data-kind="${kind}" data-i="${i}"` +
    `${pvCursor && pvCursor.kind === kind && pvCursor.index === i ? ' class="on"' : ""}>` +
    `${esc(m.san)}</button>`).join("") + `</div>`;
}

function renderActions(d) {
  const a = d.answer;
  const rows = [];
  if (!a) {
    rows.push(rowOf([btnHtml("show", "Show me the move", false, "primary")]));
  } else {
    const next = d.has_next_round
      ? "Next position" : `Drill another ${MODE_NOUN[d.mode]}`;
    const main = [btnHtml("next", next, false, "primary")];
    if (d.can_deeper) main.unshift(btnHtml("deeper", "Drill from here"));
    rows.push(rowOf(main));
  }
  rows.push(rowOf([
    btnHtml("back", "← Back", state.stack_depth <= 1 && !d.depth_level),
    btnHtml("reset", "Replay"),
    btnHtml("save", "Save"),
    btnHtml("editor", "Set up"),
  ], "small"));
  el.actions.innerHTML = rows.join("");
}

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
  setTimeout(() => board.disableMoveInput(), 0);
  shownFen = null;                 // the piece has already moved on screen
  call("/api/answer", { from, to, promotion })
    .finally(() => { submitting = false; });
  return true;
}

// --- walking a principal variation -----------------------------------------

async function walkPv(kind, index) {
  const a = state.drill && state.drill.answer;
  if (!a) return;
  const pv = kind === "best" ? a.explanation.best_pv : a.explanation.my_pv;
  const step = pv && pv[index];
  if (!step) return;
  pvCursor = { kind, index };
  board.disableMoveInput();
  await setBoard(step.fen_after, true);
  markers([[step.uci.slice(0, 2), MARKER_MOVE], [step.uci.slice(2, 4), MARKER_MOVE]], true);
  board.removeArrows();
  el.status.textContent =
    `${step.san} — ${kind === "best" ? "the engine's line" : "your line"}.` +
    ` Press Esc to come back.`;
  renderVerdict(state.drill);
}

async function leavePv() {
  if (!pvCursor) return;
  pvCursor = null;
  await renderDrill(state.drill);
}

// --- game walk -------------------------------------------------------------

async function renderGame(g) {
  board.disableMoveInput();
  board.removeArrows();
  await orient(g.colour === "black" ? COLOR.black : COLOR.white);
  await setBoard(g.fen, true);
  const last = g.ply > 0 ? g.moves[g.ply - 1] : null;
  markers(last ? [[last.uci.slice(0, 2), MARKER_MOVE], [last.uci.slice(2, 4), MARKER_MOVE]] : [], true);
  document.body.dataset.turn = g.ply % 2 === 0 ? "white" : "black";
  el.status.textContent = last
    ? `${last.number}${last.white ? "." : "..."} ${last.san}`
    : "Start of the game";

  el.context.innerHTML =
    `<div class="players">${playersLine(g)}</div>` +
    `<h1>${esc(g.white || "?")} – ${esc(g.black || "?")}</h1>` +
    `<div class="meta">${esc(g.result || "")} · ${g.moves.length} plies` +
    ` · working from ${g.colour}</div>`;
  el.progress.innerHTML = "";
  el.verdict.innerHTML = `<div class="movelist">` + g.moves.map((m, i) => {
    const num = m.white ? `<span class="num">${m.number}.</span>` : "";
    return `${num}<span class="mv ${i + 1 === g.ply ? "on" : ""}" data-ply="${i + 1}">` +
      `${esc(m.san)}</span>`;
  }).join(" ") + `</div>`;
  el.actions.innerHTML =
    rowOf([btnHtml("gplay", "Play from here", false, "primary")]) +
    rowOf([btnHtml("gstart", "⇤"), btnHtml("gprev", "←"),
           btnHtml("gnext", "→"), btnHtml("gend", "⇥"),
           btnHtml("gcolour", g.colour === "white" ? "As White" : "As Black")], "small") +
    rowOf([btnHtml("gsetup", "Set up from here"), btnHtml("gclose", "Leave game")], "small");
  el.tree.innerHTML = "";
  el.treeBox.hidden = true;
}

// --- menu, help, editor ----------------------------------------------------

async function openMenu() {
  const [groups, saved] = await Promise.all([call("/api/groups", {}), call("/api/saved", {})]);
  if (!groups) return;
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
      `Nothing here yet — ./run phases --build</span></div>`}</div>`);
  const search = $("search");
  search.focus();
  search.addEventListener("input", () => {
    const q = search.value.toLowerCase();
    el.menu.querySelectorAll(".row-item.find").forEach((row) => {
      row.hidden = !row.textContent.toLowerCase().includes(q);
    });
  });
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
    ` plays one of its five best moves, in a random order that uses all five` +
    ` before repeating. You find the best reply. Right-click the board to draw` +
    ` arrows and circles, as on chess.com.</div>` +
    `<div class="sec">Keys</div><table class="stats">` + [
      ["Enter", "Next position"], ["O / B", "Menu"], ["M", "Cycle mode"],
      ["E", "Set up a position"], ["S", "Save position"], ["Backspace", "Back"],
      ["R", "Replay this position"], ["D", "Drill from here"],
      ["Space", "Show me the move"], ["? / H", "Help"], ["Esc", "Close"],
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

const editor = { pieces: {}, turn: "black", castling: "", hand: "P", open: false };
const PIECE_ORDER = ["K", "Q", "R", "B", "N", "P", "k", "q", "r", "b", "n", "p"];

function openEditor(fen) {
  editor.open = true;
  editor.pieces = {};
  editor.castling = "";
  editor.turn = "black";
  if (fen) seedFromFen(fen);
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
  el.editor.innerHTML =
    `<div class="hint">Click a square to place, click it again to clear. ` +
    `<b>To move</b> is the side your <b>opponent</b> takes: to move ` +
    `${editor.turn === "black" ? "Black means you play White" : "White means you play Black"}.` +
    `</div><div class="palette">` + PIECE_ORDER.map((p) =>
      `<button data-act="hand" data-p="${p}" class="${editor.hand === p ? "on" : ""}">` +
      `<svg viewBox="0 0 40 40"><use href="vendor/cm-chessboard/assets/pieces/standard.svg#` +
      `${spriteName(p)}"></use></svg></button>`).join("") +
    `</div><div class="opts">` +
    `<button data-act="turn">To move: ${editor.turn === "black" ? "Black" : "White"}</button>` +
    ["K", "Q", "k", "q"].map((c) => `<button data-act="castle" data-c="${c}" ` +
      `class="${editor.castling.includes(c) ? "on" : ""}">${c}</button>`).join("") +
    `</div><input id="ed-name" placeholder="Name (optional)">` +
    `<div class="opts"><button data-act="ed-empty">Empty</button>` +
    `<button data-act="ed-start">Start position</button>` +
    `<button data-act="ed-cancel">Cancel</button>` +
    `<button class="primary" data-act="ed-go">Drill this</button></div>`;
  board.disableMoveInput();
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
  el.editor.hidden = true;
  board.disableSquareSelect();
  shownFen = null;
  if (state) apply(state);
}

async function editorGo() {
  const name = ($("ed-name") || {}).value || "";
  const data = await call("/api/editor/set", {
    pieces: editor.pieces, turn: editor.turn,
    castling: editor.castling || "-", name,
  });
  if (data) { editor.open = false; el.editor.hidden = true; }
}

function show(node, html) {
  closeOverlays();
  node.innerHTML = html;
  node.hidden = false;
}

function closeOverlays() {
  [el.menu, el.help].forEach((n) => { n.hidden = true; });
  el.modeList.hidden = true;
}

function anyOverlayOpen() { return !el.menu.hidden || !el.help.hidden; }

// --- events ----------------------------------------------------------------

// The library handles dragging. Clicking a piece and then clicking a square is
// handled here, from the same list of legal moves the server sent, so the two
// paths cannot race each other over one move.
el.board.addEventListener("click", (event) => {
  const square = squareFromEvent(event);
  if (!square) return;
  if (editor.open) return editorClick(square);
  clickToMove(square);
});

/** Place or clear one square. The board is not rebuilt: a full redraw would
    drop any click that lands while it is running. */
function editorClick(square) {
  if (editor.pieces[square]) {
    delete editor.pieces[square];
    board.setPiece(square, null);
  } else {
    editor.pieces[square] = editor.hand;
    board.setPiece(square, spriteName(editor.hand));
  }
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

function clickToMove(square) {
  const d = state && state.drill;
  if (!d || !d.can_answer || submitting) return;
  const legal = d.legal || {};
  if (selected && selected !== square) {
    const move = (legal[selected] || []).find((m) => m.to === square);
    if (move) {
      const from = selected;
      // The library is mid-click on the same move; stand it down completely so
      // the two paths cannot both try to play it.
      board.disableMoveInput();
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
  const hit = event.target.closest("[data-act], [data-node], [data-ply], [data-set-mode]");
  if (!hit) {
    if (anyOverlayOpen() && !event.target.closest(".overlay") &&
        !event.target.closest("#top")) closeOverlays();
    return;
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
    case "deeper": shownKey = null; return void call("/api/deeper", {});
    case "reset": shownKey = null; return void call("/api/reset", {});
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
    case "pv": return walkPv(hit.dataset.kind, Number(hit.dataset.i));
    case "hand": editor.hand = hit.dataset.p; return drawEditor();
    case "turn": editor.turn = editor.turn === "black" ? "white" : "black";
      return drawEditor();
    case "castle": {
      const c = hit.dataset.c;
      editor.castling = editor.castling.includes(c)
        ? editor.castling.replace(c, "") : editor.castling + c;
      return drawEditor();
    }
    case "ed-empty": editor.pieces = {}; editor.castling = ""; return drawEditor();
    case "ed-start":
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
    default: return;
  }
});

$("btn-random").addEventListener("click", () => { shownKey = null; call("/api/random", {}); });
$("btn-openings").addEventListener("click", openMenu);
$("btn-help").addEventListener("click", openHelp);
$("btn-game").addEventListener("click", async () => {
  const url = window.prompt("chess.com game link, or paste a PGN");
  if (!url) return;
  shownKey = null;
  await call("/api/game/load", { url });
});
el.modeBtn.addEventListener("click", (event) => {
  event.stopPropagation();
  el.modeList.hidden = !el.modeList.hidden;
});

document.addEventListener("keydown", (event) => {
  if (/^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)) return;
  const key = event.key;
  if (key === "Escape") {
    if (anyOverlayOpen()) { event.preventDefault(); return closeOverlays(); }
    if (editor.open) { event.preventDefault(); return closeEditor(); }
    if (pvCursor) { event.preventDefault(); return void leavePv(); }
    return;
  }
  if (editor.open) return;
  const g = state && state.game;
  const d = state && state.drill;
  if (g) {
    const map = { ArrowLeft: g.ply - 1, ArrowRight: g.ply + 1, Home: 0, End: g.moves.length };
    if (key in map) { event.preventDefault(); return void call("/api/game/goto", { ply: map[key] }); }
  }
  switch (key.toLowerCase()) {
    case "enter": event.preventDefault(); shownKey = null;
      return void call(g ? "/api/game/play" : "/api/next", {});
    case " ": if (d && d.can_answer) { event.preventDefault(); return void call("/api/show", {}); }
      return;
    case "o": case "b": event.preventDefault(); return openMenu();
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
    case "d": if (d && d.can_deeper) { event.preventDefault(); shownKey = null;
      return void call("/api/deeper", {}); } return;
    case "backspace": event.preventDefault(); shownKey = null;
      return void call("/api/back", {});
    case "r": event.preventDefault(); shownKey = null; return void call("/api/reset", {});
    case "?": case "h": event.preventDefault(); return openHelp();
    default: break;
  }
});

function esc(text) {
  return String(text === null || text === undefined ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Handy from the browser console when something looks wrong.
window.__trainer = { board, editor, get state() { return state; }, get selected() { return selected; } };

// A drill is already running when the page loads.
call("/api/state");
