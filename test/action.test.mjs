import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import test from 'node:test';

const require = createRequire(import.meta.url);
const basekit = require('../output/private/build/index.js').default;
const action = basekit.action;

test('widget form exposes only the exact record and table identifiers', () => {
  assert.deepEqual(
    action.formItems.map(({ itemId, required }) => ({ itemId, required })),
    [
      { itemId: 'recordId', required: true },
      { itemId: 'targetTableId', required: true },
    ],
  );
});

function recordResponse(fields) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => ({ code: 0, data: { record: { record_id: 'rec_current', fields } } }),
  };
}

function genericResponse(data = {}) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => ({ code: 0, data }),
  };
}

function buildContext({
  fields = {},
  chatFailure = false,
  triggerRecordId = '',
  triggerTableId = 'tbl_current',
  tenantAccessToken = 'tenant-token',
} = {}) {
  const state = {
    fields: {
      '案件编号': 'CASE-001',
      '上传材料': [{ file_token: 'file_1', name: '当前材料.txt', size: 10 }],
      '上传人': [{ id: 'ou_uploader', name: '上传人' }],
      '状态': '待分析',
      'AI分析结果': null,
      '执行日志/失败原因': null,
      ...fields,
    },
  };
  const calls = [];
  const context = {
    __testNoTimeout: true,
    tenantAccessToken,
    app: {
      token: 'base-token',
      logID: 'log_1',
      trigger: { tableID: triggerTableId, recordID: triggerRecordId },
    },
    fetch: async (url, options = {}) => {
      const u = String(url);
      calls.push({ url: u, options });
      if (u.includes('/bitable/v1/') && u.includes('/records/rec_current')) {
        if (options.method === 'GET') return recordResponse(state.fields);
        if (options.method === 'PUT') {
          const body = JSON.parse(options.body);
          Object.assign(state.fields, body.fields);
          return genericResponse({});
        }
      }
      if (u.endsWith('/chats') && options.method === 'POST') {
        if (chatFailure) {
          return {
            ok: true,
            status: 200,
            statusText: 'OK',
            json: async () => ({ code: 999, msg: 'agent unavailable' }),
          };
        }
        return genericResponse({ agent_chat_id: 'chat_1' });
      }
      throw new Error(`unexpected request: ${u}`);
    },
  };
  return { calls, context, state };
}

test('dispatch locks one record, calls one chat, and records an accepted handoff', async () => {
  const { calls, context, state } = buildContext();
  const result = await action.execute({ recordId: 'rec_current' }, context);
  assert.equal(result.accepted, true);
  assert.equal(result.dispatchState, 'accepted');
  assert.equal(result.agentChatId, 'chat_1');
  assert.equal(result.recordId, 'rec_current');
  assert.equal(result.buildId, '3.3.3-record-id-only-dispatch');
  assert.equal(calls.filter((call) => call.url.endsWith('/chats') && call.options.method === 'POST').length, 1);
  assert.equal(state.fields['状态'], '分析中');
  assert.match(state.fields['执行日志/失败原因'], /stage=accepted/);
  const submitted = calls.find((call) => call.url.endsWith('/chats') && call.options.method === 'POST');
  const body = JSON.parse(submitted.options.body);
  assert.match(body.user_message.content[0].text, /"record_id":"rec_current"/);
  assert.match(body.user_message.content[0].text, /"case_number":"CASE-001"/);
  assert.equal(submitted.options.headers.authorization, 'Bearer tenant-token');
});

test('trigger record id is a safe fallback when workflow does not map recordId', async () => {
  const { context } = buildContext({ triggerRecordId: 'rec_current' });
  const result = await action.execute({}, context);
  assert.equal(result.recordId, 'rec_current');
});

test('missing record id fails before any Aily chat and never guesses by case number', async () => {
  const { calls, context } = buildContext();
  await assert.rejects(
    action.execute({}, context),
    /缺少目标记录 ID/,
  );
  assert.equal(calls.some((call) => call.url.endsWith('/chats')), false);
});

test('case number is read from the exact record and legacy form input is ignored', async () => {
  const { calls, context } = buildContext({ fields: { '案件编号': 'RECORD-ONLY-001' } });
  await action.execute({ caseNumber: 'WRONG-LEGACY-VALUE', recordId: 'rec_current' }, context);
  const submitted = calls.find((call) => call.url.endsWith('/chats') && call.options.method === 'POST');
  const body = JSON.parse(submitted.options.body);
  assert.match(body.user_message.content[0].text, /"case_number":"RECORD-ONLY-001"/);
  assert.doesNotMatch(body.user_message.content[0].text, /WRONG-LEGACY-VALUE/);
});

test('empty record-derived case number writes a same-record failure and creates no chat', async () => {
  const { calls, context, state } = buildContext({ fields: { '案件编号': '' } });
  await assert.rejects(action.execute({ recordId: 'rec_current' }, context), /案件编号.*为空/);
  assert.equal(calls.some((call) => call.url.endsWith('/chats')), false);
  assert.equal(state.fields['状态'], '分析失败');
  assert.match(state.fields['执行日志/失败原因'], /stage=dispatch/);
});

test('empty materials or uploader are hard failures before chat', async () => {
  for (const fields of [{ '上传材料': [] }, { 上传人: [] }]) {
    const { calls, context, state } = buildContext({ fields });
    await assert.rejects(action.execute({ recordId: 'rec_current' }, context));
    assert.equal(calls.some((call) => call.url.endsWith('/chats')), false);
    assert.equal(state.fields['状态'], '分析失败');
  }
});

test('an in-flight handoff is idempotent and does not create a second chat', async () => {
  const { calls, context } = buildContext({
    fields: {
      状态: '分析中',
      '执行日志/失败原因': 'build=3.3.3-record-id-only-dispatch; chatId=chat_existing',
    },
  });
  const result = await action.execute({ recordId: 'rec_current' }, context);
  assert.equal(result.dispatchState, 'already_in_progress');
  assert.equal(result.agentChatId, 'chat_existing');
  assert.equal(calls.some((call) => call.url.endsWith('/chats')), false);
});

test('Aily failure writes a same-record failure and exposes build/stage diagnostics', async () => {
  const { calls, context, state } = buildContext({ chatFailure: true });
  await assert.rejects(
    action.execute({ recordId: 'rec_current' }, context),
    /3\.3\.3-record-id-only-dispatch.*create-agent-chat.*agent unavailable/,
  );
  assert.equal(calls.some((call) => call.url.endsWith('/chats')), true);
  assert.equal(state.fields['状态'], '分析失败');
  assert.equal(state.fields['AI分析结果'], null);
});

test('missing tenant token fails before network access', async () => {
  const { calls, context } = buildContext({ tenantAccessToken: '' });
  await assert.rejects(action.execute({ recordId: 'rec_current' }, context), /租户访问凭证/);
  assert.equal(calls.length, 0);
});
