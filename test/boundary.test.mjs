import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const source = await readFile(new URL('../src/index.ts', import.meta.url), 'utf8');

test('component dispatch boundary is record-scoped and attachment-byte free', () => {
  assert.match(source, /exactly one idempotent handoff/i);
  assert.match(source, /does not read or upload attachment bytes/i);
  assert.doesNotMatch(source, /itemId: "caseNumber"/);
  assert.match(source, /itemId: "recordId"[\s\S]*required: true/);
  assert.match(source, /itemId: "targetTableId"[\s\S]*required: true/);
  assert.match(source, /\/bitable\/v1\//);
  assert.match(source, /FIELD_MATERIALS = "上传材料"/);
  assert.match(source, /FIELD_UPLOADER = "上传人"/);
  assert.match(source, /FIELD_STATUS = "状态"/);
  assert.match(source, /full_access/);
  assert.match(source, /recordId/);
  assert.match(source, /stage=accepted/);
  assert.doesNotMatch(source, /Component\.Attachment/);
  assert.doesNotMatch(source, /agent_attachment_ids/);
  assert.doesNotMatch(source, /Buffer|multipartForm|downloadAttachment|uploadAgentAttachment/);
});

test('component does not use stale production field names', () => {
  assert.doesNotMatch(source, /案件文档|AI处理状态|待法务审核/);
  assert.doesNotMatch(source, /aixuexi\.feishu\.cn\/docx/);
});
