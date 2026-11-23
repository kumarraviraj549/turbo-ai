import React from 'react';
import {Composition} from 'remotion';
import {CompositionPropsSchema, DEFAULT_COMPOSITION_PROPS, resolveProceduralProps, type CompositionProps} from './schema';
import {ProceduralScene} from './scene';

const RenderScene: React.FC<CompositionProps> = (props) => <ProceduralScene {...resolveProceduralProps(props)} />;

export const Root: React.FC = () => (
  <Composition
    id="ProceduralScene"
    component={RenderScene}
    schema={CompositionPropsSchema}
    defaultProps={DEFAULT_COMPOSITION_PROPS}
    width={1920}
    height={1080}
    fps={30}
    durationInFrames={150}
    calculateMetadata={({props}: {props: CompositionProps}) => {
      const validated = resolveProceduralProps(props);
      return {
        durationInFrames: Math.max(1, Math.round(validated.durationInSeconds * 30)),
        width: validated.aspect === '9:16' ? 1080 : 1920,
        height: validated.aspect === '9:16' ? 1920 : 1080,
      };
    }}
  />
);
