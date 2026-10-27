const { spawn, spawnSync } = require('node:child_process');
const path = require('node:path');

const root = path.resolve(__dirname, '..', '..');
const electron = require('electron');
const viteBin = path.join(path.dirname(require.resolve('vite/package.json')), 'bin', 'vite.js');
const result = spawnSync(process.execPath, [viteBin, 'build'], { cwd: root, stdio: 'inherit' });
if (result.status !== 0) process.exit(result.status || 1);
const child = spawn(electron, ['.'], { cwd: root, stdio: 'inherit', windowsHide: false });
child.on('exit', code => process.exit(code || 0));
