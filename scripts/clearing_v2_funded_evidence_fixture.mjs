import { createServer } from '../frontend/node_modules/vite/dist/node/index.js';
import vue from '../frontend/node_modules/@vitejs/plugin-vue/dist/index.mjs';
import { fileURLToPath } from 'node:url';
const root = fileURLToPath(new URL('../frontend', import.meta.url));
const server = await createServer({ configFile: false, root, plugins: [vue()], server: { host: '127.0.0.1', port: 8131, strictPort: true, hmr: false }, appType: 'mpa' });
await server.listen();
console.log('Synthetic readonly component fixture: http://127.0.0.1:8131/tests/fixtures/funded-combinations.html; no backend/database');
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, async () => { await server.close(); process.exit(0); });
