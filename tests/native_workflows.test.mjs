import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {createHash} from 'node:crypto';
import {readScanGeologyWorkflow,verifyWorkflowProject} from '../scripts/native_workflows.mjs';

test('scan workflow keeps source, native output, fresh reopen and recovery separate',async()=>{
  const value=await readScanGeologyWorkflow();
  assert.equal(value.id,'scan-geology');
  assert.deepEqual(value.steps.map(s=>s.id),['source','georeference','native-raster','candidates',
    'create','topology','cartography','project','seams','output','reopen','recovery']);
  const seams=value.steps.find(s=>s.id==='seams');
  assert.equal(seams.validation.startsWith('FILE_TESTED'),true);
  assert.ok(seams.tools.includes('mapgis_merge_candidate_tiles'));
  assert.match(seams.validation,/native library UI are not established/);
});

test('workflow rejects same-name dependencies from another directory, drift and unsaved state',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'mapgis-workflow-'));
  try {
    const project=path.join(root,'project.mpj'), file=path.join(root,'layer.wp');
    await fs.writeFile(project,'saved project');
    await fs.writeFile(file,'saved layer');
    const sha256=createHash('sha256').update('saved layer').digest('hex');
    const manifest={project,files:[{path:file,sha256}]};
    let live={project:{path:project},project_modified:false,workspace_modified:null,
      layers:[{path:file}],token:'a'};
    const session={captureLive:async()=>structuredClone(live)};
    assert.equal((await verifyWorkflowProject(session,manifest)).status,'BOUND_SAVED_DEPENDENCIES');
    await fs.mkdir(path.join(root,'other'));
    const other=path.join(root,'other','layer.wp');
    await fs.writeFile(other,'saved layer');
    live.layers=[{path:other}];
    await assert.rejects(verifyWorkflowProject(session,manifest),/DEPENDENCY_MISMATCH/);
    live.layers=[{path:file}];
    await fs.writeFile(file,'later layer');
    await assert.rejects(verifyWorkflowProject(session,manifest),/DEPENDENCY_CHANGED/);
    live.project_modified=true;
    await assert.rejects(verifyWorkflowProject(session,manifest),/UNSAVED/);
    live.project_modified=false;
    await fs.writeFile(file,'saved layer');
    let calls=0;
    await assert.rejects(verifyWorkflowProject({captureLive:async()=>({...live,token:String(calls++)})},manifest),/STALE_WORKFLOW_NATIVE_STATE/);
  } finally {
    await fs.rm(root,{recursive:true,force:true});
  }
});
