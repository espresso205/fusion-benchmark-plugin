import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';

const MODEL = 'lanyi/gpt-6.1-sol';
const FUSION_TOOLS = new Set(['electronics_read', 'execute', 'read', 'update'].map(name => `autodeskfusion_fusion_mcp_${name}`));
const LOCAL_TOOLS = new Set(['read', 'write', 'edit', 'apply_patch', 'bash', 'glob', 'grep', 'skill', 'todowrite']);

function fail(code) {
  const error = new Error(`FUSION_GUARD:${code}`);
  error.code = code;
  throw error;
}

function stable(value) {
  if (value === null || typeof value !== 'object') return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`;
  return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${stable(value[key])}`).join(',')}}`;
}

const digest = value => createHash('sha256').update(stable(value)).digest('hex');

// Resolve the nearest existing ancestor too, so a junction followed by a new
// filename cannot escape the workspace. This is a tool-argument check, not an
// operating-system sandbox; arbitrary Python, shell and Fusion code remain code.
function canonical(filename) {
  let current = path.resolve(filename);
  const tail = [];
  while (!fs.existsSync(current)) {
    const parent = path.dirname(current);
    if (parent === current) fail('PATH_DENIED');
    tail.unshift(path.basename(current));
    current = parent;
  }
  return path.join(fs.realpathSync.native(current), ...tail);
}

function within(root, filename) {
  const rel = path.relative(root, filename);
  return rel === '' || (!path.isAbsolute(rel) && rel !== '..' && !rel.startsWith(`..${path.sep}`));
}

function validateContract(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail('INVALID_CONTRACT');
  for (const key of ['run_id', 'case_id', 'attempt_id', 'workspace', 'audit_dir']) {
    if (typeof value[key] !== 'string' || !value[key].trim()) fail('INVALID_CONTRACT');
  }
  if (value.model !== MODEL) fail('MODEL_MISMATCH');
  if (!Number.isInteger(value.max_tool_calls) || value.max_tool_calls < 1 || value.max_tool_calls > 200) fail('INVALID_BUDGET');
  if (!Number.isFinite(value.max_seconds) || value.max_seconds <= 0 || value.max_seconds > 1800) fail('INVALID_BUDGET');
  if (!path.isAbsolute(value.workspace) || !path.isAbsolute(value.audit_dir)) fail('INVALID_CONTRACT');
  if (!fs.statSync(value.workspace).isDirectory()) fail('INVALID_CONTRACT');
  const workspace = canonical(value.workspace);
  const auditDir = canonical(value.audit_dir);
  if (within(workspace, auditDir)) fail('AUDIT_IN_WORKSPACE');
  const permittedReads = value.allowed_read_paths ?? [];
  if (!Array.isArray(permittedReads) || permittedReads.some(item => typeof item !== 'string' || !path.isAbsolute(item) || !/[\\/]skills[\\/]autodeskfusion[\\/]SKILL\.md$/i.test(item))) fail('INVALID_READ_ALLOWLIST');
  return { workspace, auditDir, allowedReads: permittedReads.map(canonical) };
}

function applyEvent(state, event) {
  if (event.type === 'guard.loaded' && !state.contract_sha256) {
    Object.assign(state, { contract_sha256: event.contract_sha256, run_id: event.run_id, case_id: event.case_id,
      attempt_id: event.attempt_id, started_at: event.started_at, deadline: event.deadline });
  } else if (event.type === 'model.verified') {
    state.sessionID = event.sessionID;
    state.model_verified = true;
  } else if (event.type === 'tool.request') {
    state.total_tool_calls += 1;
    if (event.fusion) state.fusion_tool_calls += 1;
    state.calls[event.callID] = { tool: event.tool, sessionID: event.sessionID, args_sha256: event.args_sha256,
      fusion: event.fusion, status: 'pending', requested_at: event.at, permitted: false };
  } else if (event.type === 'tool.allowed') {
    state.calls[event.callID].permitted = true;
  } else if (event.type === 'tool.denied') {
    Object.assign(state.calls[event.callID], { status: 'denied', reason: event.reason, ended_at: event.at });
  } else if (event.type === 'tool.result') {
    Object.assign(state.calls[event.callID], { status: event.status, isError: event.isError,
      result_sha256: event.result_sha256, ended_at: event.at, terminal_seen: true });
  }
  state.pendingFusion = Object.values(state.calls).filter(call => call.fusion && ['pending', 'unknown'].includes(call.status)).length;
  state.last_sequence = event.sequence;
  state.last_event_sha256 = event.sha256;
}

function createGuard(contract, { now = Date.now, contractPath } = {}) {
  // Freeze our own copy so the caller cannot change policy after the digest.
  contract = JSON.parse(JSON.stringify(contract));
  const { workspace, auditDir, allowedReads } = validateContract(contract);
  const contractHash = digest(contract);
  fs.mkdirSync(auditDir, { recursive: true });
  const journalPath = path.join(auditDir, 'guard-events.jsonl');
  const statePath = path.join(auditDir, 'guard-state.json');
  const lockPath = path.join(auditDir, 'guard.lock');

  function transaction(action) {
    let lock;
    try { lock = fs.openSync(lockPath, 'wx'); } catch (error) {
      if (error.code === 'EEXIST') fail('AUDIT_LOCKED');
      fail('AUDIT_FAILURE');
    }
    try {
      const state = { version: 1, total_tool_calls: 0, fusion_tool_calls: 0, pendingFusion: 0,
        sessionID: null, model_verified: false, calls: Object.create(null), last_sequence: 0, last_event_sha256: null };
      if (fs.existsSync(statePath) && !fs.existsSync(journalPath)) fail('AUDIT_MISSING');
      if (fs.existsSync(journalPath)) {
        const raw = fs.readFileSync(journalPath, 'utf8');
        if (!raw && fs.existsSync(statePath)) fail('AUDIT_MISSING');
        if (raw && !raw.endsWith('\n')) fail('AUDIT_CORRUPT');
        for (const line of raw.split('\n').filter(Boolean)) {
          let event;
          try { event = JSON.parse(line); } catch { fail('AUDIT_CORRUPT'); }
          const { sha256, ...unsigned } = event;
          if (event.sequence !== state.last_sequence + 1 || event.previous_sha256 !== state.last_event_sha256 || sha256 !== digest(unsigned)) fail('AUDIT_CORRUPT');
          applyEvent(state, event);
        }
      }
      if (state.contract_sha256 && state.contract_sha256 !== contractHash) fail('CONTRACT_CHANGED');
      const emit = (type, data = {}) => {
        const unsigned = { type, at: now(), sequence: state.last_sequence + 1, previous_sha256: state.last_event_sha256, ...data };
        const event = { ...unsigned, sha256: digest(unsigned) };
        const fd = fs.openSync(journalPath, 'a');
        try { fs.writeFileSync(fd, `${JSON.stringify(event)}\n`); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
        applyEvent(state, event);
      };
      try { return action(state, emit); } finally {
        const temporary = `${statePath}.${process.pid}.tmp`;
        const fd = fs.openSync(temporary, 'w');
        try { fs.writeFileSync(fd, `${JSON.stringify(state, null, 2)}\n`); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
        fs.renameSync(temporary, statePath);
      }
    } finally {
      fs.closeSync(lock);
      fs.unlinkSync(lockPath);
    }
  }

  transaction((state, emit) => {
    const started = state.started_at ?? (contract.started_at === undefined ? now() : typeof contract.started_at === 'number' ? contract.started_at : Date.parse(contract.started_at));
    if (!Number.isFinite(started)) fail('INVALID_STARTED_AT');
    emit('guard.loaded', { contract_sha256: contractHash, run_id: contract.run_id, case_id: contract.case_id,
      attempt_id: contract.attempt_id, started_at: started, deadline: state.deadline ?? started + contract.max_seconds * 1000,
      shell_is_os_sandbox: false });
    emit('guard.contract', { contract_sha256: contractHash, path: contractPath ?? null });
  });

  function checkPath(filename, readOnly = false) {
    if (typeof filename !== 'string' || !filename || filename.includes('\0')) fail('PATH_DENIED');
    // Reject drive-relative paths, alternate data streams and Windows device paths.
    if (/^[a-z]:[^\\/]/i.test(filename) || /^\\\\[?.]\\/.test(filename) || /:(?![\\/])/.test(filename)) fail('PATH_DENIED');
    const resolved = canonical(path.resolve(workspace, filename));
    if (within(workspace, resolved)) return;
    if (readOnly && allowedReads.some(item => path.relative(item, resolved) === '')) return;
    fail('PATH_DENIED');
  }

  function checkArgs(tool, args) {
    if (!args || typeof args !== 'object' || Array.isArray(args)) fail('INVALID_ARGS');
    if (['read', 'write', 'edit'].includes(tool)) {
      checkPath(args.filePath ?? args.file_path ?? args.path, tool === 'read');
    } else if (tool === 'glob' || tool === 'grep') {
      checkPath(args.path ?? workspace);
      const pattern = tool === 'glob' ? args.pattern : args.include;
      if (pattern !== undefined && (typeof pattern !== 'string' || path.isAbsolute(pattern) || pattern.split(/[\\/]/).includes('..'))) fail('PATH_DENIED');
    } else if (tool === 'apply_patch') {
      const patch = args.patchText ?? args.patch;
      if (typeof patch !== 'string') fail('INVALID_ARGS');
      const paths = [...patch.matchAll(/^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$/gm)].map(match => match[1].replace(/\r$/, ''));
      if (!paths.length) fail('INVALID_ARGS');
      paths.forEach(filename => checkPath(filename));
    } else if (tool === 'skill') {
      if (args.name !== 'autodeskfusion' || !allowedReads.length) fail('SKILL_DENIED');
    } else if (tool === 'bash') {
      if (typeof args.command !== 'string') fail('INVALID_ARGS');
      checkPath(args.workdir ?? args.cwd ?? workspace);
      for (const match of args.command.matchAll(/(?:^|[;&|\n])\s*(?:cd|chdir|Set-Location|Push-Location)\s+(?:-Path\s+|-LiteralPath\s+)?(?:"([^"]+)"|'([^']+)'|([^\s;&|]+))/gi)) {
        checkPath(match[1] ?? match[2] ?? match[3]);
      }
    }
  }

  function verifyModel(input, modelName) {
    transaction((state, emit) => {
      let code;
      if (!input.sessionID || typeof input.sessionID !== 'string') code = 'INVALID_SESSION';
      else if (state.sessionID && state.sessionID !== input.sessionID) code = 'SESSION_MISMATCH';
      else if (modelName !== contract.model) code = 'MODEL_MISMATCH';
      if (code) { emit('model.denied', { reason: code, sessionID: input.sessionID ?? null }); fail(code); }
      emit('model.verified', { sessionID: input.sessionID, model: contract.model });
    });
  }

  return {
    'chat.message': async input => verifyModel(input, input.model ? `${input.model.providerID}/${input.model.modelID}` : null),
    'chat.params': async input => verifyModel(input, input.model ? `${input.model.providerID}/${input.model.id}` : null),
    'tool.execute.before': async (input, output) => transaction((state, emit) => {
      if (!input || typeof input.callID !== 'string' || !input.callID || typeof input.tool !== 'string' || !input.tool) fail('INVALID_CALL');
      if (typeof input.sessionID !== 'string' || !input.sessionID) fail('INVALID_SESSION');
      if (Object.hasOwn(state.calls, input.callID)) {
        emit('tool.duplicate', { callID: input.callID, sessionID: input.sessionID, tool: input.tool });
        fail('DUPLICATE_CALL');
      }
      const isFusion = FUSION_TOOLS.has(input.tool);
      const pending = state.pendingFusion;
      emit('tool.request', { callID: input.callID, sessionID: input.sessionID, tool: input.tool,
        args_sha256: digest(output?.args ?? null), fusion: isFusion });
      try {
        if (state.sessionID && state.sessionID !== input.sessionID) fail('SESSION_MISMATCH');
        if (!state.model_verified) fail('MODEL_NOT_VERIFIED');
        if (now() >= state.deadline) fail('TIME_BUDGET');
        if (state.total_tool_calls > contract.max_tool_calls) fail('TOOL_BUDGET');
        if (!isFusion && !LOCAL_TOOLS.has(input.tool)) fail('TOOL_DENIED');
        if (isFusion && pending > 0) fail('FUSION_PENDING');
        checkArgs(input.tool, output?.args);
      } catch (error) {
        emit('tool.denied', { callID: input.callID, reason: error.code ?? 'INVALID_ARGS' });
        throw error;
      }
      emit('tool.allowed', { callID: input.callID });
    }),
    'tool.execute.after': async (input, output) => transaction((state, emit) => {
      const call = state.calls[input.callID];
      if (!call || !call.permitted || call.tool !== input.tool || call.sessionID !== input.sessionID) fail('UNMATCHED_TERMINAL');
      if (call.terminal_seen) fail('DUPLICATE_TERMINAL');
      const isError = output?.isError === true || output?.metadata?.error === true;
      const recognized = typeof output?.output === 'string' || Array.isArray(output?.content);
      const text = typeof output?.output === 'string' ? output.output : (Array.isArray(output?.content) ? output.content : []).filter(part => part?.type === 'text').map(part => part.text ?? '').join('\n');
      const uncertain = !recognized || isError && /timed?\s*out|timeout|cancelled|canceled|unknown execution/i.test(text);
      emit('tool.result', { callID: input.callID, isError, status: uncertain ? 'unknown' : isError ? 'error' : 'completed', result_sha256: digest(output ?? null) });
    }),
    dispose: async () => transaction((state, emit) => emit('guard.disposed', { pendingFusion: state.pendingFusion })),
  };
}

async function FusionBenchmarkGuard() {
  const source = process.env.FUSION_BENCHMARK_CONTRACT;
  if (!source) fail('CONTRACT_REQUIRED');
  const inline = source.trimStart().startsWith('{');
  if (!inline && !path.isAbsolute(source)) fail('CONTRACT_PATH_REQUIRED');
  let contract;
  try { contract = JSON.parse(inline ? source : fs.readFileSync(source, 'utf8').replace(/^\uFEFF/, '')); } catch { fail('INVALID_CONTRACT_JSON'); }
  return createGuard(contract, { contractPath: inline ? undefined : source });
}

// Only default is exported: OpenCode invokes every exported function as a plugin.
// The property is also useful for offline integration tests with a controlled clock.
Object.defineProperty(FusionBenchmarkGuard, 'createGuard', { value: createGuard });
export default FusionBenchmarkGuard;
