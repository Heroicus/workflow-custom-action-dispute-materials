import assert from 'node:assert/strict';
import { access, readFile } from 'node:fs/promises';
import test from 'node:test';

const skillPath = new URL('../agent-skill/organize-dispute-materials/SKILL.md', import.meta.url);
const agentPromptPath = new URL('../agent-skill/organize-dispute-materials/agents/openai.yaml', import.meta.url);
const attachmentContractPath = new URL('../agent-skill/organize-dispute-materials/references/attachment-reader-contract.md', import.meta.url);
const runtimeContractPath = new URL('../agent-skill/organize-dispute-materials/references/feishu-runtime-contract.md', import.meta.url);

 test('agent Skill and component share the exact-record production contract', async () => {
  const skill = await readFile(skillPath, 'utf8');
  const agentPrompt = await readFile(agentPromptPath, 'utf8');
  const attachmentContract = await readFile(attachmentContractPath, 'utf8');
  const runtimeContract = await readFile(runtimeContractPath, 'utf8');

  assert.match(skill, /version: 3\.3\.3/);
  assert.match(skill, /table_id/);
  assert.match(skill, /record_id/);
  assert.match(skill, /record_id.*唯一.*定位键/);
  assert.match(skill, /上传材料/);
  assert.match(skill, /full_access/);
  assert.match(skill, /member_type.*openid|成员类型 `openid`/);
  assert.match(skill, /权限读回/);
  assert.match(skill, /原生飞书云文档/);
  assert.match(skill, /状态.*分析中/);
  assert.match(skill, /AI分析结果/);
  assert.match(skill, /BASE_CONNECTOR_UNAVAILABLE/);
  assert.match(skill, /禁止用 `bash` 搜索/);
  assert.match(skill, /只更新组件传入、精确读取并锁定的同一 `record_id`/);
  assert.doesNotMatch(skill, /案件文档/);
  assert.doesNotMatch(skill, /AI处理状态/);
  assert.doesNotMatch(skill, /待法务审核/);
  assert.doesNotMatch(skill, /aixuexi\.feishu\.cn/);

  assert.match(agentPrompt, /RUNTIME_INPUT_JSON/);
  assert.match(agentPrompt, /BASE_CONNECTOR_UNAVAILABLE/);
  assert.match(agentPrompt, /禁止用 bash 搜索/);
  assert.match(agentPrompt, /member_type=openid/);
  assert.match(agentPrompt, /full_access/);
  assert.doesNotMatch(agentPrompt, /仅处理消息中的唯一案件编号|在生产 Base 中精确匹配且只命中一条记录/);
  assert.doesNotMatch(agentPrompt, /aixuexi\.feishu\.cn/);

  assert.match(attachmentContract, /table_id/);
  assert.match(attachmentContract, /record_id/);
  assert.match(attachmentContract, /读取正文/);
  assert.match(attachmentContract, /只有元数据不能算正文读取成功/);
  assert.match(attachmentContract, /full_access/);
  assert.match(attachmentContract, /BASE_CONNECTOR_UNAVAILABLE/);
  assert.match(attachmentContract, /不得调用 bash 搜索/);
  assert.match(attachmentContract, /同一记录写回与读回/);
  assert.match(runtimeContract, /base_record_get_exact/);
  assert.match(runtimeContract, /BASE_CONNECTOR_UNAVAILABLE/);
  assert.match(runtimeContract, /\| 定位键 \|[^\n]*record_id/);
  assert.doesNotMatch(runtimeContract, /\| 定位键 \|\s*案件编号\s*\|/);
  assert.doesNotMatch(attachmentContract, /案件文档|AI处理状态/);

  await access(new URL('../agent-skill/organize-dispute-materials/assets/reference-template.docx', import.meta.url));
  await access(new URL('../agent-skill/organize-dispute-materials/references/template-contract.md', import.meta.url));
});
