// Step 3: merge new statusLine, write UTF-8 no BOM
const fs = require('fs');
const path = 'C:/Users/20906/.claude/settings.json';
const raw = fs.readFileSync(path, 'utf8');

// Refuse to merge if input has BOM
if (raw.charCodeAt(0) === 0xFEFF) {
	console.error('ABORT: settings.json has a UTF-8 BOM, refusing to merge');
	process.exit(1);
}

const settings = JSON.parse(raw);

// New command per Windows + Git Bash branch (win32 + OSTYPE=msys)
const RUNTIME_PATH = '/c/nvm4w/nodejs/node.exe';
const SOURCE = 'dist/index.js';

// Build by concatenation (NOT template literal — bash ${VAR} breaks JS template)
const NEW_COMMAND =
	'cols=${COLUMNS:-}; case "$cols" in ""|*[!0-9]*) cols=$(stty size </dev/tty 2>/dev/null | awk \'{print $2}\');; esac; ' +
	'case "$cols" in ""|*[!0-9]*) cols=120;; esac; ' +
	'export COLUMNS=$(( cols > 4 ? cols - 4 : 1 )); ' +
	'plugin_dir=$(ls -1d "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"/plugins/cache/*/claude-hud/*/ 2>/dev/null | sort -V | tail -1); ' +
	'exec "' + RUNTIME_PATH + '" "${plugin_dir}' + SOURCE + '"';

settings.statusLine = { type: 'command', command: NEW_COMMAND };

const out = JSON.stringify(settings, null, 2);
fs.writeFileSync(path, out, { encoding: 'utf8' });
console.log('Wrote ' + out.length + ' bytes');
console.log('NEW command:');
console.log(NEW_COMMAND);

// Verify no BOM
const verify = fs.readFileSync(path);
const first4 = Array.from(verify.slice(0, 4)).map(b => b.toString(16).padStart(2, '0').toUpperCase()).join(' ');
console.log('First 4 bytes (hex): ' + first4);
console.log('BOM check: ' + (verify[0] === 0xEF ? 'PRESENT (BAD)' : 'absent (good)'));

// Also re-parse to confirm valid JSON
JSON.parse(fs.readFileSync(path, 'utf8'));
console.log('JSON re-parse: OK');
