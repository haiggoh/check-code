// Fixture: JSX that only parses as TSX, compiled against a local `h`, plus ONE planted error.
declare function h(tag: unknown, props: unknown, ...children: unknown[]): unknown
declare namespace JSX {
  interface IntrinsicElements { [name: string]: unknown }
}
const Text = (_props: { dimColor?: boolean; children?: unknown }) => null
const count: number = 3
export const row = <Text dimColor>{count}</Text>
export const planted: number = 'not a number'
