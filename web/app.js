// The one screen. Board on the left, panel on the right.
import { Board } from "./board.js";
import { pieceSvg } from "./pieces/pieces.js";

const $ = (id) => document.getElementById(id);
const el = { board: $("board"), foot: $("board-foot"), panel: $("panel"),
  pick: $("pick"), tree: $("tree"), drill: $("drill"), note: $("note"),
  menu: $("menu"), editor: $("editor"), help: $("help"),
  modeBtn: $("btn-mode"), modeList: $("mode-list") };

const MODE_LABEL = { openings: "Openings", middlegame: "Middlegame", endgame: "Endgame" };
const MODE_NOUN = { openings: "opening", middlegame: "middlegame", endgame: "endgame" };

let state = null;
let preview = null;
const board = new Board(el.board, (from, to, promotion) =>
  post("/api/answer", { from, to, promotion }));

// --- transport -------------------------------------------------------------

async function post(path, body) {
  note("");
  try {
    const res = await fetch(path, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await res.json();
    if (data.error) { note(data.error); return null; }
    if (data.mode) apply(data);
    if (data.message) note(data.message);
    return data;
  } catch (err) {
    note("The server is not answering. Is ./run web still running?");
    return null;
  }
}

async function get(path) {
  const res = await fetch(path);
  const data = await res.json();
  if (data.error) { note(data.error); return null; }
  return data;
}

function note(text) { el.note.textContent = text || ""; }

// --- top-level render ------------------------------------------------------

function apply(data) {
  state = data;
  preview = null;
  document.body.dataset.mode = data.mode;
  el.modeBtn.textContent = MODE_LABEL[data.mode] + " ▾";
  el.pick.classList.toggle("wide", data.mode !== "openings");
  renderModeList();
  if (data.game) { renderGame(); } else { renderDrill(); }
}

function renderModeList() {
  el.modeList.innerHTML = (state.modes || []).map((m) =>
    `<li data-mode="${m.mode}" class="${m.active ? "on" : ""}">` +
    `<span>${MODE_LABEL[m.mode]}</span>` +
    `<span class="n">${m.count} position${m.count === 1 ? "" : "s"}</span></li>`
  ).join("");
}

// --- the drill -------------------------------------------------------------

function renderDrill() {
  const d = state.drill;
  if (!d) {
    el.tree.innerHTML = "";
    el.drill.innerHTML = `<h2>Nothing in this pool yet</h2>
      <div class="where">Import games, then build the pools:</div>
      <div class="tail">./run import &lt;pgn-or-url&gt; &nbsp; ./run phases --build</div>
      <div class="acts"><button data-act="menu">Open the menu</button>
      <button data-act="editor">Set up a position</button></div>`;
    board.set({ board: [], flip: false, interactive: false });
    el.foot.textContent = "";
    return;
  }
  const a = d.answer;
  const marks = {};
  if (a) {
    if (a.my_move) {
      marks[a.my_move.slice(0, 2)] = "from";
      marks[a.my_move.slice(2, 4)] = a.verdict === "best" ? "best-to" :
        (a.verdict === "wrong" ? "wrong-to" : "to");
    }
    if (a.best_move && a.best_move !== a.my_move) {
      marks[a.best_move.slice(0, 2)] = "best-from";
      marks[a.best_move.slice(2, 4)] = "best-to";
    }
  } else if (d.opp_move) {
    marks[d.opp_move.slice(0, 2)] = "from";
    marks[d.opp_move.slice(2, 4)] = "to";
  }
  board.set({
    board: d.board, legal: d.legal, flip: d.my_colour === "black",
    interactive: d.can_answer, marks,
  });
  el.foot.textContent = d.can_answer
    ? `They played ${d.opp_san}. Your move as ${d.my_colour}.`
    : `${d.opp_san} — round ${d.round} of ${d.rounds}.`;

  renderTree();
  el.drill.innerHTML = drillHtml(d);
}

function drillHtml(d) {
  const a = d.answer;
  const src = d.source || {};
  let head = d.name || MODE_LABEL[d.mode];
  let where = `${d.phase} · round ${d.round} of ${d.rounds}` +
    (d.depth_level ? ` · level ${d.depth_level}` : "");
  let players = "";
  const g = src.game;
  if (g) {
    players = `<div class="players">${gameLine(g)}</div>`;
  }
  let html = `${players}<h2>${esc(head)}</h2><div class="where">${where}</div>`;
  if (src.tail) html += `<div class="tail">${esc(src.tail)}</div>`;

  html += `<div class="opts">` + d.candidates.map((c, i) =>
    `<button class="opt ${c.active ? "on" : ""}" data-act="cand" data-i="${i}">` +
    `<span class="k">${i + 1}</span>${esc(c.san)}</button>`).join("") + `</div>`;

  if (a) {
    const pts = a.delta_wp === null || a.delta_wp === undefined ? "" :
      `<span class="pts">−${a.delta_wp.toFixed(1)} points</span>`;
    const wp = a.wp_best === null || a.wp_best === undefined ? "" :
      `<span class="pts">best ${a.wp_best.toFixed(1)}%` +
      (a.wp_mine === null || a.wp_mine === undefined ? "" :
        `, yours ${a.wp_mine.toFixed(1)}%`) + `</span>`;
    const label = a.verdict === "shown"
      ? `Shown: ${esc(a.best_san)}`
      : `${esc(a.my_san)} — ${esc(a.label)}`;
    html += `<div class="verdict ${a.verdict}">${label}${pts}${wp}</div>`;
    const ex = a.explanation || {};
    if (ex.text) html += `<div class="explain">${esc(ex.text)}</div>`;
    html += `<div class="pvs">` +
      pvLine("Yours", ex.my_pv, "mine") + pvLine("Best", ex.best_pv, "best") +
      `</div>`;
  }

  const acts = [];
  acts.push(btn("back", "← Back", state.stack_depth <= 1 && !d.depth_level));
  acts.push(btn("start", "Start"));
  acts.push(btn("save", "Save position"));
  acts.push(btn("reset", "Reset"));
  if (!a) {
    acts.push(btn("show", "Show me the move", false, "primary"));
  } else {
    if (d.can_deeper) acts.push(btn("deeper", "Drill from here", false, "primary"));
    acts.push(btn("next", d.has_next_round ? "Next position"
      : `Drill another ${MODE_NOUN[d.mode]}`, false, d.can_deeper ? "" : "primary"));
  }
  return html + `<div class="acts">${acts.join("")}</div>`;
}

function pvLine(label, pv, kind) {
  if (!pv || !pv.length) return "";
  return `<div class="pv"><span class="lab">${label}</span>` + pv.map((m, i) =>
    `<button data-act="pv" data-kind="${kind}" data-i="${i}">${esc(m.san)}</button>`
  ).join("") + `</div>`;
}

function btn(act, label, disabled, cls) {
  return `<button class="${cls || ""}" data-act="${act}"${disabled ? " disabled" : ""}>` +
    `${label}</button>`;
}

function gameLine(g) {
  const w = `${esc(g.white || "?")}${g.white_elo ? ` (${g.white_elo})` : ""}`;
  const b = `${esc(g.black || "?")}${g.black_elo ? ` (${g.black_elo})` : ""}`;
  if (g.my_colour === "white") return `You (White, ${g.white_elo || "?"}) vs ${b}` +
    `  ${esc(g.result || "")}`;
  if (g.my_colour === "black") return `${w} vs you (Black, ${g.black_elo || "?"})` +
    `  ${esc(g.result || "")}`;
  return `${w} vs ${b}  ${esc(g.result || "")} · not your game`;
}

// --- tree ------------------------------------------------------------------

function renderTree() {
  const rows = state.tree || [];
  const here = state.drill && (state.drill.node_id_current || state.drill.node_id);
  el.tree.innerHTML = rows.map((r) => {
    const guide = r.depth ? `<span class="guide">${"│ ".repeat(r.depth - 1)}└</span>` : "";
    const v = r.verdict ? `<span class="v ${r.verdict}">${r.verdict === "best" ? "●" :
      r.verdict === "playable" ? "◐" : "○"}</span>` : "";
    const arrow = r.drilled && r.id !== here ? ` <span class="guide">›</span>` : "";
    const visits = r.visits > 1 ? `<sup>${r.visits}</sup>` : "";
    const label = r.san || "start";
    return `<div class="tr ${r.id === here ? "here" : ""}" data-node="${r.id}">` +
      `${guide}<span>${esc(label)}${visits}</span>${v}${arrow}</div>`;
  }).join("") || `<div class="tr">start</div>`;
}

// --- game walk -------------------------------------------------------------

function renderGame() {
  const g = state.game;
  board.set({ board: g.board, flip: g.flip, interactive: false, legal: {} });
  el.tree.innerHTML = "";
  el.foot.textContent = g.url || "";
  const moves = g.moves.map((m, i) => {
    const num = m.white ? `<span class="num">${m.number}.</span>` : "";
    return num + `<span class="mv ${i + 1 === g.ply ? "on" : ""}" data-ply="${i + 1}">` +
      `${esc(m.san)}</span>`;
  }).join(" ");
  el.drill.innerHTML =
    `<div class="players">${gameLine(g)}</div>` +
    `<h2>${esc(g.white || "?")} – ${esc(g.black || "?")}</h2>` +
    `<div class="where">${g.result || ""} · move ${Math.ceil(g.ply / 2) || 0}` +
    ` of ${Math.ceil(g.moves.length / 2)} · working from ` +
    `<button data-act="gcolour">${g.colour === "white" ? "White" : "Black"}</button></div>` +
    `<div class="movelist">${moves}</div>` +
    `<div class="acts">` +
    btn("gstart", "⇤ Start") + btn("gprev", "←") + btn("gnext", "→") +
    btn("gend", "End ⇥") +
    btn("gplay", "Play from here", false, "primary") +
    btn("gsetup", "Set up from here") +
    btn("gclose", "Leave the game") +
    `</div>`;
}

// --- menu ------------------------------------------------------------------

async function openMenu() {
  const [groups, saved] = await Promise.all([get("/api/groups"), get("/api/saved")]);
  if (!groups) return;
  const mode = state.mode;
  let html = `<button class="close ghost" data-act="close">✕</button>` +
    `<h3>${MODE_LABEL[mode]}</h3>` +
    `<input id="search" placeholder="Search" autocomplete="off">` +
    `<div class="rows">` +
    `<div class="row" data-act="editor"><span class="t">Set up a position</span>` +
    `<span class="meta">E</span></div>` +
    `<div class="row" data-act="random"><span class="t">Random</span>` +
    `<span class="meta">weighted</span></div></div>`;

  if (saved && saved.saved.length) {
    html += `<div class="sec">Your positions</div><div class="rows">` +
      saved.saved.map((s) =>
        `<div class="row find" data-act="saved" data-id="${s.id}">` +
        `<span class="t">${esc(s.name || "Saved position")}</span></div>`).join("") +
      `</div>`;
  }
  html += `<div class="sec">${mode === "openings" ? "Openings" : MODE_LABEL[mode]}</div>`;
  html += `<div class="rows">` + (groups.groups.length ? groups.groups.map((g) => {
    const sides = Object.entries(g.colours).filter(([c]) => c).map(([c, n]) =>
      `<button data-act="group" data-name="${esc(g.name)}" data-colour="${c}">` +
      `${c === "white" ? "White" : "Black"} ${n}</button>`).join("");
    return `<div class="row find"><span class="t">${esc(g.name)}</span>` +
      `<span class="sides">${sides}</span></div>`;
  }).join("") : `<div class="row"><span class="meta">Nothing here yet. ` +
    `./run phases --build</span></div>`) + `</div>`;
  show(el.menu, html);
  const search = $("search");
  search.focus();
  search.addEventListener("input", () => {
    const q = search.value.toLowerCase();
    el.menu.querySelectorAll(".row.find").forEach((row) => {
      row.hidden = !row.textContent.toLowerCase().includes(q);
    });
  });
}

// --- editor ----------------------------------------------------------------

const editor = { pieces: {}, turn: "black", castling: "", hand: "P" };

function openEditor(fen) {
  editor.pieces = {};
  editor.castling = "";
  editor.turn = "black";
  if (fen) seedFromFen(fen);
  drawEditor();
}

function seedFromFen(fen) {
  // Reading a placement string is rendering, not rules: the server still owns
  // every judgement about the position.
  const [placement, turn, castling] = fen.split(" ");
  let rank = 7, file = 0;
  for (const ch of placement) {
    if (ch === "/") { rank -= 1; file = 0; }
    else if (/\d/.test(ch)) file += Number(ch);
    else { editor.pieces["abcdefgh"[file] + (rank + 1)] = ch; file += 1; }
  }
  editor.turn = turn === "b" ? "black" : "white";
  editor.castling = castling === "-" ? "" : castling;
}

function drawEditor() {
  const palette = ["K", "Q", "R", "B", "N", "P", "k", "q", "r", "b", "n", "p"]
    .map((s) => `<button data-act="hand" data-p="${s}" ` +
      `class="${editor.hand === s ? "on" : ""}">${pieceSvg(s)}</button>`).join("");
  const rights = ["K", "Q", "k", "q"].map((c) =>
    `<button data-act="castle" data-c="${c}" ` +
    `class="${editor.castling.includes(c) ? "on" : ""}">${c}</button>`).join("");
  show(el.editor,
    `<button class="close ghost" data-act="close">✕</button>` +
    `<h3>Set up a position</h3>` +
    `<div class="hint">Click a square to place, click it again to clear.` +
    ` To move is the side the <b>opponent</b> takes: ` +
    `to move ${editor.turn === "black" ? "Black means you are White" :
      "White means you are Black"}.</div>` +
    `<div class="palette">${palette}</div>` +
    `<div class="opts-row">` +
    `<button data-act="turn">To move: ${editor.turn === "black" ? "Black" : "White"}</button>` +
    `<span class="meta">Castling</span>${rights}` +
    `</div>` +
    `<div class="opts-row">` +
    `<input id="ed-name" placeholder="Name (optional)">` +
    `<button data-act="ed-empty">Empty board</button>` +
    `<button data-act="ed-start">Starting position</button>` +
    `<button class="primary" data-act="ed-go">Drill this</button></div>`);
  // show() clears the other overlays first, so the board shrinks after it.
  document.getElementById("left").classList.add("editing");
  board.set({
    board: Object.entries(editor.pieces).map(([square, piece]) => ({ square, piece })),
    flip: editor.turn === "white", interactive: false, legal: {},
  });
  board.onSquare = (sq) => {
    if (editor.pieces[sq]) delete editor.pieces[sq];
    else editor.pieces[sq] = editor.hand;
    drawEditor();
  };
}

async function editorGo() {
  const name = ($("ed-name") || {}).value || "";
  const data = await post("/api/editor/set", {
    pieces: editor.pieces, turn: editor.turn,
    castling: editor.castling || "-", name,
  });
  if (data) closeOverlays();
}

// --- help, statistics, leaks ----------------------------------------------

async function openHelp() {
  const [stats, leaks] = await Promise.all([get("/api/stats"), get("/api/leaks")]);
  const a = (stats && stats.answers) || {};
  const rows = (leaks && leaks.positions) || [];
  show(el.help,
    `<button class="close ghost" data-act="close">✕</button>` +
    `<h3>Chess Trainer</h3>` +
    `<div class="sec">Keys</div>` +
    `<table class="stats">` + [
      ["Enter", "Next (and play from here in a game)"],
      ["O / B", "Open the menu"], ["M", "Cycle mode"],
      ["E", "Set up a position"], ["S", "Save position"],
      ["Backspace", "Back"], ["R", "Reset and replay this position"],
      ["? / H", "Help"], ["Esc", "Close"],
      ["← →", "In a game: step a move"],
      ["Home / End", "In a game: jump to start / end"],
      ["1 – 5", "Select the nth opponent option"],
    ].map(([k, v]) => `<tr><th><kbd>${k}</kbd></th><td>${v}</td></tr>`).join("") +
    `</table>` +
    `<div class="sec">Answers</div>` +
    `<table class="stats"><tr><th>answered</th><td>${a.answers || 0}</td></tr>` +
    `<tr><th>best</th><td>${a.best || 0}</td></tr>` +
    `<tr><th>playable</th><td>${a.playable || 0}</td></tr>` +
    `<tr><th>wrong</th><td>${a.wrong || 0}</td></tr>` +
    `<tr><th>shown</th><td>${a.shown || 0}</td></tr>` +
    `<tr><th>cached positions</th><td>${(stats && stats.cached_positions) || 0}</td></tr>` +
    `<tr><th>engine</th><td>${esc((stats && stats.engine) || "?")}</td></tr></table>` +
    `<div class="sec">Which move do I keep getting wrong</div>` +
    (rows.length ? `<table class="stats"><tr><th>group</th><th>you play</th>` +
      `<th>refuted by</th><th>missed</th></tr>` + rows.map((r) =>
      `<tr><td>${esc(r.name || "—")}</td><td>${esc(r.you_play || "—")}</td>` +
      `<td>${esc(r.refuted_by || "—")}</td>` +
      `<td>${r.misses}/${r.attempts}</td></tr>`).join("") + `</table>`
      : `<div class="hint">Nothing yet.</div>`) +
    `<div class="sec">How deep does it start</div>` +
    `<table class="stats"><tr><th>level</th><th>answers</th><th>best</th></tr>` +
    ((leaks && leaks.by_depth) || []).map((d) =>
      `<tr><td>${d.depth_level}</td><td>${d.attempts}</td>` +
      `<td>${d.accuracy === null ? 0 : d.accuracy}%</td></tr>`).join("") + `</table>` +
    `<div class="sec">Command line</div>` +
    `<table class="stats">` + [
      ["./run import &lt;pgn-or-url&gt;", "import games"],
      ["./run phases --build", "rebuild the pools"],
      ["./run warm", "pre-analyse everything"],
      ["./run leaks", "the leak list"], ["./run tree", "recorded answers"],
      ["./run doctor", "check engine, DB, port"],
    ].map(([c, v]) => `<tr><th>${c}</th><td>${v}</td></tr>`).join("") + `</table>`);
}

// --- overlays --------------------------------------------------------------

function show(node, html) {
  closeOverlays();
  node.innerHTML = html;
  node.hidden = false;
}

function closeOverlays() {
  [el.menu, el.editor, el.help].forEach((n) => { n.hidden = true; });
  document.getElementById("left").classList.remove("editing");
  el.modeList.hidden = true;
  board.onSquare = null;
  if (state) { if (state.game) renderGame(); else renderDrill(); }
}

function openOverlayIsOpen() {
  return !el.menu.hidden || !el.editor.hidden || !el.help.hidden;
}

// --- preview ---------------------------------------------------------------

function showPreview(kind, index) {
  const ex = state.drill && state.drill.answer && state.drill.answer.explanation;
  if (!ex) return;
  const pv = kind === "best" ? ex.best_pv : ex.my_pv;
  const item = pv && pv[index];
  if (!item) return;
  preview = { kind, index };
  board.set({
    board: item.board, flip: state.drill.my_colour === "black",
    interactive: false, legal: {},
    marks: { [item.uci.slice(0, 2)]: "from", [item.uci.slice(2, 4)]: "to" },
  });
  el.foot.textContent = `${item.san} — ${kind === "best" ? "the engine's line"
    : "your line"}. Esc returns to the position.`;
}

// --- events ----------------------------------------------------------------

document.addEventListener("click", async (event) => {
  const hit = event.target.closest("[data-act], [data-node], [data-ply], [data-mode]");
  if (!hit) {
    // The menu and the help close on an outside click. The editor does not:
    // the board is part of it, and that is where you place pieces.
    const dismissable = !el.menu.hidden || !el.help.hidden;
    if (dismissable && !event.target.closest(".overlay")) closeOverlays();
    if (!el.modeList.hidden) el.modeList.hidden = true;
    return;
  }
  if (hit.dataset.mode) {
    el.modeList.hidden = true;
    return void post("/api/mode", { mode: hit.dataset.mode });
  }
  if (hit.dataset.node) return void post("/api/goto", { node_id: Number(hit.dataset.node) });
  if (hit.dataset.ply) return void post("/api/game/goto", { ply: Number(hit.dataset.ply) });

  const act = hit.dataset.act;
  const g = state && state.game;
  switch (act) {
    case "close": return closeOverlays();
    case "menu": return openMenu();
    case "editor": return openEditor();
    case "random": closeOverlays(); return void post("/api/random", {});
    case "saved": closeOverlays();
      return void post("/api/drill", { saved_id: Number(hit.dataset.id) });
    case "group": closeOverlays();
      return void post("/api/drill", { name: hit.dataset.name, colour: hit.dataset.colour });
    case "cand": return void post("/api/select", { index: Number(hit.dataset.i) });
    case "show": return void post("/api/show", {});
    case "next": return void post("/api/next", {});
    case "deeper": return void post("/api/deeper", {});
    case "reset": return void post("/api/reset", {});
    case "back": return void post("/api/back", {});
    case "start": return void post("/api/start", {});
    case "save": {
      const name = window.prompt("Name this position", (state.drill && state.drill.name) || "");
      if (name === null) return;
      const data = await post("/api/save", { name });
      if (data) note("Saved.");
      return;
    }
    case "pv": return showPreview(hit.dataset.kind, Number(hit.dataset.i));
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
    case "ed-go": return editorGo();
    case "gcolour": return void post("/api/game/colour",
      { colour: g.colour === "white" ? "black" : "white" });
    case "gprev": return void post("/api/game/goto", { ply: g.ply - 1 });
    case "gnext": return void post("/api/game/goto", { ply: g.ply + 1 });
    case "gstart": return void post("/api/game/goto", { ply: 0 });
    case "gend": return void post("/api/game/goto", { ply: g.moves.length });
    case "gplay": return void post("/api/game/play", {});
    case "gsetup": {
      const data = await get("/api/game/setup");
      if (data) openEditor(data.fen);
      return;
    }
    case "gclose": return void post("/api/random", {});
    default: return;
  }
});

$("btn-random").addEventListener("click", () => post("/api/random", {}));
$("btn-openings").addEventListener("click", openMenu);
$("btn-game").addEventListener("click", async () => {
  const url = window.prompt("chess.com game link, or paste a PGN");
  if (!url) return;
  await post("/api/game/load", { url });
});
$("btn-help").addEventListener("click", openHelp);
el.modeBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  el.modeList.hidden = !el.modeList.hidden;
});

