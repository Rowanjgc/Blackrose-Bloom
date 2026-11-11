import {makeScene2D, Node, Line, Circle, Txt} from '@motion-canvas/2d';
import {createRef, createSignal, linear, waitFor} from '@motion-canvas/core';

// One input rotation drives both gears; the large gear has twice as many teeth.
export default makeScene2D(function* (view) {
  const angle = createSignal(0);
  const assembly = createRef<Node>();
  view.add(<Txt text="Motion, carried by teeth" y={-205} fontSize={34}
    fill="#e0e0e0" fontFamily="sans-serif" />);
  view.add(<Node ref={assembly} opacity={0} y={-5} />);

  function gear(x: number, teeth: number, pitch: number, color: string, rotation: () => number) {
    const points: [number, number][] = [];
    for (let i = 0; i < teeth * 4; i++) {
      const a = i / (teeth * 4) * Math.PI * 2;
      const radius = i % 4 === 1 || i % 4 === 2 ? pitch + 7 : pitch - 7;
      points.push([Math.cos(a) * radius, Math.sin(a) * radius]);
    }
    return <Node x={x} rotation={rotation}>
      <Line points={points} closed fill="#1a1a1a" stroke={color} lineWidth={3} />
      <Circle size={pitch * 1.35} stroke="#333333" lineWidth={2} />
      {[0, 120, 240].map(a => <Line rotation={a} points={[[pitch * .24, 0], [pitch * .60, 0]]}
        stroke={color} opacity={.65} lineWidth={8} lineCap="round" />)}
      <Circle size={pitch * .38} fill="#0d0d0d" stroke={color} lineWidth={3} />
      <Line points={[[0, -pitch * .48], [0, -pitch * .62]]} stroke="#e0e0e0" lineWidth={4} />
    </Node>;
  }
  assembly().add(gear(-134, 12, 68, '#ef6461', () => angle()));
  assembly().add(gear(70, 24, 136, '#d4af37', () => -angle() / 2 + 3.75));
  view.add(<Txt text="12 teeth" x={-160} y={165} fontSize={22} fill="#ef6461" />);
  view.add(<Txt text="24 teeth" x={90} y={165} fontSize={22} fill="#d4af37" />);
  view.add(<Txt text="Two turns in · one turn out" y={220} fontSize={22} fill="#b3b3b3" />);
  yield* assembly().opacity(1, .6);
  yield* angle(720, 6, linear);
  yield* waitFor(1.4);
});
