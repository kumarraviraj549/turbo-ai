import {z} from 'zod';

const BrandSchema = z.object({
  primary: z.string().regex(/^#[0-9a-fA-F]{6}$/),
  secondary: z.string().regex(/^#[0-9a-fA-F]{6}$/),
  accent: z.string().regex(/^#[0-9a-fA-F]{6}$/),
}).strict();

const Common = {
  durationInSeconds: z.number().min(1).max(30),
  aspect: z.enum(['16:9', '9:16']),
  channelName: z.string().max(100),
  title: z.string().min(1).max(160),
  brand: BrandSchema,
};

const MetricSchema = z.object({
  label: z.string().min(1).max(80),
  value: z.union([z.string().max(80), z.number().finite()]),
}).strict();

export const ProceduralPropsSchema = z.discriminatedUnion('template', [
  z.object({
    ...Common,
    template: z.literal('3d_bar_chart'),
    subtitle: z.string().max(200),
    unit: z.string().max(20),
    bars: z.array(z.object({label: z.string().min(1).max(40), value: z.number().finite()}).strict()).min(1).max(8),
  }).strict(),
  z.object({
    ...Common,
    template: z.literal('3d_stat_callout'),
    value: z.number().finite(),
    prefix: z.string().max(12),
    suffix: z.string().max(12),
    subtitle: z.string().max(200),
  }).strict(),
  z.object({
    ...Common,
    template: z.literal('comparison_split'),
    left: z.object({label: z.string().min(1).max(60), metrics: z.array(MetricSchema).max(5)}).strict(),
    right: z.object({label: z.string().min(1).max(60), metrics: z.array(MetricSchema).max(5)}).strict(),
    winner: z.enum(['A', 'B']),
  }).strict(),
  z.object({
    ...Common,
    template: z.literal('timeline_journey'),
    milestones: z.array(z.object({
      year: z.string().min(1).max(20),
      label: z.string().min(1).max(60),
      caption: z.string().max(120),
    }).strict()).min(2).max(8),
  }).strict(),
  z.object({
    ...Common,
    template: z.literal('process_flow'),
    steps: z.array(z.object({
      label: z.string().min(1).max(60),
      caption: z.string().max(120),
    }).strict()).min(2).max(8),
  }).strict(),
  z.object({
    ...Common,
    template: z.literal('quote_evidence_card'),
    quote: z.string().min(1).max(500),
    source: z.string().min(1).max(300),
    context: z.string().max(200),
  }).strict(),
  z.object({
    ...Common,
    template: z.literal('2d_character_explainer'),
    characterName: z.string().max(80),
    characterSvg: z.string().max(3000).optional(),
    emotion: z.enum(['neutral', 'happy', 'shocked', 'explaining', 'serious']),
    mouthStateTimeline: z.array(z.object({
      startFrame: z.number().int().nonnegative(),
      endFrame: z.number().int().nonnegative(),
      mouth: z.enum(['closed', 'half', 'open', 'wide']),
    }).strict()).max(200),
  }).strict(),
  z.object({
    ...Common,
    template: z.literal('kinetic_hook_title'),
    hookText: z.string().min(1).max(160),
    kicker: z.string().max(80),
  }).strict(),
]);

export type ProceduralProps = z.infer<typeof ProceduralPropsSchema>;

export const CompositionPropsSchema = z.object({
  ...Common,
  template: z.enum([
    '3d_bar_chart', '3d_stat_callout', 'comparison_split', 'timeline_journey',
    'process_flow', 'quote_evidence_card', '2d_character_explainer', 'kinetic_hook_title',
  ]),
  subtitle: z.string().max(200).optional(),
  unit: z.string().max(20).optional(),
  bars: z.array(z.object({label: z.string().min(1).max(40), value: z.number().finite()}).strict()).min(1).max(8).optional(),
  value: z.number().finite().optional(),
  prefix: z.string().max(12).optional(),
  suffix: z.string().max(12).optional(),
  left: z.object({label: z.string().min(1).max(60), metrics: z.array(MetricSchema).max(5)}).strict().optional(),
  right: z.object({label: z.string().min(1).max(60), metrics: z.array(MetricSchema).max(5)}).strict().optional(),
  winner: z.enum(['A', 'B']).optional(),
  milestones: z.array(z.object({
    year: z.string().min(1).max(20),
    label: z.string().min(1).max(60),
    caption: z.string().max(120),
  }).strict()).min(2).max(8).optional(),
  steps: z.array(z.object({
    label: z.string().min(1).max(60),
    caption: z.string().max(120),
  }).strict()).min(2).max(8).optional(),
  quote: z.string().min(1).max(500).optional(),
  source: z.string().min(1).max(300).optional(),
  context: z.string().max(200).optional(),
  characterName: z.string().max(80).optional(),
  characterSvg: z.string().max(3000).optional(),
  emotion: z.enum(['neutral', 'happy', 'shocked', 'explaining', 'serious']).optional(),
  mouthStateTimeline: z.array(z.object({
    startFrame: z.number().int().nonnegative(),
    endFrame: z.number().int().nonnegative(),
    mouth: z.enum(['closed', 'half', 'open', 'wide']),
  }).strict()).max(200).optional(),
  hookText: z.string().min(1).max(160).optional(),
  kicker: z.string().max(80).optional(),
}).strict();

export type CompositionProps = z.infer<typeof CompositionPropsSchema>;

const templateDefaults: Record<ProceduralProps['template'], Record<string, unknown>> = {
  '3d_bar_chart': {
    subtitle: 'A measurable shift over time',
    unit: '%',
    bars: [{label: '2019', value: 38}, {label: '2021', value: 62}, {label: '2024', value: 91}],
  },
  '3d_stat_callout': {value: 42, prefix: '', suffix: '', subtitle: 'A key figure'},
  comparison_split: {
    left: {label: 'Option A', metrics: []},
    right: {label: 'Option B', metrics: []},
    winner: 'A',
  },
  timeline_journey: {
    milestones: [{year: 'Then', label: 'Starting point', caption: ''}, {year: 'Now', label: 'Outcome', caption: ''}],
  },
  process_flow: {steps: [{label: 'Discover', caption: ''}, {label: 'Outcome', caption: ''}]},
  quote_evidence_card: {quote: 'Verified story evidence', source: 'Story source', context: ''},
  '2d_character_explainer': {
    characterName: 'Explainer',
    emotion: 'explaining',
    mouthStateTimeline: [],
  },
  kinetic_hook_title: {hookText: 'The hidden truth', kicker: 'WATCH THIS'},
};

export const resolveProceduralProps = (props: CompositionProps): ProceduralProps => {
  const parsed = CompositionPropsSchema.parse(props);
  return ProceduralPropsSchema.parse({...templateDefaults[parsed.template], ...parsed});
};

export const DEFAULT_COMPOSITION_PROPS: CompositionProps = {
  template: '3d_bar_chart',
  durationInSeconds: 5,
  aspect: '16:9',
  channelName: 'TubeForge',
  title: 'A growing signal',
  brand: {primary: '#8B5CF6', secondary: '#22D3EE', accent: '#F472B6'},
};
