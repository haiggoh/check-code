// Functional-probe fixture for the typescript-lsp catalog entry: exactly ONE planted error
// (TS2322 — a string assigned to a number). A working server reports it; a present-but-broken
// one (a stub typescript lib, so every tsserverRequest answers noServer) reports nothing.
// "Installed" must mean "actually reports errors", not "a binary exists".
export const planted: number = 'not a number'