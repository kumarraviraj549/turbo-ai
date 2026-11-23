import React from 'react';
import {ThreeCanvas} from '@remotion/three';
import {AbsoluteFill, interpolate, Sequence, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import type {ProceduralProps} from './schema';

const background = '#080B15';

const BarChart: React.FC<Extract<ProceduralProps, {template: '3d_bar_chart'}>> = (props) => {
  const frame = useCurrentFrame();
  const max = Math.max(...props.bars.map((bar) => Math.abs(bar.value)), 1);
  const width = Math.min(1.45, 8 / props.bars.length);
  return (
    <group rotation={[-0.12, -0.2, 0]}>
      {props.bars.map((bar, index) => {
        const height = 0.6 + (Math.abs(bar.value) / max) * 4.1;
        const grow = spring({frame, fps: 30, delay: index * 5, config: {damping: 18, mass: 0.7}});
        return (
          <group key={`${bar.label}-${index}`}>
            <mesh position={[(index - (props.bars.length - 1) / 2) * 2.15, -2.4 + (height * grow) / 2, 0]}
              scale={[width, Math.max(0.001, grow), 1]}>
              <boxGeometry args={[1, height, 0.5]} />
              <meshPhysicalMaterial color={index % 2 ? props.brand.secondary : props.brand.primary}
                metalness={0.65} roughness={0.22} clearcoat={0.8} />
            </mesh>
            <mesh position={[(index - (props.bars.length - 1) / 2) * 2.15, -2.43, 0]}>
              <boxGeometry args={[width + 0.22, 0.08, 0.8]} />
              <meshStandardMaterial color={props.brand.accent} emissive={props.brand.accent} emissiveIntensity={0.4} />
            </mesh>
          </group>
        );
      })}
      <mesh position={[0, -2.55, -0.2]}>
        <boxGeometry args={[Math.max(6, props.bars.length * 2.1), 0.12, 1.2]} />
        <meshStandardMaterial color="#273047" metalness={0.7} roughness={0.3} />
      </mesh>
      <mesh position={[0, -2.44, -0.9]} rotation={[0, 0, Math.sin(frame / 70) * 0.01]}>
        <boxGeometry args={[Math.max(6, props.bars.length * 2.1), 0.03, 0.03]} />
        <meshBasicMaterial color={props.brand.secondary} />
      </mesh>
    </group>
  );
};

const Pedestal: React.FC<{frame: number; color: string}> = ({frame, color}) => {
  const rise = spring({frame, fps: 30, config: {damping: 14, mass: 0.8}});
  return (
    <group position={[0, -1.5, 0]}>
      <mesh scale={[1.8 * rise, 0.34 * rise, 1.25 * rise]}>
        <cylinderGeometry args={[1.35, 1.6, 0.45, 10, 1, false]} />
        <meshPhysicalMaterial color="#151C2E" metalness={0.85} roughness={0.2} />
      </mesh>
      <mesh position={[0, 0.23 * rise, 0]} rotation={[Math.PI / 2, 0, 0]} scale={rise}>
        <torusGeometry args={[1.25, 0.035, 8, 64]} />
        <meshBasicMaterial color={color} />
      </mesh>
    </group>
  );
};

const StatScene: React.FC<Extract<ProceduralProps, {template: '3d_stat_callout'}>> = (props) => {
  const frame = useCurrentFrame();
  return (
    <group>
      <Pedestal frame={frame} color={props.brand.secondary} />
      <mesh position={[0, 0.25, 0]} rotation={[0, frame / 120, 0]} scale={spring({frame, fps: 30, delay: 4})}>
        <icosahedronGeometry args={[1.15, 1]} />
        <meshPhysicalMaterial color={props.brand.primary} metalness={0.55} roughness={0.15}
          clearcoat={1} emissive={props.brand.primary} emissiveIntensity={0.12} />
      </mesh>
      <pointLight position={[-4, 3, 5]} color={props.brand.secondary} intensity={70} />
      <pointLight position={[4, 0, -3]} color={props.brand.accent} intensity={45} />
    </group>
  );
};

const ComparisonScene: React.FC<Extract<ProceduralProps, {template: 'comparison_split'}>> = ({brand, winner}) => {
  const frame = useCurrentFrame();
  const tilt = spring({frame, fps: 30, config: {damping: 20}});
  return (
    <group>
      {[-1, 1].map((side, index) => (
        <group key={side} position={[side * 2.3, 0, 0]} rotation={[0, side * interpolate(tilt, [0, 1], [0.35, 0.08]), 0]}>
          <mesh scale={[2.8, 3.2, 0.24]}>
            <boxGeometry />
            <meshPhysicalMaterial color={winner === (index === 0 ? 'A' : 'B') ? brand.primary : '#182035'}
              metalness={0.55} roughness={0.3} clearcoat={0.8} />
          </mesh>
          <mesh position={[0, 1.65, 0.2]} scale={[2.4, 0.055, 0.055]}>
            <boxGeometry />
            <meshBasicMaterial color={index === 0 ? brand.secondary : brand.accent} />
          </mesh>
        </group>
      ))}
    </group>
  );
};

const NodeScene: React.FC<{count: number; colors: string[]; journey: boolean}> = ({count, colors, journey}) => {
  const frame = useCurrentFrame();
  const fps = useVideoConfig().fps;
  const drift = interpolate(frame, [0, Math.max(1, fps * 5)], journey ? [-2.2, 2.2] : [0, 0.3]);
  return (
    <group>
      {Array.from({length: count}, (_, index) => {
        const x = (index - (count - 1) / 2) * 2.15;
        const appear = spring({frame, fps, delay: index * 7, config: {damping: 17}});
        return (
          <group key={index} position={[x - drift, 0, 0]} scale={appear}>
            <mesh rotation={[frame / 90, frame / 100, 0]}>
              <dodecahedronGeometry args={[0.55, 0]} />
              <meshPhysicalMaterial color={colors[index % colors.length]} metalness={0.5} roughness={0.18}
                emissive={colors[index % colors.length]} emissiveIntensity={0.14} />
            </mesh>
            <mesh position={[0, -1.15, 0]}>
              <cylinderGeometry args={[0.42, 0.62, 0.24, 8]} />
              <meshStandardMaterial color="#171E30" metalness={0.7} roughness={0.25} />
            </mesh>
          </group>
        );
      })}
      {Array.from({length: Math.max(0, count - 1)}, (_, index) => (
        <mesh key={`link-${index}`} position={[(index - (count - 2) / 2) * 2.15, 0, -0.3]}>
          <boxGeometry args={[1.2, 0.055, 0.055]} />
          <meshBasicMaterial color={colors[index % colors.length]} />
        </mesh>
      ))}
    </group>
  );
};

const EvidenceScene: React.FC<{color: string}> = ({color}) => {
  const frame = useCurrentFrame();
  return (
    <group rotation={[0.05, interpolate(frame, [0, 150], [0.25, -0.25]), 0]}>
      <mesh scale={[5.7, 3.15, 0.28]}>
        <boxGeometry />
        <meshPhysicalMaterial color="#10182A" metalness={0.45} roughness={0.16} clearcoat={1} />
      </mesh>
      <mesh position={[-2.65, 0, 0.18]} scale={[0.07, 2.8, 0.07]}>
        <boxGeometry />
        <meshBasicMaterial color={color} />
      </mesh>
      <pointLight position={[2, 2, 4]} color={color} intensity={50} />
    </group>
  );
};

const CharacterExplainer: React.FC<Extract<ProceduralProps, {template: '2d_character_explainer'}>> = (props) => {
  const frame = useCurrentFrame();
  const fps = useVideoConfig().fps;
  const breath = 1 + interpolate(spring({frame, fps, config: {damping: 12, mass: 0.8}}), [0, 1], [0, 0.02]);
  const mouthRange = props.mouthStateTimeline.find((state) => frame >= state.startFrame && frame <= state.endFrame);
  const mouthScale = mouthRange?.mouth === 'wide' ? 1.0 : mouthRange?.mouth === 'open' ? 0.7
    : mouthRange?.mouth === 'half' ? 0.38 : 0.12;
  const blink = frame % (fps * 4) > fps * 3.82 ? 0.12 : 1;
  return (
    <group scale={breath}>
      <mesh position={[0, -1.0, 0]} scale={[2.4, 2.8, 0.18]}>
        <sphereGeometry args={[1, 32, 32]} />
        <meshStandardMaterial color={props.brand.primary} roughness={0.28} metalness={0.08} />
      </mesh>
      <mesh position={[0, 0.95, 0.18]} scale={[1.7, 1.55, 0.2]}>
        <sphereGeometry args={[1, 32, 32]} />
        <meshStandardMaterial color="#E9C5A8" roughness={0.5} />
      </mesh>
      {[-0.55, 0.55].map((x) => (
        <mesh key={x} position={[x, 1.05, 0.39]} scale={[0.16, 0.22 * blink, 0.06]}>
          <sphereGeometry args={[1, 16, 16]} />
          <meshBasicMaterial color="#172033" />
        </mesh>
      ))}
      <mesh position={[0, 0.35, 0.42]} scale={[0.52, 0.22 * mouthScale, 0.08]}>
        <sphereGeometry args={[1, 20, 12]} />
        <meshBasicMaterial color="#44202A" />
      </mesh>
      <mesh position={[0, -2.05, 0]} scale={[3.1, 0.18, 0.18]}>
        <boxGeometry />
        <meshBasicMaterial color={props.brand.accent} />
      </mesh>
    </group>
  );
};

const HookTitle: React.FC<Extract<ProceduralProps, {template: 'kinetic_hook_title'}>> = (props) => {
  const frame = useCurrentFrame();
  const fps = useVideoConfig().fps;
  const punch = spring({frame, fps, config: {damping: 11, mass: 0.65}});
  const titleStyle: React.CSSProperties = {
    color: '#F8FAFC',
    fontFamily: 'Arial, sans-serif',
    fontSize: props.aspect === '9:16' ? 'clamp(58px,11vw,150px)' : 'clamp(50px,7vw,120px)',
    fontWeight: 900,
    textAlign: 'center',
    textTransform: 'uppercase',
    transform: `scale(${interpolate(punch, [0, 1], [0.58, 1])})`,
    textShadow: `0 14px 70px ${props.brand.primary}CC`,
  };
  return (
    <AbsoluteFill style={{justifyContent: 'center', alignItems: 'center', background: 'rgba(5,8,18,.82)',
      borderTop: `12px solid ${props.brand.accent}`, borderBottom: `12px solid ${props.brand.secondary}`}}>
      <div style={{maxWidth: '86%', ...titleStyle}}>{props.hookText}</div>
      <div style={{marginTop: 28, color: props.brand.accent, fontWeight: 900, letterSpacing: '.22em'}}>{props.kicker}</div>
    </AbsoluteFill>
  );
};

const Overlay: React.FC<{props: ProceduralProps}> = ({props}) => {
  const frame = useCurrentFrame();
  const enter = spring({frame, fps: 30, config: {damping: 18, mass: 0.7}});
  const titleStyle: React.CSSProperties = {
    color: '#F8FAFC', fontSize: 'clamp(34px, 4.4vw, 82px)', fontWeight: 800, lineHeight: 1.06,
    maxWidth: '80%', textShadow: '0 8px 36px rgba(0,0,0,.62)', transform: `translateY(${(1 - enter) * 32}px)`,
  };
  let subtitle = '';
  if (props.template === '3d_bar_chart' || props.template === '3d_stat_callout') subtitle = props.subtitle;
  if (props.template === 'quote_evidence_card') subtitle = props.context;
  const value = props.template === '3d_stat_callout'
    ? `${props.prefix}${Math.round(interpolate(enter, [0, 1], [0, props.value])).toLocaleString()}${props.suffix}`
    : '';
  const details = props.template === '3d_bar_chart'
    ? props.bars.map((bar) => bar.label).join('     /     ')
    : props.template === 'comparison_split'
      ? `${props.left.label}: ${props.left.metrics.map((metric) => `${metric.label} ${metric.value}`).join(' · ')}`
        + `     VS     ${props.right.label}: ${props.right.metrics.map((metric) => `${metric.label} ${metric.value}`).join(' · ')}`
      : props.template === 'timeline_journey'
        ? props.milestones.map((item) => `${item.year}  ${item.label}`).join('     /     ')
        : props.template === 'process_flow'
          ? props.steps.map((item) => item.label).join('     →     ')
          : props.template === 'quote_evidence_card' ? props.source : '';
  const barMax = props.template === '3d_bar_chart'
    ? Math.max(...props.bars.map((bar) => Math.abs(bar.value)), 1)
    : 1;
  const winnerLabel = props.template === 'comparison_split'
    ? (props.winner === 'A' ? props.left.label : props.right.label)
    : '';
  return (
    <AbsoluteFill style={{justifyContent: 'space-between', padding: '7% 8%', fontFamily: 'Arial, sans-serif',
      background: 'linear-gradient(180deg,rgba(8,11,21,.78),transparent 42%,rgba(8,11,21,.42))'}}>
      <div>
        <div style={{color: props.brand.secondary, fontSize: 'clamp(15px,1.35vw,25px)', fontWeight: 700,
          letterSpacing: '.18em', textTransform: 'uppercase'}}>{props.channelName || 'STUDIO'}</div>
        <div style={{...titleStyle, marginTop: 18}}>{props.title}</div>
        {value ? <div style={{color: props.brand.accent, fontSize: 'clamp(34px,4vw,74px)', fontWeight: 800,
          marginTop: 16, textShadow: `0 0 32px ${props.brand.primary}88`}}>{value}</div> : null}
      </div>
      {props.template === '3d_bar_chart' ? (
        <div style={{position: 'absolute', inset: '0 8% 0', pointerEvents: 'none'}}>
          {props.bars.map((bar, index) => {
            const count = Math.round(interpolate(enter, [0, 1], [0, bar.value]));
            const bottom = 25 + (Math.abs(bar.value) / barMax) * 29;
            return (
              <div key={`${bar.label}-${index}`} style={{position: 'absolute',
                left: `${((index + 0.5) / props.bars.length) * 100}%`, bottom: `${bottom}%`,
                transform: 'translateX(-50%)', textAlign: 'center', whiteSpace: 'nowrap',
                color: '#F8FAFC', fontWeight: 800, fontSize: 'clamp(14px,1.4vw,26px)',
                textShadow: `0 2px 14px ${props.brand.primary}CC`}}>
                {count}{props.unit}
              </div>
            );
          })}
        </div>
      ) : null}
      {props.template === 'quote_evidence_card' ? (
        <div style={{position: 'absolute', top: '43%', left: '17%', right: '13%',
          transform: `translateY(${(1 - enter) * 18}px)`, color: '#F8FAFC',
          fontSize: 'clamp(18px,2.1vw,38px)', fontWeight: 600, lineHeight: 1.35,
          textShadow: '0 4px 24px rgba(0,0,0,.8)'}}>“{props.quote}”</div>
      ) : null}
      <div style={{maxWidth: '82%', color: '#D5DCEC', fontSize: 'clamp(15px,1.55vw,28px)', lineHeight: 1.45}}>
        {subtitle ? <div style={{marginBottom: 10}}>{subtitle}</div> : null}
        {winnerLabel ? <div style={{display: 'inline-block', marginBottom: 8, padding: '6px 12px',
          borderRadius: 999, background: `${props.brand.accent}33`, color: props.brand.accent,
          fontWeight: 800, fontSize: '.72em'}}>WINNER · {winnerLabel}</div> : null}
        <div style={{color: '#A8B3C9', fontSize: '0.8em'}}>{details}</div>
      </div>
    </AbsoluteFill>
  );
};

export const ProceduralScene: React.FC<ProceduralProps> = (props) => {
  const {width, height} = useVideoConfig();
  const frame = useCurrentFrame();
  const cameraDistance = props.aspect === '9:16' ? 24 : 13;
  const camera = props.template === 'timeline_journey'
    ? {position: [interpolate(frame, [0, props.durationInSeconds * 30], [-1.4, 1.4]), 0.25, cameraDistance] as [number, number, number]}
    : {position: [0, 0, cameraDistance] as [number, number, number]};
  return (
    <AbsoluteFill style={{background, overflow: 'hidden'}}>
      <Sequence from={0} durationInFrames={Math.max(1, Math.round(props.durationInSeconds * 30))} layout="none">
        <ThreeCanvas width={width} height={height} camera={camera} style={{position: 'absolute', inset: 0}}>
          <ambientLight intensity={1.4} />
          <directionalLight position={[4, 8, 6]} intensity={3} color="#E4ECFF" />
          {props.template === '3d_bar_chart' ? <BarChart {...props} /> : null}
          {props.template === '3d_stat_callout' ? <StatScene {...props} /> : null}
          {props.template === 'comparison_split' ? <ComparisonScene {...props} /> : null}
          {props.template === 'timeline_journey'
            ? <NodeScene count={props.milestones.length} colors={[props.brand.primary, props.brand.secondary]} journey />
            : null}
          {props.template === 'process_flow'
            ? <NodeScene count={props.steps.length} colors={[props.brand.secondary, props.brand.accent]} journey={false} />
            : null}
          {props.template === 'quote_evidence_card' ? <EvidenceScene color={props.brand.secondary} /> : null}
          {props.template === '2d_character_explainer' ? <CharacterExplainer {...props} /> : null}
        </ThreeCanvas>
      </Sequence>
      {props.template === 'kinetic_hook_title' ? <HookTitle {...props} /> : null}
      <Overlay props={props} />
    </AbsoluteFill>
  );
};
