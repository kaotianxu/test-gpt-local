// Step 4: merge selected optional features into plugins/claude-hud/config.json
const fs = require('fs');
const path = 'C:/Users/20906/.claude/plugins/claude-hud/config.json';

let cfg = {};
if (fs.existsSync(path)) {
	const raw = fs.readFileSync(path, 'utf8');
	if (raw.charCodeAt(0) === 0xFEFF) {
		console.error('ABORT: existing config.json has a UTF-8 BOM');
		process.exit(1);
	}
	cfg = raw.trim() ? JSON.parse(raw) : {};
}
cfg.display = cfg.display || {};

// Selected: Tools activity, Agents & Todos, Session info + name, Custom line "token消耗"
cfg.display.showTools = true;
cfg.display.showAgents = true;
cfg.display.showTodos = true;
cfg.display.showDuration = true;
cfg.display.showConfigCounts = true;
cfg.display.showSessionName = true;
cfg.display.customLine = 'token消耗';

// Preserve showUsage=false default (user did not request showUsage)
// (Already in existing config; we don't touch it.)

const out = JSON.stringify(cfg, null, 2);
fs.writeFileSync(path, out, { encoding: 'utf8' });
console.log('Wrote ' + out.length + ' bytes to ' + path);
console.log('--- new config.json ---');
console.log(out);

// Verify
const verify = fs.readFileSync(path);
const first4 = Array.from(verify.slice(0, 4)).map(b => b.toString(16).padStart(2, '0').toUpperCase()).join(' ');
console.log('First 4 bytes: ' + first4);
console.log('BOM: ' + (verify[0] === 0xEF ? 'PRESENT (BAD)' : 'absent (good)'));
JSON.parse(fs.readFileSync(path, 'utf8'));
console.log('JSON re-parse: OK');
