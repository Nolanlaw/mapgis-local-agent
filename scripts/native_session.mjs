/** Live Sky observations and binding guards for the installed MapGIS 6.7 UI.
 * Pass the documented @oai/sky object from node_repl. No second UI driver,
 * memory injection, background listener, or global MCP registration.
 */
import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';

const execute = promisify(execFile);
const hash = value => createHash('sha256').update(value).digest('hex');
const key = value => path.win32.normalize(value).toLowerCase();

export function parseNativeState(tree) {
  if (typeof tree !== 'string' || !tree.includes('MAPGIS 编辑子系统')) throw new Error('NO_NATIVE_OBSERVATION');
  const projectName = tree.match(/^\s*0 窗口 (?:\(disabled\) )?MAPGIS 编辑子系统(?: - (.*?))? Secondary Actions: Raise/m)?.[1] ?? null;
  const dialogs = [...new Set([...tree.matchAll(/^\s*\d+ 对话框 (.*?) Secondary Actions: Raise/gm)].map(x => x[1]))];
  const layers = [];
  for (const match of tree.matchAll(/^(\t+)\d+ 列表项目 \([^\n]*\) ([^\n]+\.(?:WP|WL|WT))\r?\n([\s\S]*?)(?=^\1\d+ |^\t{0,3}\d+ |(?![\s\S]))/gim)) {
    const body = match[3];
    const directory = body.match(/文本 (?:\(disabled\) )?([A-Za-z]:\\[^\n]*)/);
    const status = body.match(/文本 (?:\(disabled\) )?(当前编辑|编辑|打开|关闭)\s*$/m);
    if (directory && status) layers.push({name: match[2], file: path.win32.join(directory[1].trim(), match[2]), status: status[1]});
  }
  // Titles alone cannot disambiguate projects. This field is observed in the
  // native 工程项目属性 dialog, not inferred from a caller-supplied basename.
  const projectPath = dialogs.includes('工程项目属性')
    ? tree.match(/编辑[^\n]* Value: ([A-Za-z]:\\[^\n]+\.MPJ) ID: 3012/i)?.[1] ?? null : null;
  const selected = dialogs.map(x => x.match(/^第(\d+)号区参数,共有(\d+)条弧段$/)).filter(Boolean);
  let selection = null;
  if (selected.length === 1) {
    const editable = layers.filter(x => ['编辑', '当前编辑'].includes(x.status) && /\.wp$/i.test(x.file));
    if (editable.length !== 1) throw new Error('AMBIGUOUS_SELECTION_LAYER: require exactly one editable area layer');
    const style = {};
    const names = ['fill_color', 'fill_pattern', 'pattern_height', 'pattern_width', 'pattern_color', 'layer'];
    for (let i = 0; i < names.length; i++) {
      const values = [...tree.matchAll(new RegExp(`编辑[^\\n]* Value: ([^\\n ]+) ID: ${1056 + i}`, 'g'))].map(x => Number(x[1]));
      if (!values.length || values.some(x => !Number.isFinite(x) || x !== values[0])) throw new Error('INCOMPLETE_NATIVE_PARAMETERS');
      style[names[i]] = values[0];
    }
    selection = {scope: 'active_native_parameter_dialog', record_id: Number(selected[0][1]),
      arcs: Number(selected[0][2]), type: 'POLYGON', file: editable[0].file, style};
  }
  return {project_name: projectName, project_path_observed: projectPath, layers, dialogs, selection,
    selection_set_scope: selection ? 'one native parameter-edit target' : 'unknown; not an empty selection claim'};
}

export function bindLiveObservation({tree, live, identity, windowId}) {
  const title = tree?.match(/^\s*0 窗口 (?:\(disabled\) )?MAPGIS 编辑子系统(?: - (.*?))? Secondary Actions: Raise/m)?.[1];
  if (!title || live.pid !== identity.pid || live.create_time !== identity.create_time ||
      key(live.executable) !== key(identity.exe)) throw new Error('LIVE_IDENTITY_MISMATCH');
  let candidates = live.project_views.filter(view => view.project_name.toLowerCase() === title.toLowerCase());
  const propertyPath = tree.includes('对话框 工程项目属性 ')
    ? tree.match(/编辑[^\n]* Value: ([A-Za-z]:\\[^\n]+\.MPJ) ID: 3012/i)?.[1] : null;
  if (propertyPath) candidates = candidates.filter(view => view.project_path && key(view.project_path) === key(propertyPath));
  if (candidates.length !== 1 || !candidates[0].project_path) throw new Error('AMBIGUOUS_ACTIVE_PROJECT');
  const view = candidates[0];
  if (view.selection.some(row => row.layer_binding_status !== 'unique')) throw new Error('AMBIGUOUS_SELECTION_LAYER');
  const bound = {process: identity, window_id: windowId, project: {path: view.project_path},
    project_name: view.project_name, project_modified: view.project_modified,
    workspace_modified: view.workspace_modified, layers: view.layers,
    selection_count: view.selection_count, selection: view.selection,
    native_token: live.token, proof: 'exact-build native data + independent Sky active window title'};
  return {...bound, observed_at: live.observed_at, token: hash(JSON.stringify(bound))};
}

