import {api, apiOnce} from '../../core.js';

export const SECURITY_SCHEMA_VERSION = 2;
export const SECURITY_SECTIONS = Object.freeze([
  'overview', 'cases', 'incidents', 'events', 'rules', 'ipsets',
  'recommendations', 'schemas',
]);

const ENVELOPE_STATES = new Set([
  'available', 'empty', 'unavailable', 'partial', 'failed', 'stale',
]);

export class SecurityContractError extends Error {
  constructor(message = 'Security data uses an unsupported contract.') {
    super(message); this.name = 'SecurityContractError';
    this.code = 'security_contract_invalid';
  }
}

export class SecurityConflictError extends Error {
  constructor() {
    super('Security state changed. The latest version was loaded; review it and confirm again.');
    this.name = 'SecurityConflictError'; this.code = 'stale_revision';
    this.status = 409;
  }
}

function safeSectionName(name) {
  return SECURITY_SECTIONS.includes(name) ? name : null;
}

function sectionEnvelope(name, value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || !ENVELOPE_STATES.has(value.status)
      || typeof value.cutoff !== 'string'
      || !value.window || typeof value.window !== 'object'
      || typeof value.truncated !== 'boolean'
      || !Object.prototype.hasOwnProperty.call(value, 'data')) {
    throw new SecurityContractError(`The ${name} security section is malformed.`);
  }
  return Object.freeze({
    status: value.status, observed_at: typeof value.observed_at === 'string' ? value.observed_at : null,
    cutoff: value.cutoff, window: Object.freeze({...value.window}),
    truncated: value.truncated, reason: typeof value.reason === 'string' ? value.reason : '',
    data: value.data,
  });
}

export async function readSecurity(sections, {signal, limit = 100, params = {}} = {}) {
  const names = [...new Set(sections)].map(safeSectionName);
  if (!names.length || names.some((name) => name == null)) throw new SecurityContractError();
  const query = new URLSearchParams({sections: names.join(','), limit: String(limit)});
  for (const [key, value] of Object.entries(params)) {
    if (value != null && value !== '') query.set(key, String(value));
  }
  const result = await api(`/api/incident/admin/security?${query}`, {signal});
  if (!result || result.schema_version !== SECURITY_SCHEMA_VERSION
      || !result.sections || typeof result.sections !== 'object') {
    throw new SecurityContractError();
  }
  const values = {};
  for (const name of names) values[name] = sectionEnvelope(name, result.sections[name]);
  return Object.freeze({schema_version: result.schema_version, sections: Object.freeze(values)});
}

export function actionSchemas(report) {
  const section = report?.sections?.schemas;
  const actions = section?.data?.actions;
  if (!actions || typeof actions !== 'object' || Array.isArray(actions)) {
    throw new SecurityContractError('Security action schemas are unavailable.');
  }
  return actions;
}

export async function performSecurityAction(report, action, payload, {reread} = {}) {
  const schema = actionSchemas(report)[action];
  if (!schema || schema?.properties?.action?.const !== action
      || schema.additional_properties !== false) {
    throw new SecurityContractError('This security action is not advertised by the server.');
  }
  try {
    return await apiOnce('/api/incident/admin/security/action', {
      method: 'POST', body: JSON.stringify({...payload, action}),
    });
  } catch (error) {
    if (error?.status === 409) {
      await reread?.();
      throw new SecurityConflictError();
    }
    throw error;
  }
}

export function sectionRows(envelope) {
  return Array.isArray(envelope?.data) ? envelope.data : [];
}
