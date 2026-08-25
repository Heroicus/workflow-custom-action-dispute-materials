import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { promisify } from 'node:util';
import test from 'node:test';

const execFileAsync = promisify(execFile);
const skillRoot = fileURLToPath(
  new URL('../agent-skill/organize-dispute-materials/', import.meta.url),
);

test('Aily upload package keeps SKILL.md at the archive root', async (t) => {
  const temporary = await mkdtemp(join(tmpdir(), 'organize-dispute-materials-'));
  t.after(() => rm(temporary, { recursive: true, force: true }));
  const zipPath = join(temporary, 'organize-dispute-materials-v3.3.3.zip');
  await execFileAsync('python3', [
    join(skillRoot, 'scripts/package_skill.py'),
    '--source',
    skillRoot,
    '--output',
    zipPath,
  ]);
  const { stdout } = await execFileAsync('unzip', ['-Z1', zipPath]);
  const entries = stdout.trim().split(/\r?\n/).filter(Boolean);
  assert.ok(entries.includes('SKILL.md'), 'SKILL.md must be at ZIP root');
  assert.ok(entries.includes('agents/openai.yaml'));
  assert.ok(entries.includes('references/feishu-runtime-contract.md'));
  assert.ok(entries.includes('assets/reference-template.docx'));
  assert.ok(entries.includes('scripts/validate_template.py'));
  assert.equal(
    entries.some((entry) => entry.startsWith('organize-dispute-materials/')),
    false,
    'do not wrap the package in a second root directory',
  );
  const { stdout: skill } = await execFileAsync('unzip', ['-p', zipPath, 'SKILL.md']);
  assert.match(skill, /version: 3\.3\.3/);
  assert.match(skill, /feishu-runtime-contract\.md/);
});