export class NativeSession {
  constructor({sky, window, root, python, pid}) {
    this.sky = sky;
    this.window = window; // Must be an actual object from sky.list_windows().
    this.root = path.resolve(root);
    this.python = python;
    this.pid = pid;
    this.project = null;
    this.process = null;
  }

  async support(...args) {
    let stdout;
    try {
      ({stdout} = await execute(this.python, ['-s', '-B', '-X', 'utf8', path.join(this.root, 'scripts/native_support.py'), ...args.map(String)],
        {encoding: 'utf8', windowsHide: true, timeout: 30000, maxBuffer: 1024 * 1024}));
    } catch (error) {
      let reason;
      try { reason = JSON.parse(error.stdout).error; } catch {}
      throw new Error(reason ?? 'NATIVE_SUPPORT_FAILED: inspect the Python support error', {cause: error});
    }
    const result = JSON.parse(stdout);
    if (result.error) throw new Error(result.error);
    return result;
  }

  async capture() {
    const info = await this.support('identity', this.root, this.pid);
    const identity = {pid: info.pid, exe: info.exe, create_time: info.create_time, executable_sha256: info.executable_sha256};
    if (this.process && JSON.stringify(identity) !== JSON.stringify(this.process)) throw new Error('STALE_PROCESS: bind a new native session');
    const windows = (await this.sky.list_windows()).filter(x => x.id === this.window.id && x.app === this.window.app);
    if (windows.length !== 1 || key(windows[0].app.replace(/^process:/, '')) !== key(identity.exe)) throw new Error('WINDOW_IDENTITY_MISMATCH');
    this.window = windows[0];
    const raw = await this.sky.get_window_state({window: this.window, include_text: true, include_screenshot: false});
    const state = parseNativeState(raw.accessibility?.tree);
    this.process = identity;
    if (this.project && (state.project_name?.toLowerCase() !== path.basename(this.project.path).toLowerCase() ||
        (state.project_path_observed && key(await fs.realpath(state.project_path_observed)) !== key(this.project.path)))) throw new Error('STALE_PROJECT: bind the current full native project path again');
    const files = [...new Set(state.layers.map(x => key(x.file)))];
    const fingerprints = {};
    for (const file of files) fingerprints[file] = hash(await fs.readFile(file));
    const bound = {...state, process: identity, window_id: this.window.id,
      project: this.project, disk_sha256: fingerprints};
    return {...bound, observed_at: new Date().toISOString(), token: hash(JSON.stringify(bound))};
  }

  async captureLive() {
    const info = await this.support('identity', this.root, this.pid);
    const identity = {pid: info.pid, exe: info.exe, create_time: info.create_time, executable_sha256: info.executable_sha256};
    if (this.process && JSON.stringify(identity) !== JSON.stringify(this.process)) throw new Error('STALE_PROCESS');
    const windows = (await this.sky.list_windows()).filter(x => x.id === this.window.id && x.app === this.window.app);
    if (windows.length !== 1 || key(windows[0].app.replace(/^process:/, '')) !== key(identity.exe)) throw new Error('WINDOW_IDENTITY_MISMATCH');
    this.window = windows[0];
    const live = await this.support('live', this.root, this.pid);
    const raw = await this.sky.get_window_state({window: this.window, include_text: true, include_screenshot: false});
    const state = bindLiveObservation({tree: raw.accessibility?.tree, live, identity, windowId: this.window.id});
    const after = await this.support('live', this.root, this.pid);
    if (live.token !== after.token) throw new Error('STALE_LIVE_STATE: native state changed across the Sky observation');
    if (this.project && key(this.project.path) !== key(await fs.realpath(state.project.path))) throw new Error('STALE_PROJECT');
    this.process = identity;
    return state;
  }

