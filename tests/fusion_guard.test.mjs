import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const pluginPath = path.resolve(import.meta.dirname, '../plugins/fusion-benchmark-guard.mjs');
const fusion = 'autodeskfusion_fusion_mcp_execute';
const skill = 'C:\\benchmark-test\\skills\\autodeskfusion\\SKILL.md';
const baseTime = 1_800_000_000_000;

async function setup(t, overrides = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fusion-guard-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const workspace = path.join(root, 'workspace');
  fs.mkdirSync(workspace);
  const contract = {
    run_id: 'run-test', case_id: 'Q01', attempt_id: 'attempt-1',
    workspace, audit_dir: path.join(root, 'audit'), max_tool_calls: 200,
    max_seconds: 1800, model: 'lanyi/gpt-6.1-sol', started_at: baseTime,
    allowed_read_paths: [skill], ...overrides,
  };
  assert.ok(fs.existsSync(pluginPath), 'The guard plugin must exist');
  const mod = await import(pathToFileURL(pluginPath).href);
  assert.deepEqual(Object.keys(mod), ['default'], 'OpenCode must see only the plugin export');
  let now = baseTime;
  const hooks = mod.default.createGuard(contract, { now: () => now });
  const state = () => JSON.parse(fs.readFileSync(path.join(contract.audit_dir, 'guard-state.json'), 'utf8'));
  const events = () => fs.readFileSync(path.join(contract.audit_dir, 'guard-events.jsonl'), 'utf8').trim().split('\n').map(JSON.parse);
  const input = (callID, tool = fusion) => ({ sessionID: 'session-1', callID, tool });
  const bind = () => hooks['chat.message']({ sessionID: 'session-1', model: { providerID: 'lanyi', modelID: 'gpt-6.1-sol' } }, {});
  const before = (callID, tool = fusion, args = { code: 'secret-code-should-not-be-in-audit' }) => hooks['tool.execute.before'](input(callID, tool), { args });
  const after = (callID, output = { content: [{ type: 'text', text: 'ok' }], isError: false }, tool = fusion) => hooks['tool.execute.after']({ ...input(callID, tool), args: {} }, output);
  return { root, workspace, contract, hooks, state, events, input, bind, before, after, mod, setTime: value => { now = value; } };
}

test('loads one public contract and persists its digest without raw parameters', async t => {
  const g = await setup(t);
  assert.equal(g.events()[0].type, 'guard.loaded');
  assert.match(g.state().contract_sha256, /^[a-f0-9]{64}$/);
  assert.equal(g.state().started_at, baseTime);
  assert.equal(g.state().deadline, baseTime + 1_800_000);
  await g.bind();
  await g.before('one');
  assert.equal(g.state().pendingFusion, 1);
  assert.equal(g.state().total_tool_calls, 1);
  assert.equal(g.state().fusion_tool_calls, 1);
  assert.ok(g.events().some(event => event.type === 'tool.request' && event.callID === 'one'));
  const journal = fs.readFileSync(path.join(g.contract.audit_dir, 'guard-events.jsonl'), 'utf8');
  assert.ok(!journal.includes('secret-code-should-not-be-in-audit'));
});

test('rejects tools until the required model is verified and rejects session switching', async t => {
  const g = await setup(t);
  await assert.rejects(g.before('unverified'), /MODEL_NOT_VERIFIED/);
  await assert.rejects(g.hooks['chat.message']({ sessionID: 'session-1', model: { providerID: 'other', modelID: 'gpt-6.1-sol' } }, {}), /MODEL_MISMATCH/);
  await g.bind();
  await assert.rejects(g.hooks['chat.message']({ sessionID: 'session-2', model: { providerID: 'lanyi', modelID: 'gpt-6.1-sol' } }, {}), /SESSION_MISMATCH/);
  await assert.rejects(g.hooks['tool.execute.before']({ ...g.input('different'), sessionID: 'session-2' }, { args: {} }), /SESSION_MISMATCH/);
});

test('serializes Fusion calls while allowing safe non-Fusion work', async t => {
  const g = await setup(t);
  await g.bind();
  await g.before('first');
  await assert.rejects(g.before('overlap'), /FUSION_PENDING/);
  await g.before('read', 'read', { filePath: path.join(g.workspace, 'drawing.dxf') });
  await g.after('read', { title: 'drawing', output: 'public', metadata: {} }, 'read');
  assert.equal(g.state().pendingFusion, 1);
  await g.after('first');
  await g.before('next');
  assert.equal(g.state().pendingFusion, 1);
  assert.equal(g.state().fusion_tool_calls, 3);
});

test('duplicate call IDs cannot execute again after completion or restart', async t => {
  const g = await setup(t);
  await g.bind();
  await g.before('once');
  await g.after('once');
  await assert.rejects(g.before('once'), /DUPLICATE_CALL/);
  const reloaded = g.mod.default.createGuard(g.contract, { now: () => baseTime });
  await assert.rejects(reloaded['tool.execute.before'](g.input('once'), { args: {} }), /DUPLICATE_CALL/);
  assert.equal(g.state().total_tool_calls, 1);
});

