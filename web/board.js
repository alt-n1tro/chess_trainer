// Board rendering and clicking. No chess logic lives here: the server says
// what is on the board and what is clickable.
import { pieceSvg } from "./pieces/pieces.js";

const FILES = "abcdefgh";

export class Board {
  constructor(el, onMove) {
    this.el = el;
    this.onMove = onMove;
    this.flip = false;
    this.pieces = {};
    this.legal = {};
    this.marks = {};
    this.selected = null;
    this.interactive = false;
    this.onSquare = null;          // the editor borrows the board
    el.addEventListener("click", (e) => this.click(e));
  }

  set(state) {
    this.pieces = {};
    (state.board || []).forEach((p) => { this.pieces[p.square] = p.piece; });
    this.legal = state.legal || {};
    this.marks = state.marks || {};
    this.flip = !!state.flip;
    this.interactive = !!state.interactive;
    this.selected = null;
    this.render();
  }

  render() {
    const ranks = this.flip ? [0, 1, 2, 3, 4, 5, 6, 7] : [7, 6, 5, 4, 3, 2, 1, 0];
    const files = this.flip ? [7, 6, 5, 4, 3, 2, 1, 0] : [0, 1, 2, 3, 4, 5, 6, 7];
    const targets = new Set(
      (this.selected && (this.legal[this.selected] || []).map((m) => m.to)) || []
    );
    let html = "";
    for (const r of ranks) {
      for (const f of files) {
        const sq = FILES[f] + (r + 1);
        const dark = (f + r) % 2 === 0;
        const cls = ["sq", dark ? "d" : "l"];
        if (sq === this.selected) cls.push("sel");
        if (targets.has(sq)) {
          cls.push("target");
          if (this.pieces[sq]) cls.push("occupied");
        }
        if (this.marks[sq]) cls.push(this.marks[sq]);
        const coord = (f === files[0] ? (r + 1) : "") ||
                      (r === ranks[7] ? FILES[f] : "");
        html += `<div class="${cls.join(" ")}" data-sq="${sq}">` +
                (coord ? `<span class="coord">${coord}</span>` : "") +
                (this.pieces[sq] ? pieceSvg(this.pieces[sq]) : "") + "</div>";
      }
    }
    this.el.innerHTML = html;
  }

  click(event) {
    const cell = event.target.closest(".sq");
    if (!cell) return;
    const sq = cell.dataset.sq;
    if (this.onSquare) return this.onSquare(sq);
    if (!this.interactive) return;
    if (this.selected) {
      const move = (this.legal[this.selected] || []).find((m) => m.to === sq);
      if (move) {
        const from = this.selected;
        this.selected = null;
        this.render();
        return this.onMove(from, sq, move.promotion ? this.askPromotion() : null);
      }
    }
    this.selected = this.legal[sq] ? sq : null;
    this.render();
  }

  askPromotion() {
    // The server offers every promotion; the one question it cannot answer.
    const reply = (window.prompt("Promote to? q, r, b or n", "q") || "q")
      .trim().toLowerCase();
    return ["q", "r", "b", "n"].includes(reply) ? reply : "q";
  }
}
