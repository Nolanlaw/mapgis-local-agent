import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {NativeControl} from '../scripts/native_control.mjs';

async function fixture(t) {
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'mapgis-control-'));
  t.after(()=>fs.rm(root,{recursive:true,force:true}));
  const folder=path.join(root,'work','ui');await fs.mkdir(folder,{recursive:true});
  const window={id:9,app:'process:D:\\MapGIS\\Face67.exe',title:'MAPGIS6.7 主菜单'};
  const process={pid:77,create_time:123.456,exe:'D:\\MapGIS\\Face67.exe'};
  const proof={module:'utilities',process,executable:process.exe,executable_sha256:'a'.repeat(64),window};
  const calls=[];
  const sky={list_windows:async()=>[window],get_window:async()=>window,
    get_window_state:async()=>({window,accessibility:{tree:'0 对话框 MAPGIS6.7\n 2 按钮 图形处理\n 3 编辑 (settable, string) Value: a\n 4 按钮 (disabled) 禁用',focused_element:'3 编辑 Value: a'},screenshots:[]}),
    click:async a=>calls.push(['click',a]),set_value:async a=>calls.push(['set_value',a]),press_key:async a=>calls.push(['press_key',a])};
  const session=new NativeControl({sky,root,python:'unused',module:'utilities',folder,proof});
  session.processInfo=async()=>({...proof,process,processes:[process]});
  return {session,sky,calls,window,root,folder,process};
}

test('an actual returned window and one observation drive one action; stale token is consumed',async t=>{
  const {session,calls,window}=await fixture(t);const state=await session.observe();
  const r=await session.act({token:state.token,request_id:'one',action:'click',options:{element_index:2}});
  assert.equal(r.state,'APPLIED');assert.equal(calls.length,1);assert.equal(calls[0][1].window,window);
  await assert.rejects(session.act({token:state.token,request_id:'two',action:'click',options:{element_index:2}}),/STALE_UI_OBSERVATION/);
  assert.equal(calls.length,1);
});

test('a completed action can be reconciled/replayed without sending the click again',async t=>{
  const {session,calls}=await fixture(t);let state=await session.observe();
  await session.act({token:state.token,request_id:'one',action:'click',options:{element_index:2}});
  state=await session.observe();const r=await session.act({token:state.token,request_id:'one',action:'click',options:{element_index:2}});
  assert.equal(r.replayed,true);assert.equal(calls.length,1);
  const reconciled=await session.reconcile('one');assert.equal(reconciled.automatic_repeat,false);
});

test('input failure stays uncertain and the same request is never blindly resent',async t=>{
  const {session,sky,calls}=await fixture(t);
  sky.click=async a=>{calls.push(['click',a]);throw Error('lost reply');};
  let state=await session.observe();
  await assert.rejects(session.act({token:state.token,request_id:'lost',action:'click',options:{element_index:2}}),/UI_OUTCOME_UNKNOWN/);
  state=await session.observe();
  await assert.rejects(session.act({token:state.token,request_id:'lost',action:'click',options:{element_index:2}}),/UI_OUTCOME_UNKNOWN/);
  assert.equal(calls.length,1);assert.equal((await session.reconcile('lost')).receipt.state,'OUTCOME_UNKNOWN');
});

test('PID reuse and another window owner stop before input',async t=>{
  const {session,sky,calls,process}=await fixture(t);await session.observe();
  session.processInfo=async()=>({...session.proof,process:{...process,create_time:124}});
  await assert.rejects(session.identity(),/STALE_NATIVE_PROCESS/);assert.equal(calls.length,0);
  session.processInfo=async()=>({...session.proof,process});sky.list_windows=async()=>[{id:9,app:'other-app'}];
  await assert.rejects(session.observe(),/NATIVE_WINDOW_LOST/);
});

test('unobserved/disabled elements and arbitrary input overrides are rejected',async t=>{
  const {session,calls}=await fixture(t);const state=await session.observe();
  for(const options of [{element_index:90},{element_index:4},{element_index:2,window:{id:10}}])
    await assert.rejects(session.act({token:state.token,request_id:'bad',action:'click',options}),/OBSERVED_ENABLED_ELEMENT_REQUIRED|NATIVE_UI_ACTION_PARAMETERS/);
  assert.equal(calls.length,0);
});

test('file dialog paths stay in owned work/deliverables including resolved parents',async t=>{
  const {session,root,calls}=await fixture(t);let state=await session.observe();
  await assert.rejects(session.act({token:state.token,request_id:'bad',action:'set_value',options:{element_index:3,value:path.join(root,'original.wt')},purpose:'path'}),/TASK_COPY_PATH_REQUIRED/);
  const r=await session.act({token:state.token,request_id:'good',action:'set_value',options:{element_index:3,value:path.join(root,'work','new.wt')},purpose:'path'});
  assert.equal(r.state,'APPLIED');assert.equal(calls.length,1);
});

test('coordinates need the actual screenshot and Windows-key chords are refused',async t=>{
  const {session,calls}=await fixture(t);const state=await session.observe();
  await assert.rejects(session.act({token:state.token,request_id:'pixel',action:'click',options:{x:10,y:10}}),/OBSERVED_SCREENSHOT_REQUIRED/);
  await assert.rejects(session.act({token:state.token,request_id:'win',action:'press_key',options:{key:'Win+r'}}),/NATIVE_KEY_NOT_ALLOWED/);
  assert.equal(calls.length,0);
});
