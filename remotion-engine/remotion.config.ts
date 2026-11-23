import {Config} from '@remotion/cli/config';
import {spawnSync} from 'node:child_process';

const gpu = spawnSync('nvidia-smi', ['-L'], {stdio: 'ignore'}).status === 0;
Config.setChromiumOpenGlRenderer(gpu ? 'angle' : 'swangle');
Config.setOverwriteOutput(true);