test('the tool budget counts all unique requests including denied tools', async t => {
  const g = await setup(t, { max_tool_calls: 2 });
  await g.bind();
  await assert.rejects(g.before('web', 'webfetch', {}), /TOOL_DENIED/);
  await g.before('read', 'read', { filePath: 'TASK.md' });
  await g.after('read', { title: '', output: '', metadata: {} }, 'read');
  await assert.rejects(g.before('over-budget', 'read', { filePath: 'TASK.md' }), /TOOL_BUDGET/);
  assert.equal(g.state().total_tool_calls, 3);
  assert.equal(g.state().pendingFusion, 0);
});

test('deadline is checked before allowing a tool and never resets on reload', async t => {
  const g = await setup(t, { max_seconds: 1 });
  await g.bind();
  g.setTime(baseTime + 1000);
  await assert.rejects(g.before('late'), /TIME_BUDGET/);
  const reloaded = g.mod.default.createGuard(g.contract, { now: () => baseTime + 2000 });
  await assert.rejects(reloaded['tool.execute.before'](g.input('later'), { args: {} }), /TIME_BUDGET/);
  assert.equal(g.state().started_at, baseTime);
});

test('MCP errors and ordinary results persist terminal status without raw output', async t => {
  const g = await setup(t);
  await g.bind();
  await g.before('error');
  await g.after('error', { content: [{ type: 'text', text: 'private-result-text' }], isError: true });
  assert.equal(g.state().calls.error.status, 'error');
  assert.equal(g.state().calls.error.isError, true);
  assert.equal(g.state().pendingFusion, 0);
  await assert.rejects(g.after('error'), /DUPLICATE_TERMINAL/);
  assert.ok(!JSON.stringify(g.events()).includes('private-result-text'));
});

test('missing completion and timeout responses leave Fusion pending after restart', async t => {
  const g = await setup(t);
  await g.bind();
  await g.before('timeout');
  await g.after('timeout', { isError: true, content: [{ type: 'text', text: 'Request timed out' }] });
  assert.equal(g.state().calls.timeout.status, 'unknown');
  assert.equal(g.state().pendingFusion, 1);
  const reloaded = g.mod.default.createGuard(g.contract, { now: () => baseTime + 100 });
  await assert.rejects(reloaded['tool.execute.before'](g.input('retry'), { args: {} }), /FUSION_PENDING/);
});

test('only the four discovered Fusion tools and named local tools are permitted', async t => {
  const g = await setup(t);
  await g.bind();
  for (const [n, tool] of ['task', 'solidworks_execute', 'autodeskfusion_new_tool', 'websearch', 'question'].entries()) {
    await assert.rejects(g.before(`deny-${n}`, tool, {}), /TOOL_DENIED/);
  }
  for (const tool of ['fusion_mcp_electronics_read', 'fusion_mcp_execute', 'fusion_mcp_read', 'fusion_mcp_update']) {
    await g.before(tool, `autodeskfusion_${tool}`, {});
    await g.after(tool, { isError: false, content: [] }, `autodeskfusion_${tool}`);
  }
});

test('file tools reject escaping paths including future files and symbolic links', async t => {
  const g = await setup(t);
  await g.bind();
  await assert.rejects(g.before('escape', 'read', { filePath: '../private.txt' }), /PATH_DENIED/);
  await assert.rejects(g.before('write-escape', 'write', { filePath: path.join(g.root, 'future', 'private.txt'), content: '' }), /PATH_DENIED/);
  await assert.rejects(g.before('glob-escape', 'glob', { pattern: '../**/*' }), /PATH_DENIED/);
  await assert.rejects(g.before('grep-escape', 'grep', { path: g.root, pattern: 'anything' }), /PATH_DENIED/);
  const outside = path.join(g.root, 'outside');
  fs.mkdirSync(outside);
  fs.symlinkSync(outside, path.join(g.workspace, 'link'), process.platform === 'win32' ? 'junction' : 'dir');
  await assert.rejects(g.before('junction-escape', 'write', { filePath: path.join(g.workspace, 'link', 'new.txt'), content: '' }), /PATH_DENIED/);
  await g.before('okay', 'write', { filePath: path.join(g.workspace, 'new', 'model.py'), content: 'safe-public-code' });
});

test('apply_patch validates every affected path including Move to', async t => {
  const g = await setup(t);
  await g.bind();
  await assert.rejects(g.before('bad-patch', 'apply_patch', { patchText: '*** Begin Patch\n*** Add File: ok.txt\n+hello\n*** Add File: ../private.txt\n+bad\n*** End Patch' }), /PATH_DENIED/);
  await assert.rejects(g.before('bad-move', 'apply_patch', { patchText: '*** Begin Patch\n*** Update File: ok.txt\n*** Move to: ../private.txt\n@@\n-old\n+new\n*** End Patch' }), /PATH_DENIED/);
  await g.before('okay-patch', 'apply_patch', { patchText: '*** Begin Patch\n*** Add File: ok.txt\n+hello\n*** End Patch' });
});

