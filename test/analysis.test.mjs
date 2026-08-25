import assert from 'node:assert/strict';
import test from 'node:test';
import { buildAnalysisPrompt } from '../.test-dist/analysis.js';

test('handoff includes exact record identity and current-run permission context', () => {
  const message = buildAnalysisPrompt({
    caseNumber: 'CASE-2025-001',
    recordId: 'rec_current',
    tableId: 'tbl_current',
    uploaderOpenId: 'ou_uploader',
  });
  assert.match(message, /"table_id":"tbl_current"/);
  assert.match(message, /"record_id":"rec_current"/);
  assert.match(message, /"case_number":"CASE-2025-001"/);
  assert.match(message, /上传人 open_id：ou_uploader/);
  assert.match(message, /full_access/);
  assert.match(message, /BASE_CONNECTOR_UNAVAILABLE/);
});

test('record identity is mandatory and case number is not used as a lookup substitute', () => {
  assert.throws(
    () => buildAnalysisPrompt({ caseNumber: 'CASE-001', recordId: '', tableId: 'tbl_current' }),
    /目标记录 ID/,
  );
  assert.throws(
    () => buildAnalysisPrompt({ caseNumber: 'CASE-001', recordId: 'rec_current', tableId: '' }),
    /目标数据表 ID/,
  );
});

test('handoff message is bounded and does not contain attachment bytes', () => {
  const message = buildAnalysisPrompt({
    caseNumber: 'CASE-001',
    recordId: 'rec_current',
    tableId: 'tbl_current',
  });
  assert.ok(message.length < 800);
  assert.doesNotMatch(message, /file_token|base64|multipart|attachment_ids/);
});