document.addEventListener("keydown", (event) => {
  const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName);
  const key = event.key;
  if (key === "Escape") {
    if (openOverlayIsOpen() || preview) { event.preventDefault(); return closeOverlays(); }
    return;
  }
  if (typing) return;
  const g = state && state.game;
  const d = state && state.drill;
  const k = key.toLowerCase();
  if (key >= "1" && key <= "5" && d) {
    event.preventDefault();
    return void post("/api/select", { index: Number(key) - 1 });
  }
  switch (k) {
    case "enter": event.preventDefault();
      return void post(g ? "/api/game/play" : "/api/next", {});
    case "o": case "b": event.preventDefault(); return openMenu();
    case "m": {
      event.preventDefault();
      const order = ["openings", "middlegame", "endgame"];
      const next = order[(order.indexOf(state.mode) + 1) % order.length];
      return void post("/api/mode", { mode: next });
    }
    case "e": event.preventDefault(); return openEditor();
    case "s": event.preventDefault();
      return void document.querySelector('[data-act="save"]').click();
    case "backspace": event.preventDefault(); return void post("/api/back", {});
    case "r": event.preventDefault(); return void post("/api/reset", {});
    case "?": case "h": event.preventDefault(); return openHelp();
    default: break;
  }
  if (g) {
    if (key === "ArrowLeft") { event.preventDefault(); post("/api/game/goto", { ply: g.ply - 1 }); }
    if (key === "ArrowRight") { event.preventDefault(); post("/api/game/goto", { ply: g.ply + 1 }); }
    if (key === "Home") { event.preventDefault(); post("/api/game/goto", { ply: 0 }); }
    if (key === "End") { event.preventDefault(); post("/api/game/goto", { ply: g.moves.length }); }
  }
});

function esc(text) {
  return String(text === null || text === undefined ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// On load a drill is already running and a position is already waiting.
get("/api/state").then((data) => {
  if (data) { apply(data); if (data.message) note(data.message); }
});
