import test from 'node:test';
import assert from 'node:assert/strict';
import {NativeSession, parseNativeState, bindLiveObservation} from '../scripts/native_session.mjs';

const frame = 'Window: "MAPGIS 编辑子系统 - 实测.MPJ", App: mdiedit6x.exe.\n\t0 窗口 MAPGIS 编辑子系统 - 实测.MPJ Secondary Actions: Raise\n';
const layer = (directory, status = '编辑', index = 20) => `\t\t\t\t${index} 列表项目 (selectable) 地层.WP\n\t\t\t\t\t${index+1} 编辑 地层.WP\n\t\t\t\t\t${index+2} 文本 ${status}\n\t\t\t\t\t${index+3} 文本 区文件\n\t\t\t\t\t${index+4} 文本 ${directory}\\\n`;
const params = '\t\t90 对话框 第26号区参数,共有27条弧段 Secondary Actions: Raise\n' + [120, 0, 0, 0, 0, 0].map((v,i)=>`\t\t\t${100+i} 编辑 (settable, string) Value: ${v} ID: ${1056+i}\n`).join('');

test('full layer paths distinguish same names and bind the only editable layer', () => {
  const state = parseNativeState(frame + layer('E:\\原始', '打开') + layer('E:\\工作', '编辑', 40) + params);
  assert.equal(state.layers.length, 2);
  assert.equal(state.selection.file, 'E:\\工作\\地层.WP');
  assert.equal(state.selection.record_id, 26);
  assert.equal(state.selection.style.fill_color, 120);
});
test('multiple editable area layers are ambiguous, never choose the first', () => {
  assert.throws(()=>parseNativeState(frame + layer('E:\\甲') + layer('E:\\乙', '编辑', 40) + params), /AMBIGUOUS_SELECTION_LAYER/);
});
test('native current-edit state also owns the parameter target', () => {
  const state = parseNativeState(frame + layer('E:\\工作', '当前编辑') + params);
  assert.equal(state.selection.file, 'E:\\工作\\地层.WP');
});
test('no parameter dialog means unknown selection, not an empty set', () => {
  const state = parseNativeState(frame + layer('E:\\工作'));
  assert.equal(state.selection, null);
  assert.match(state.selection_set_scope, /unknown/);
});
test('native project property supplies full path despite misleading legacy field label', () => {
  const state = parseNativeState(frame + '\t\t80 对话框 工程项目属性 Secondary Actions: Raise\n\t\t\t81 编辑 最大显示比例 Value: E:\\工程\\实测.MPJ ID: 3012\n');
  assert.equal(state.project_path_observed, 'E:\\工程\\实测.MPJ');
});
test('a title alone is not a full project path', () => {
  assert.equal(parseNativeState(frame).project_path_observed, null);
});
test('incomplete native parameters cannot become a selection token', () => {
  assert.throws(()=>parseNativeState(frame + layer('E:\\工作') + params.replace(' ID: 1061', ' ID: 9999')), /INCOMPLETE_NATIVE_PARAMETERS/);
});

const identity = {pid: 123, create_time: 12, exe: 'D:\\MapGIS\\mdiedit6x.exe'};
const live = {pid: 123, create_time: 12, executable: identity.exe, token: 'a',
  project_views: [{project_name: '实测.MPJ', project_path: 'E:\\甲\\实测.MPJ',
    selection_count: 0, selection: [], layers: [], project_modified: false, workspace_modified: false}]};
test('live empty pool is bound to an independently observed active MDI project', () => {
  const state = bindLiveObservation({tree: frame, live, identity, windowId: 9});
  assert.equal(state.selection_count, 0);
  assert.deepEqual(state.selection, []);
  assert.equal(state.project.path, 'E:\\甲\\实测.MPJ');
});
test('an inactive view cannot impersonate the current non-project document', () => {
  assert.throws(() => bindLiveObservation({tree: frame.replaceAll('实测.MPJ', '新文件1'), live, identity, windowId: 9}), /AMBIGUOUS_ACTIVE_PROJECT/);
});
test('duplicate project basenames require independent full path observation', () => {
  const duplicate = {...live, project_views: [...live.project_views, {...live.project_views[0], project_path: 'E:\\乙\\实测.MPJ'}]};
  assert.throws(() => bindLiveObservation({tree: frame, live: duplicate, identity, windowId: 9}), /AMBIGUOUS_ACTIVE_PROJECT/);
  const tree = frame + '\t\t80 对话框 工程项目属性 Secondary Actions: Raise\n\t\t\t81 编辑 最大显示比例 Value: E:\\乙\\实测.MPJ ID: 3012\n';
  assert.equal(bindLiveObservation({tree, live: duplicate, identity, windowId: 9}).project.path, 'E:\\乙\\实测.MPJ');
});
test('a recycled process identity is rejected even when the title matches', () => {
  assert.throws(() => bindLiveObservation({tree: frame, live: {...live, create_time: 99}, identity, windowId: 9}), /LIVE_IDENTITY_MISMATCH/);
});

// A second document window is not a new editor process. Exercise the acceptance
// boundary independently of Sky input and the binary reader tests.
const reopenSession = ({pid = 200, createTime = 2, recordId = 26, color = 3} = {}) => {
  const session = new NativeSession({sky: {}, window: {}, root: '.', python: '', pid});
  session.verifySaved = async () => ({before: {process: {pid: 100, create_time: 1},
    selection: {record_id: 26}}, disk: {path: 'E:\\工作\\地层.WP'}, requested: {fill_color: 3}});
  session.capture = async () => ({process: {pid, create_time: createTime}, window_id: 999,
    selection: {record_id: recordId, file: 'E:\\工作\\地层.WP', style: {fill_color: color}}});
  session.receipt = async (_id, value) => value;
  return session;
};

test('a different window in the same process cannot pass fresh reopen', async () => {
  await assert.rejects(reopenSession({pid: 100, createTime: 1}).verifyReopened('test', 'baseline'), /FRESH_PROCESS_REQUIRED/);
});

test('fresh reopen rejects the wrong native record and unsaved color', async () => {
  await assert.rejects(reopenSession({recordId: 25}).verifyReopened('test', 'baseline'), /READBACK_MISMATCH/);
  await assert.rejects(reopenSession({color: 2}).verifyReopened('test', 'baseline'), /READBACK_MISMATCH/);
});

test('fresh process and matching native record pass reopen', async () => {
  assert.equal((await reopenSession().verifyReopened('test', 'baseline')).state, 'reopened_verified');
});