  async bindProject() {
    const state = await this.capture();
    if (!state.project_path_observed) throw new Error('OPEN_NATIVE_PROJECT_PROPERTIES: full path must be read from 工程项目属性 first');
    const actual = await fs.realpath(state.project_path_observed);
    if (path.basename(actual).toLowerCase() !== state.project_name?.toLowerCase()) throw new Error('PROJECT_TITLE_PATH_MISMATCH');
    this.project = {path: actual, sha256_at_bind: hash(await fs.readFile(actual)), proof: 'native 工程项目属性 ID3012'};
    await this.receipt('binding', {project: this.project, process: this.process, window_id: this.window.id});
    return this.capture();
  }

  async bindLiveProject() {
    const state = await this.captureLive();
    const actual = await fs.realpath(state.project.path);
    this.project = {path: actual, sha256_at_bind: hash(await fs.readFile(actual)),
      proof: 'exact native project field + independent Sky active project'};
    await this.receipt('binding', {project: this.project, process: this.process, window_id: this.window.id});
    return this.captureLive();
  }

  async receipt(id, value, group = 'requests') {
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/.test(id)) throw new Error('Invalid request id');
    if (!['requests', 'file-requests', 'file-reopens'].includes(group)) throw new Error('Invalid receipt group');
    const folder = path.join(this.root, 'evidence/native-session', group);
    await fs.mkdir(folder, {recursive: true});
    const target = path.join(folder, id + '.json');
    await fs.writeFile(target + '.tmp', JSON.stringify(value, null, 2));
    await fs.rename(target + '.tmp', target);
    return value;
  }

  async verifyFileRequest(requestId, {file = null, reopened = false} = {}) {
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/.test(requestId)) throw new Error('Invalid request id');
    if (!this.project) throw new Error('BIND_LIVE_PROJECT_FIRST');
    let saved;
    if (reopened) {
      try {
        saved = JSON.parse(await fs.readFile(path.join(this.root, 'evidence/native-session/file-requests', requestId + '.json'), 'utf8'));
      } catch (error) {
        if (error.code === 'ENOENT') throw new Error('SAVED_FILE_VERIFICATION_REQUIRED');
        throw error;
      }
    }
    const native = await this.captureLive();
    const request = await this.support('request', this.root, requestId);
    if (!['APPLIED', 'OUTPUT_CHANGED'].includes(request.state)) throw new Error('APPLIED_FILE_REQUEST_REQUIRED');
    const actual = await fs.realpath(file || request.output);
    const relative = path.relative(this.root, actual);
    if (relative.startsWith('..') || path.isAbsolute(relative) || !/^(?:work|deliverables)[\\/]/.test(relative)) {
      throw new Error('FILE_REQUEST_REQUIRES_OWNED_BRANCH');
    }
    const layers = [];
    for (const layer of native.layers) {
      if (key(await fs.realpath(layer.path)) === key(actual)) layers.push(layer);
    }
    if (layers.length !== 1) throw new Error('REQUEST_LAYER_NOT_BOUND');
    if (native.project_modified !== false || layers[0].workspace?.modified !== false) throw new Error('UNSAVED_NATIVE_STATE');
    const verification = await this.support('verify-request', this.root, requestId, actual);
    if (!verification.matches_candidate_records) throw new Error('SAVED_CANDIDATE_MISMATCH');
    const entity = {POINT:'POINT', LINE:'LINE', POLYGON:'REGION'}[verification.comparison.type];
    if (layers[0].workspace?.counts?.[entity]?.active !== verification.comparison.after.count) throw new Error('NATIVE_COUNT_MISMATCH');
    if (reopened) {
      if (native.process.pid === saved.native.process.pid && native.process.create_time === saved.native.process.create_time) {
        throw new Error('FRESH_PROCESS_REQUIRED');
      }
      if (key(actual) !== key(saved.file)) throw new Error('REOPENED_FILE_MISMATCH');
      if ((await fs.stat(actual)).mtimeMs / 1000 >= native.process.create_time) throw new Error('FILE_NEWER_THAN_PROCESS');
    }
    const after = await this.captureLive();
    if (after.token !== native.token) throw new Error('STALE_NATIVE_STATE');
    if (hash(await fs.readFile(actual)) !== verification.comparison.after.sha256) throw new Error('SAVED_FILE_CHANGED');
    return this.receipt(requestId, {state:reopened ? 'reopened_verified' : 'file_request_verified',
      request_id:requestId, kind:request.kind, file:actual, native, file_verification:verification,
      scope:'bound project and loaded clean layer count plus saved candidate records; no UI action is sent'},
    reopened ? 'file-reopens' : 'file-requests');
  }

  async prepareStyle(requestId, fillColor) {
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/.test(requestId)) throw new Error('Invalid request id');
    if (!this.project) throw new Error('Bind full native project path first');
    // The installed 6.7 color picker and native TIFF calibration expose 1024
    // entries. A color number is not an eight-bit RGB channel.
    if (!Number.isInteger(fillColor) || fillColor < 0 || fillColor > 1023) throw new Error('fill_color must be 0..1023');
    const state = await this.capture();
    if (!state.selection) throw new Error('NO_NATIVE_SELECTION: open the selected area parameter dialog');
    const actual = await fs.realpath(state.selection.file);
    const relative = path.relative(this.root, actual);
    if (relative.startsWith('..') || path.isAbsolute(relative) || !/^(?:deliverables|work)[\\/]/.test(relative)) throw new Error('Native edits require a project-owned work copy');
    const disk = await this.support('record', actual, state.selection.record_id);
    const value = {state: 'prepared', request_id: requestId, before: state, disk,
      requested: {fill_color: fillColor}, native_commit: 'not started'};
    const file = path.join(this.root, 'evidence/native-session/requests', requestId + '.json');
    try {
      const old = JSON.parse(await fs.readFile(file, 'utf8'));
      if (old.before.token !== state.token || old.requested.fill_color !== fillColor) throw new Error('REQUEST_CONFLICT');
      return old;
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    return this.receipt(requestId, value);
  }

  async guard(requestId, {fieldAlreadySet = false} = {}) {
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/.test(requestId)) throw new Error('Invalid request id');
    const request = JSON.parse(await fs.readFile(path.join(this.root, 'evidence/native-session/requests', requestId + '.json'), 'utf8'));
    if (request.state !== 'prepared') throw new Error('RECONCILE_REQUEST: do not repeat an uncertain native commit');
    const current = await this.capture();
    const expected = structuredClone(request.before);
    if (fieldAlreadySet) expected.selection.style.fill_color = request.requested.fill_color;
    const normalized = x => ({process: x.process, window_id: x.window_id, project: x.project,
      project_name: x.project_name, layers: x.layers, disk_sha256: x.disk_sha256, selection: x.selection});
    if (JSON.stringify(normalized(current)) !== JSON.stringify(normalized(expected))) throw new Error('STALE_SELECTION_OR_FILE: prepare again from the current native dialog');
    return request;
  }

  async beginCommit(requestId) {
    const request = await this.guard(requestId, {fieldAlreadySet: true});
    // Persist before clicking native OK. After unknown input, never blindly retry.
    return this.receipt(requestId, {...request, state: 'commit_requested', native_commit: 'reconcile with native readback before retry'});
  }

  async verifySaved(requestId, baseline) {
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/.test(requestId)) throw new Error('Invalid request id');
    const request = JSON.parse(await fs.readFile(path.join(this.root, 'evidence/native-session/requests', requestId + '.json'), 'utf8'));
    if (!['commit_requested', 'saved_verified', 'reopened_verified'].includes(request.state)) throw new Error('Native commit was not recorded');
    if (hash(await fs.readFile(baseline)) !== request.disk.sha256) throw new Error('BASELINE_MISMATCH');
    const current = await this.capture();
    if (key(current.project.path) !== key(request.before.project.path)) throw new Error('STALE_PROJECT');
    const result = await this.support('verify', baseline, request.disk.path, JSON.stringify([
      {record_id: request.before.selection.record_id, field: 'fill_color', after: request.requested.fill_color}]));
    if (!result.verified) throw new Error('NATIVE_SAVE_MISMATCH: inspect geometry, attributes and unexpected style changes');
    return this.receipt(requestId, {...request, state: 'saved_verified', baseline: await fs.realpath(baseline), verification: result});
  }

  async verifyReopened(requestId, baseline) {
    const request = await this.verifySaved(requestId, baseline);
    const current = await this.capture();
    const selected = current.selection;
    if (!selected || selected.record_id !== request.before.selection.record_id ||
        key(selected.file) !== key(request.disk.path) || selected.style.fill_color !== request.requested.fill_color) {
      throw new Error('NATIVE_REOPEN_READBACK_MISMATCH: query the saved physical record in the reopened native project');
    }
    if (current.process.pid === request.before.process.pid &&
        current.process.create_time === request.before.process.create_time) {
      throw new Error('FRESH_PROCESS_REQUIRED: use a newly launched editor for this acceptance gate');
    }
    return this.receipt(requestId, {...request, state: 'reopened_verified', native_reopen: current});
  }
}
