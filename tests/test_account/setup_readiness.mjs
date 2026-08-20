import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';

const pageUrl = new URL(
  '../../mojo/apps/account/admin_portal/assets/features/platform/page.js',
  import.meta.url,
);
const page = await readFile(pageUrl, 'utf8');
const start = page.indexOf('function operatorChecks');
const end = page.indexOf('function checkAction');
assert.notEqual(start, -1, 'the System Setup readiness normalizer is missing');
assert.notEqual(end, -1, 'the System Setup renderer boundary is missing');
const source = [
  "const READINESS_SEVERITY = ['fail', 'pending', 'warn', 'pass'];",
  page.slice(start, end),
  'export {operatorReport};',
].join('\n');
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
const {operatorReport} = await import(moduleUrl);

function renderStatuses(report) {
  return report.sections.flatMap((section) => [
    section.status.toUpperCase(),
    ...section.checks.map((check) => check.status.toUpperCase()),
  ]);
}

const validSection = {
  code: 'django', label: 'Django installation', status: 'warn',
  checks: [{
    code: 'django.database', status: 'warn',
    explanation: 'The database needs attention.', remediation: 'Inspect it.',
  }],
};

test('a scalar section cannot crash the readiness rendering pipeline', () => {
  const report = operatorReport({sections: ['[truncated]', validSection]});
  assert.deepEqual(renderStatuses(report), ['WARN', 'WARN']);
  assert.equal(report.truncated, true);
});

test('a scalar check cannot crash the readiness rendering pipeline', () => {
  const report = operatorReport({
    sections: [{...validSection, checks: ['[truncated]', ...validSection.checks]}],
  });
  assert.deepEqual(renderStatuses(report), ['WARN', 'WARN']);
  assert.equal(report.truncated, true);
});
