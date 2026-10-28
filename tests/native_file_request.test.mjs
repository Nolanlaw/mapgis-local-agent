import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {NativeSession} from '../scripts/native_session.mjs';

async function fixture(t) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'mapgis-native-file-'));
  t.after(() => fs.rm(root, {recursive:true}));
  await fs.mkdir(path.join(root, 'deliverables'));
  const file = path.join(root, 'deliverables', 'result.wt');
  await fs.writeFile(file, 'candidate');
  const digest = createHash('sha256').update('candidate').digest('hex');
  const native = {token:'observed', process:{pid:1, create_time:Date.now()/1000-60},
    project:{path:path.join(root, 'deliverables', 'drawing.mpj')},
    project_modified:false, workspace_modified:false,
    layers:[{path:file, workspace:{modified:false, counts:{POINT:{active:2, max_physical_id:4}}}}]};
  await fs.writeFile(native.project.path, 'project');
  const verification = {matches_candidate_records:true, comparison:{type:'POINT', after:{sha256:digest,count:2}}};
  const session = new NativeSession({sky:{}, window:{id:9}, root, python:'python', pid:1});
  session.project = native.project;
  session.captureLive = async () => structuredClone(native);
  session.support = async action => action === 'request'
    ? {id:'branch',state:'OUTPUT_CHANGED',kind:'DELETE',output:file}
    : structuredClone(verification);
  return {root,file,session,native,verification};
}

test('file requests join the saved candidate to loaded clean layer counts and a fresh process', async t => {
  const {root,session,native} = await fixture(t);
  const first = await session.verifyFileRequest('branch');
  assert.equal(first.state, 'file_request_verified');
  assert.equal(first.file_verification.matches_candidate_records, true);
  await assert.rejects(session.verifyFileRequest('branch', {reopened:true}), /FRESH_PROCESS_REQUIRED/);
  native.process = {pid:2,create_time:Date.now()/1000+2};
  const reopened = await session.verifyFileRequest('branch', {reopened:true});
  assert.equal(reopened.state, 'reopened_verified');
  assert.equal(JSON.parse(await fs.readFile(path.join(root, 'evidence/native-session/file-requests/branch.json'))).native.process.pid,1);
  assert.equal(JSON.parse(await fs.readFile(path.join(root, 'evidence/native-session/file-reopens/branch.json'))).native.process.pid,2);
});

test('wrong/unloaded/dirty layers and mismatched records cannot pass the file handoff', async t => {
  const {session,native,verification} = await fixture(t);
  native.layers[0].workspace.modified = true;
  await assert.rejects(session.verifyFileRequest('branch'), /UNSAVED_NATIVE_STATE/);
  native.layers[0].workspace.modified = false;
  native.layers[0].workspace.counts.POINT.active = 4;
  await assert.rejects(session.verifyFileRequest('branch'), /NATIVE_COUNT_MISMATCH/);
  native.layers[0].workspace.counts.POINT.active = 2;
  verification.matches_candidate_records = false;
  await assert.rejects(session.verifyFileRequest('branch'), /SAVED_CANDIDATE_MISMATCH/);
  verification.matches_candidate_records = true;
  native.layers = [];
  await assert.rejects(session.verifyFileRequest('branch'), /REQUEST_LAYER_NOT_BOUND/);
});

test('state or file drift during verification is rejected, and missing prior acceptance is explicit', async t => {
  const {session,native,file} = await fixture(t);
  await assert.rejects(session.verifyFileRequest('branch',{reopened:true}), /SAVED_FILE_VERIFICATION_REQUIRED/);
  let captures = 0;
  session.captureLive = async () => ({...structuredClone(native),token:String(++captures)});
  await assert.rejects(session.verifyFileRequest('branch'), /STALE_NATIVE_STATE/);
  session.captureLive = async () => { if (++captures > 1) await fs.writeFile(file,'drift'); return structuredClone(native); };
  captures = 0;
  await assert.rejects(session.verifyFileRequest('branch'), /SAVED_FILE_CHANGED/);
});
