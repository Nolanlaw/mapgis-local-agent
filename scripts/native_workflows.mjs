/** Workflow knowledge and saved dependency checks; UI stays with NativeSession/Sky. */
import fs from 'node:fs/promises';
import {createHash} from 'node:crypto';

const digest = data => createHash('sha256').update(data).digest('hex');
const key = value => value.toLowerCase();

export async function readScanGeologyWorkflow() {
  return JSON.parse(await fs.readFile(new URL('../workflows/scan-geology.json', import.meta.url), 'utf8'));
}

export async function verifyWorkflowProject(session, manifest) {
  // Capture the exact build/process/active MDI through the existing adapter.
  const live = await session.captureLive();
  const actualProject = await fs.realpath(live.project.path);
  const expectedProject = await fs.realpath(manifest.project);
  if (key(actualProject) !== key(expectedProject)) throw new Error('WRONG_WORKFLOW_PROJECT');
  if (live.project_modified !== false || live.workspace_modified === true) throw new Error('UNSAVED_WORKFLOW_STATE');
  const project_sha256 = digest(await fs.readFile(actualProject));
  if (manifest.project_sha256 && manifest.project_sha256 !== project_sha256) throw new Error('WORKFLOW_PROJECT_CHANGED');
  const expected = new Map();
  for (const entry of manifest.files) {
    const real = await fs.realpath(entry.path);
    if (expected.has(key(real))) throw new Error('DUPLICATE_WORKFLOW_DEPENDENCY');
    if (digest(await fs.readFile(real)) !== entry.sha256) throw new Error('WORKFLOW_DEPENDENCY_CHANGED');
    expected.set(key(real), entry.sha256);
  }
  const actual = await Promise.all(live.layers.map(layer => fs.realpath(layer.path)));
  if (actual.length !== expected.size || actual.some(file => !expected.has(key(file)))) {
    throw new Error('WORKFLOW_DEPENDENCY_MISMATCH');
  }
  const after = await session.captureLive();
  if (live.token !== after.token) throw new Error('STALE_WORKFLOW_NATIVE_STATE');
  return {status:'BOUND_SAVED_DEPENDENCIES', project:actualProject, project_sha256,
    files:actual, live_token:live.token, observed_at:live.observed_at,
    workspace_modified:live.workspace_modified,
    scope:'saved files and native project identity; no claim that every workspace is loaded or UI operation has executed'};
}