test('only the explicitly permitted Autodesk Fusion skill can be loaded', async t => {
  const g = await setup(t);
  await g.bind();
  await g.before('skill-read', 'read', { filePath: skill });
  await g.before('skill', 'skill', { name: 'autodeskfusion' });
  await assert.rejects(g.before('other-skill', 'skill', { name: 'anything-else' }), /SKILL_DENIED/);
  await assert.rejects(g.before('skill-write', 'write', { filePath: skill, content: 'bad' }), /PATH_DENIED/);
});

test('bash supports DXF parsing in the workspace but rejects an explicit outside working directory', async t => {
  const g = await setup(t);
  await g.bind();
  await g.before('parse', 'bash', { command: 'python -c "import ezdxf; print(ezdxf.readfile(\'drawing.dxf\').dxfversion)"', workdir: g.workspace });
  await assert.rejects(g.before('outside-cwd', 'bash', { command: 'python parse.py', workdir: g.root }), /PATH_DENIED/);
  assert.equal(g.events()[0].shell_is_os_sandbox, false);
});

test('an existing transaction lock causes fail closed without removing the other lock', async t => {
  const g = await setup(t);
  await g.bind();
  const lock = path.join(g.contract.audit_dir, 'guard.lock');
  fs.writeFileSync(lock, 'another-process', { flag: 'wx' });
  await assert.rejects(g.before('locked'), /AUDIT_LOCKED/);
  assert.equal(fs.readFileSync(lock, 'utf8'), 'another-process');
});

test('a changed contract or truncated journal refuses recovery', async t => {
  const g = await setup(t);
  assert.throws(() => g.mod.default.createGuard({ ...g.contract, case_id: 'Q02' }, { now: () => baseTime }), /CONTRACT_CHANGED/);
  fs.appendFileSync(path.join(g.contract.audit_dir, 'guard-events.jsonl'), '{broken');
  assert.throws(() => g.mod.default.createGuard(g.contract, { now: () => baseTime }), /AUDIT_CORRUPT/);
});

test('a missing audit journal cannot reset an existing attempt', async t => {
  const g = await setup(t);
  await g.bind();
  await g.before('pending');
  fs.unlinkSync(path.join(g.contract.audit_dir, 'guard-events.jsonl'));
  assert.throws(() => g.mod.default.createGuard(g.contract, { now: () => baseTime }), /AUDIT_MISSING/);
});

test('an unrecognizable Fusion result remains uncertain instead of releasing the queue', async t => {
  const g = await setup(t);
  await g.bind();
  await g.before('pending');
  await g.after('pending', {});
  assert.equal(g.state().calls.pending.status, 'unknown');
  assert.equal(g.state().pendingFusion, 1);
});

test('chat.params checks the actual model immediately before model invocation', async t => {
  const g = await setup(t);
  await g.bind();
  await assert.rejects(g.hooks['chat.params']({ sessionID: 'session-1', model: { providerID: 'lanyi', id: 'other' } }, {}), /MODEL_MISMATCH/);
  await g.hooks['chat.params']({ sessionID: 'session-1', model: { providerID: 'lanyi', id: 'gpt-6.1-sol' } }, {});
});

test('two plugin instances sharing an audit directory cannot overlap Fusion execution', async t => {
  const g = await setup(t);
  await g.bind();
  const other = g.mod.default.createGuard(g.contract, { now: () => baseTime });
  const outcomes = await Promise.allSettled([
    g.before('first-instance'),
    other['tool.execute.before'](g.input('second-instance'), { args: {} }),
  ]);
  assert.equal(outcomes.filter(result => result.status === 'fulfilled').length, 1);
  assert.equal(outcomes.filter(result => result.status === 'rejected').length, 1);
  assert.equal(g.state().pendingFusion, 1);
});

test('invalid session identifiers cannot create unreplayable audit events', async t => {
  const g = await setup(t);
  await g.bind();
  await assert.rejects(g.hooks['tool.execute.before']({ callID: 'missing-session', tool: fusion }, { args: {} }), /INVALID_SESSION/);
  assert.doesNotThrow(() => g.mod.default.createGuard(g.contract, { now: () => baseTime }));
});

test('the plugin initializes from a contract file without exporting helper functions', async t => {
  const g = await setup(t);
  const contractPath = path.join(g.root, 'contract.json');
  fs.writeFileSync(contractPath, JSON.stringify(g.contract));
  const old = process.env.FUSION_BENCHMARK_CONTRACT;
  t.after(() => { if (old === undefined) delete process.env.FUSION_BENCHMARK_CONTRACT; else process.env.FUSION_BENCHMARK_CONTRACT = old; });
  process.env.FUSION_BENCHMARK_CONTRACT = contractPath;
  const hooks = await g.mod.default({});
  assert.equal(typeof hooks['tool.execute.before'], 'function');
  assert.ok(g.events().some(event => event.type === 'guard.contract' && event.path === contractPath));
});
