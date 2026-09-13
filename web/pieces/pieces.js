// Inline SVG piece set. Served locally, nothing is fetched from outside.
// Each entry is the inner markup of a 0 0 45 45 viewBox. Colour comes from the
// two CSS variables below, so the pieces keep their meaning in every mode.

const SHAPES = {
  p: `<path d="M22.5 9a5.2 5.2 0 0 1 3 9.5c2.6 1.5 4.2 4 4.2 6.8h-14.4c0-2.8 1.6-5.3 4.2-6.8A5.2 5.2 0 0 1 22.5 9z"/>
      <path d="M15 26h15l1.8 5.5H13.2z"/>
      <path d="M11.5 33h22l2 5h-26z"/>`,
  r: `<path d="M11 11h4v3.5h4V11h7v3.5h4V11h4v7l-3.5 3v10l3.5 3v5H11v-5l3.5-3V21L11 18z"/>
      <path d="M9 38h27v3H9z"/>`,
  n: `<path d="M16 10c1.5 2.5 4 2.5 6.5 1.5 5.5 0 10.5 4 11.5 10.5.8 5.2-.5 8.5-.5 12H14c0-4 3-6.5 6-9.5 2-2 3-3.5 2.5-5-1.5 1.8-3.5 3.4-6 4.2-1.7.5-3-.5-3.2-2.2-.2-2 .6-3.5 1.7-5.2 1.2-1.8 1.6-4 1-6.3z"/>
      <circle cx="17.8" cy="18.4" r="1.1" fill="var(--piece-eye)"/>
      <path d="M11 38h23v3H11z"/>`,
  b: `<path d="M22.5 8c1.7 0 3 1.3 3 3 0 1-.5 1.9-1.3 2.4 3.7 1.8 6.3 5.6 6.3 10 0 3-1 5.3-2.5 6.6h-11C15.5 28.7 14.5 26.4 14.5 23.4c0-4.4 2.6-8.2 6.3-10a3 3 0 0 1-1.3-2.4c0-1.7 1.3-3 3-3z"/>
      <path d="M16 31h13l1.5 4h-16z"/>
      <path d="M11 36h23v4H11z"/>`,
  q: `<circle cx="8" cy="13" r="2.4"/><circle cx="16" cy="10" r="2.4"/>
      <circle cx="22.5" cy="8.5" r="2.6"/><circle cx="29" cy="10" r="2.4"/>
      <circle cx="37" cy="13" r="2.4"/>
      <path d="M9 16l4 13h19l4-13-6 7-3.5-9-4 9.5-4-9.5-3.5 9z"/>
      <path d="M12 30h21l1.5 4.5h-24z"/>
      <path d="M10 36h25v4.5H10z"/>`,
  k: `<path d="M21 6h3v3.5h3.5v3H24V17h-3v-4.5h-3.5v-3H21z"/>
      <path d="M22.5 17c5 0 9 3.2 9 7.6 0 3-1.6 5.3-4 7.4h-10c-2.4-2.1-4-4.4-4-7.4 0-4.4 4-7.6 9-7.6z"/>
      <path d="M12 33h21v3.5H12z"/><path d="M10 38h25v3.5H10z"/>`,
};

const NAMES = { p: "pawn", r: "rook", n: "knight", b: "bishop", q: "queen", k: "king" };

export function pieceSvg(symbol) {
  const type = symbol.toLowerCase();
  const white = symbol === symbol.toUpperCase();
  const shape = SHAPES[type];
  if (!shape) return "";
  const cls = white ? "pc pc-w" : "pc pc-b";
  return `<svg class="${cls}" viewBox="0 0 45 45" aria-label="${white ? "white" : "black"} ${NAMES[type]}" role="img">${shape}</svg>`;
}
