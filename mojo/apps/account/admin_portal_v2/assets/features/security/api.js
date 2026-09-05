import {api, apiOnce} from '../../core.js';

export const SECURITY_SCHEMA_VERSION = 3;
export const SECURITY_SECTIONS = Object.freeze([
  'overview', 'cases', 'incidents', 'events', 'rules', 'ipsets',
  'recommendations', 'schemas',
]);

const ENVELOPE_STATES = new Set([
  'available', 'empty', 'unavailable', 'partial', 'failed', 'stale',
]);
const ROW_LIMIT = 100;
const TARGET_LIMIT = 1024;
const HOST_LIMIT = 128;
const OBJECT_ID_LIMIT = 2147483647;
const ACTION_NAMES = Object.freeze([
  'ruleset.create', 'ruleset.replace', 'ruleset.activate',
  'ruleset.deactivate', 'ruleset.delete', 'recommendation.approve',
  'recommendation.reject', 'recommendation.cancel',
  'recommendation.reverse', 'ipset.sync', 'ipset.enable', 'ipset.disable',
]);
const POLICY_FIELDS = Object.freeze([
  'name', 'category', 'priority', 'bundle_minutes', 'bundle_by',
  'bundle_by_rule_set', 'match_by', 'trigger_count', 'trigger_window',
  'retrigger_every', 'handlers', 'rules', 'delete_on_resolution', 'is_active',
]);
const OVERVIEW_METRICS = Object.freeze([
  'open_incidents', 'active_rule_sets', 'pending_recommendations',
  'recommendation_transitions', 'case_learning', 'resolution_rate',
]);
const ACTION_REQUIRED = Object.freeze({
  'ruleset.create': ['action', 'confirm', 'ruleset'],
  'ruleset.replace': ['action', 'ruleset_id', 'expected_modified', 'confirm', 'ruleset'],
  'ruleset.activate': ['action', 'ruleset_id', 'expected_modified', 'confirm'],
  'ruleset.deactivate': ['action', 'ruleset_id', 'expected_modified', 'confirm'],
  'ruleset.delete': ['action', 'ruleset_id', 'expected_modified', 'confirm'],
  'recommendation.approve': ['action', 'recommendation_id', 'expected_modified', 'confirm'],
  'recommendation.reject': ['action', 'recommendation_id', 'expected_modified', 'confirm'],
  'recommendation.cancel': ['action', 'recommendation_id', 'expected_modified', 'confirm'],
  'recommendation.reverse': ['action', 'recommendation_id', 'expected_modified', 'confirm'],
  'ipset.sync': ['action', 'ipset_id', 'expected_modified', 'confirm'],
  'ipset.enable': ['action', 'ipset_id', 'expected_modified', 'confirm'],
  'ipset.disable': ['action', 'ipset_id', 'expected_modified', 'confirm'],
});

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

function plainObject(value) {
  return value != null && typeof value === 'object' && !Array.isArray(value);
}

function hasOwn(value, key) {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function invalid(name) {
  throw new SecurityContractError(`The ${name} security section is malformed.`);
}

function validString(value, maximum = 512, nullable = false) {
  return (nullable && value == null)
    || (typeof value === 'string' && value.length <= maximum);
}

function validInteger(value, maximum = 1000000, minimum = 0) {
  return Number.isInteger(value) && value >= minimum && value <= maximum;
}

function validChunk(value) {
  return plainObject(value) && ['text', 'json'].includes(value.encoding)
    && typeof value.chunk === 'string' && validInteger(value.offset, Number.MAX_SAFE_INTEGER)
    && validInteger(value.next_offset, Number.MAX_SAFE_INTEGER)
    && validInteger(value.byte_length, Number.MAX_SAFE_INTEGER)
    && typeof value.complete === 'boolean'
    && validString(value.next_cursor, 4096, true)
    && typeof value.digest === 'string' && /^[0-9a-f]{64}$/.test(value.digest);
}

function validateCapabilities(value) {
  if (!plainObject(value) || !['global', 'group'].includes(value.scope)
      || !validString(value.credential_kind, 64)
      || typeof value.view !== 'boolean' || typeof value.manage !== 'boolean'
      || !plainObject(value.fresh_auth)
      || typeof value.fresh_auth.enabled !== 'boolean'
      || !validInteger(value.fresh_auth.window_seconds, 86400 * 30)
      || typeof value.fresh_auth.applies_to_credential !== 'boolean'
      || (value.group_id != null && !validInteger(value.group_id, OBJECT_ID_LIMIT, 1))) {
    throw new SecurityContractError('Security capabilities are malformed.');
  }
}

function sameMembers(left, right) {
  return Array.isArray(left) && left.length === right.length
    && new Set(left).size === left.length
    && right.every((value) => left.includes(value));
}

function validWindow(value) {
  return plainObject(value) && sameMembers(Object.keys(value), ['hours', 'start', 'end'])
    && validInteger(value.hours, 2160, 1)
    && validString(value.start, 64) && validString(value.end, 64);
}

function requireFields(row, fields, name) {
  if (!plainObject(row) || fields.some((field) => !hasOwn(row, field))) invalid(name);
}

function validateHostList(value) {
  if (!Array.isArray(value) || value.length > HOST_LIMIT) return false;
  const unique = new Set(value);
  return unique.size === value.length && value.every((host) =>
    typeof host === 'string' && host.length <= 254
      && /^(?=.*[a-z])[a-z0-9][a-z0-9.-]*$/.test(host));
}

function validateCases(data, name) {
  const strings = ['created', 'first_seen', 'last_seen', 'sensor_kind',
    'resource_id', 'family', 'state', 'urgency', 'accuracy'];
  const counts = ['occurrence_count', 'receipt_count', 'projected_event_count',
    'distinct_count', 'sample_count', 'overflow_count', 'distinct_source_count',
    'policy_version', 'evaluator_version'];
  for (const row of data) {
    requireFields(row, ['id', ...strings, ...counts], name);
    if (!validInteger(row.id, OBJECT_ID_LIMIT, 1)
        || strings.some((field) => !validString(row[field]))
        || counts.some((field) => !validInteger(row[field]))) invalid(name);
  }
}

function validateIncidents(data, name) {
  const fields = ['created', 'priority', 'state', 'status', 'scope', 'category',
    'group_id', 'rule_set_id'];
  for (const row of data) {
    requireFields(row, ['id', ...fields], name);
    if (!validInteger(row.id, OBJECT_ID_LIMIT, 1)
        || !validString(row.created, 64)
        || !validInteger(row.priority, 10000)
        || ['state', 'status', 'scope', 'category'].some(
          (field) => !validString(row[field]))
        || (row.group_id != null && !validInteger(row.group_id, OBJECT_ID_LIMIT, 1))
        || (row.rule_set_id != null && !validInteger(row.rule_set_id, OBJECT_ID_LIMIT, 1))) invalid(name);
  }
}

function validateEvents(data, name) {
  const strings = ['created', 'scope', 'category', 'country_code'];
  for (const row of data) {
    requireFields(row, ['id', 'level', 'title', 'group_id', 'incident_id', ...strings], name);
    if (!validInteger(row.id, OBJECT_ID_LIMIT, 1)
        || !validInteger(row.level, 10000)
        || !(validString(row.title, 512, true) || validChunk(row.title))
        || strings.some((field) => !validString(row[field], 512, true))
        || (row.group_id != null && !validInteger(row.group_id, OBJECT_ID_LIMIT, 1))
        || (row.incident_id != null && !validInteger(row.incident_id, OBJECT_ID_LIMIT, 1))) invalid(name);
  }
}

function validateRules(data, name) {
  const strings = ['created', 'modified', 'category'];
  const integers = ['priority', 'bundle_minutes', 'bundle_by', 'match_by',
    'trigger_count', 'trigger_window', 'retrigger_every'];
  for (const row of data) {
    requireFields(row, ['id', ...strings, ...integers, 'is_active',
      'bundle_by_rule_set', 'validation'], name);
    if (!validInteger(row.id, OBJECT_ID_LIMIT, 1)
        || strings.some((field) => !validString(row[field]))
        || !(validString(row.name) || validChunk(row.name))
        || integers.some((field) => row[field] != null && !validInteger(row[field], 1000000))
        || typeof row.is_active !== 'boolean'
        || typeof row.bundle_by_rule_set !== 'boolean'
        || !plainObject(row.validation)
        || !['valid', 'legacy', 'replacement_required'].includes(row.validation.status)
        || typeof row.validation.legacy !== 'boolean'
        || !Array.isArray(row.validation.handlers)
        || row.validation.handlers.length > 8
        || row.validation.handlers.some((handler) => !plainObject(handler))) invalid(name);
    const summary = validInteger(row.rule_count);
    const hasDetail = ['handler', 'metadata', 'rules', 'delete_on_resolution']
      .some((field) => hasOwn(row, field));
    const detail = !hasDetail || (validChunk(row.handler)
      && validChunk(row.metadata) && validChunk(row.rules)
      && (!hasOwn(row, 'handlers') || (Array.isArray(row.handlers)
        && row.handlers.length <= 8 && row.handlers.every(plainObject)))
      && typeof row.delete_on_resolution === 'boolean');
    if (!summary && !detail) invalid(name);
  }
}

function validateIPSets(data, name) {
  const states = new Set(['verified', 'stale', 'missing', 'partial', 'unavailable']);
  for (const row of data) {
    requireFields(row, ['id', 'created', 'modified', 'name', 'kind', 'description',
      'is_enabled', 'cidr_count', 'last_synced', 'has_sync_error',
      'enforcement_status', 'enforcement_ok', 'enforcement'], name);
    const proof = row.enforcement;
    if (!validInteger(row.id, OBJECT_ID_LIMIT, 1)
        || ['created', 'modified', 'name', 'kind'].some(
          (field) => !validString(row[field]))
        || !validString(row.description, 512, true)
        || typeof row.is_enabled !== 'boolean' || !validInteger(row.cidr_count)
        || !validString(row.last_synced, 64, true)
        || typeof row.has_sync_error !== 'boolean' || !states.has(row.enforcement_status)
        || typeof row.enforcement_ok !== 'boolean' || !plainObject(proof)) invalid(name);
    requireFields(proof, ['status', 'desired', 'observed', 'generation',
      'observation_cutoff', 'expected_host_ids', 'responded_host_ids',
      'succeeded_host_ids', 'failed_host_ids', 'missing_host_ids'], name);
    const desired = proof.desired;
    if (!states.has(proof.status) || proof.status !== row.enforcement_status
        || proof.observed !== proof.status
        || row.enforcement_ok !== (proof.status === 'verified')
        || !plainObject(desired)
        || Object.keys(desired).length > 3
        || Object.keys(desired).some(
          (field) => !['present', 'count', 'digest'].includes(field))
        || (hasOwn(desired, 'present') && typeof desired.present !== 'boolean')
        || (hasOwn(desired, 'count') && !validInteger(desired.count))
        || (hasOwn(desired, 'digest')
          && (typeof desired.digest !== 'string' || !/^[0-9a-f]{64}$/.test(desired.digest)))
        || (proof.generation != null
          && !validInteger(proof.generation, Number.MAX_SAFE_INTEGER, 1))
        || !validString(proof.observation_cutoff, 64)
        || ['expected_host_ids', 'responded_host_ids', 'succeeded_host_ids',
          'failed_host_ids', 'missing_host_ids'].some(
          (field) => !validateHostList(proof[field]))) invalid(name);
    if (proof.status === 'verified'
        && (!proof.expected_host_ids.length
          || JSON.stringify(proof.expected_host_ids) !== JSON.stringify(proof.responded_host_ids)
          || JSON.stringify(proof.expected_host_ids) !== JSON.stringify(proof.succeeded_host_ids)
          || proof.failed_host_ids.length || proof.missing_host_ids.length)) invalid(name);
    for (const field of ['data', 'sync_error', 'checked_proof']) {
      if (hasOwn(row, field) && !validChunk(row[field])) invalid(name);
    }
  }
}

function validateRecommendations(data, name) {
  const strings = ['created', 'modified', 'action', 'state', 'reason_code',
    'confidence', 'urgency', 'requested_scope'];
  const counts = ['requested_ttl_seconds', 'target_count', 'validated_count',
    'protected_count', 'executed_count', 'failed_count', 'reversed_count',
    'policy_version', 'evaluator_version'];
  for (const row of data) {
    requireFields(row, ['id', 'case_id', 'group_id', ...strings, ...counts, 'expires_at', 'approved_at'], name);
    if (!validInteger(row.id, OBJECT_ID_LIMIT, 1)
        || !validInteger(row.case_id, OBJECT_ID_LIMIT, 1)
        || (row.group_id != null && !validInteger(row.group_id, OBJECT_ID_LIMIT, 1))
        || strings.some((field) => !validString(row[field]))
        || counts.some((field) => row[field] != null && !validInteger(row[field]))
        || !validString(row.expires_at, 64, true)
        || !validString(row.approved_at, 64, true)) invalid(name);
    if (hasOwn(row, 'targets') && !validChunk(row.targets)
        && (!Array.isArray(row.targets) || row.targets.length > TARGET_LIMIT
          || typeof row.targets_truncated !== 'boolean'
          || row.targets.some((target) => !plainObject(target)))) invalid(name);
    for (const field of ['explanation', 'approval_note', 'collateral',
      'transitions', 'attempts']) {
      if (hasOwn(row, field) && !validChunk(row[field])) invalid(name);
    }
  }
}

function validateOverview(data, name) {
  requireFields(data, ['current', 'recommendation_transitions', 'accuracy',
    'unavailable', 'metric_definitions'], name);
  requireFields(data.current, ['open_incidents', 'active_rule_sets',
    'pending_recommendations'], name);
  if (Object.values(data.current).some((value) => !validInteger(value))
      || !plainObject(data.recommendation_transitions)
      || Object.keys(data.recommendation_transitions).length > ROW_LIMIT
      || Object.keys(data.recommendation_transitions).some(
        (key) => !validString(key, 64))
      || Object.values(data.recommendation_transitions).some(
        (value) => !validInteger(value))
      || !plainObject(data.accuracy) || !plainObject(data.unavailable)
      || !sameMembers(Object.keys(data.accuracy),
        ['current', 'recommendation_transitions', 'resolution_rate'])
      || Object.values(data.accuracy).some((value) => !validString(value, 64))
      || !sameMembers(Object.keys(data.unavailable), ['resolution_rate'])
      || !validString(data.unavailable.resolution_rate, 256)
      || !plainObject(data.metric_definitions)
      || !sameMembers(Object.keys(data.metric_definitions), OVERVIEW_METRICS)) invalid(name);
  for (const definition of Object.values(data.metric_definitions)) {
    if (!plainObject(definition) || !validString(definition.source, 256, true)
        || !validString(definition.accuracy, 64)
        || !(validString(definition.window, 32)
          || validWindow(definition.window))) invalid(name);
  }
}

function validateSchemas(data, name) {
  requireFields(data, ['rule_policy', 'actions', 'action_names'], name);
  const policy = data.rule_policy;
  const aggregate = policy?.aggregate;
  if (!plainObject(policy) || policy.schema_version !== 1 || !plainObject(aggregate)
      || aggregate.type !== 'object' || aggregate.additional_properties !== false
      || !sameMembers(aggregate.required, ['name', 'category'])
      || !plainObject(aggregate.properties)
      || !sameMembers(Object.keys(aggregate.properties), POLICY_FIELDS)
      || !plainObject(policy.limits) || policy.limits.rules !== 32
      || policy.limits.handlers !== 8
      || !Array.isArray(data.action_names)
      || JSON.stringify(data.action_names) !== JSON.stringify(ACTION_NAMES)
      || !plainObject(data.actions)
      || Object.keys(data.actions).length !== ACTION_NAMES.length
      || ACTION_NAMES.some((action) => !hasOwn(data.actions, action))) invalid(name);
  const policyTypes = {
    name: 'string', category: 'string', priority: 'integer',
    bundle_minutes: ['integer', 'null'], bundle_by: 'integer',
    bundle_by_rule_set: 'boolean', match_by: 'integer',
    trigger_count: ['integer', 'null'], trigger_window: ['integer', 'null'],
    retrigger_every: ['integer', 'null'], handlers: 'array', rules: 'array',
    delete_on_resolution: 'boolean', is_active: 'boolean',
  };
  for (const field of POLICY_FIELDS) {
    const schema = aggregate.properties[field];
    if (!plainObject(schema)
        || JSON.stringify(schema.type) !== JSON.stringify(policyTypes[field])) invalid(name);
  }
  if (aggregate.properties.handlers.max_items !== 8
      || aggregate.properties.rules.max_items !== 32
      || aggregate.properties.name.min_length !== 1
      || aggregate.properties.name.max_length !== 160
      || aggregate.properties.category.min_length !== 1
      || aggregate.properties.category.max_length !== 124
      || aggregate.properties.priority.minimum !== 0
      || aggregate.properties.priority.maximum !== 10000
      || aggregate.properties.bundle_minutes.minimum !== 0
      || aggregate.properties.bundle_minutes.maximum !== 10080
      || aggregate.properties.trigger_count.minimum !== 1
      || aggregate.properties.trigger_count.maximum !== 1000000
      || aggregate.properties.trigger_window.minimum !== 1
      || aggregate.properties.trigger_window.maximum !== 10080
      || aggregate.properties.retrigger_every.minimum !== 1
      || aggregate.properties.retrigger_every.maximum !== 1000000
      || !Array.isArray(aggregate.properties.bundle_by.enum)
      || !aggregate.properties.bundle_by.enum.length
      || aggregate.properties.bundle_by.enum.length > 8
      || aggregate.properties.bundle_by.enum.some(
        (value) => !validInteger(value, 16))
      || !Array.isArray(aggregate.properties.match_by.enum)
      || !aggregate.properties.match_by.enum.length
      || aggregate.properties.match_by.enum.length > 8
      || aggregate.properties.match_by.enum.some(
        (value) => !validInteger(value, 16))) invalid(name);
  for (const action of ACTION_NAMES) {
    const schema = data.actions[action];
    const properties = schema?.properties;
    if (!plainObject(schema) || schema.type !== 'object'
        || schema.additional_properties !== false
        || !sameMembers(schema.required, ACTION_REQUIRED[action])
        || !plainObject(properties) || properties.action?.const !== action
        || properties.action?.type !== 'string' || properties.confirm?.type !== 'string'
        || properties.confirm?.min_length !== 1
        || properties.confirm?.max_length !== 128
        || !plainObject(schema.confirmation)
        || !['exact', 'template'].includes(schema.confirmation.kind)
        || !validString(schema.confirmation.value, 128)
        || !schema.confirmation.value.length) invalid(name);
    const identity = action.startsWith('ruleset.') ? 'ruleset_id'
      : action.startsWith('recommendation.') ? 'recommendation_id' : 'ipset_id';
    if (action !== 'ruleset.create'
        && (properties[identity]?.type !== 'integer'
          || properties[identity]?.minimum !== 1
          || properties[identity]?.maximum !== OBJECT_ID_LIMIT
          || properties.expected_modified?.type !== 'string'
          || properties.expected_modified?.format !== 'date-time'
          || properties.expected_modified?.max_length !== 64)) invalid(name);
    if (action === 'ruleset.create' || action === 'ruleset.replace') {
      if (properties.ruleset?.$ref !== 'rule_policy.aggregate') invalid(name);
    }
    if (action === 'ruleset.activate'
        && (properties.confirm_catch_all?.type !== 'string'
          || properties.confirm_catch_all?.max_length !== 128
          || !validString(schema.confirmation.catch_all_value, 128)
          || !schema.confirmation.catch_all_value.length)) invalid(name);
    if (action.startsWith('recommendation.')
        && (properties.note?.type !== 'string'
          || properties.note?.max_length !== 256)) invalid(name);
  }
}

function validateSectionData(name, status, data) {
  if (status === 'unavailable' || status === 'failed') {
    if (!plainObject(data) || Object.keys(data).length) invalid(name);
    return;
  }
  if (name === 'overview') return validateOverview(data, name);
  if (name === 'schemas') return validateSchemas(data, name);
  if (!Array.isArray(data) || data.length > ROW_LIMIT) invalid(name);
  const validators = {cases: validateCases, incidents: validateIncidents,
    events: validateEvents, rules: validateRules, ipsets: validateIPSets,
    recommendations: validateRecommendations};
  validators[name](data, name);
}

function sectionEnvelope(name, value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || !ENVELOPE_STATES.has(value.status)
      || !validString(value.observed_at, 64, true)
      || !validString(value.cutoff, 64)
      || !validWindow(value.window)
      || typeof value.truncated !== 'boolean'
      || !validString(value.next_cursor, 4096, true)
      || (value.truncated && !value.next_cursor
        && !['overview', 'schemas'].includes(name))
      || (hasOwn(value, 'reason') && !validString(value.reason, 128))
      || !Object.prototype.hasOwnProperty.call(value, 'data')) {
    throw new SecurityContractError(`The ${name} security section is malformed.`);
  }
  validateSectionData(name, value.status, value.data);
  return Object.freeze({
    status: value.status, observed_at: typeof value.observed_at === 'string' ? value.observed_at : null,
    cutoff: value.cutoff, window: Object.freeze({...value.window}),
    truncated: value.truncated, reason: typeof value.reason === 'string' ? value.reason : '',
    next_cursor: value.next_cursor || null, data: value.data,
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
  validateCapabilities(result.capabilities);
  const values = {};
  for (const name of names) values[name] = sectionEnvelope(name, result.sections[name]);
  return Object.freeze({schema_version: result.schema_version,
    capabilities: Object.freeze({...result.capabilities}), sections: Object.freeze(values)});
}

export async function readNextSecurityPage(report, section, {signal} = {}) {
  const current = report?.sections?.[section];
  if (!current?.next_cursor || !Array.isArray(current.data)) return report;
  const next = await readSecurity(
    [section], {signal, params: {page_cursor: current.next_cursor}});
  const page = next.sections[section];
  if (next.capabilities.scope !== report.capabilities.scope
      || next.capabilities.group_id !== report.capabilities.group_id
      || next.capabilities.credential_kind !== report.capabilities.credential_kind
      || page.window.start !== current.window.start
      || page.window.end !== current.window.end) {
    throw new SecurityContractError('Security evidence page changed authority or snapshot.');
  }
  const known = new Set(current.data.map((row) => row.id));
  if (page.data.some((row) => known.has(row.id))) {
    throw new SecurityContractError('Security evidence page repeated a row.');
  }
  const mergedSection = Object.freeze({...page,
    data: Object.freeze([...current.data, ...page.data])});
  return Object.freeze({...report, sections: Object.freeze({
    ...report.sections, [section]: mergedSection,
  })});
}

export async function readSecurityChunk(cursor, {signal} = {}) {
  const query = new URLSearchParams({chunk_cursor: cursor});
  const result = await api(`/api/incident/admin/security?${query}`, {signal});
  if (!result || result.schema_version !== SECURITY_SCHEMA_VERSION
      || !validChunk(result.chunk)) throw new SecurityContractError('Security evidence chunk is malformed.');
  validateCapabilities(result.capabilities);
  return result.chunk;
}

export async function completeChunk(first, {signal} = {}) {
  if (!validChunk(first)) throw new SecurityContractError('Security evidence chunk is malformed.');
  let value = first.chunk; let current = first; let count = 0;
  while (!current.complete) {
    if (!current.next_cursor || ++count > 4096) {
      throw new SecurityContractError('Security evidence pagination did not terminate.');
    }
    current = await readSecurityChunk(current.next_cursor, {signal});
    if (current.digest !== first.digest) {
      throw new SecurityContractError('Security evidence changed during retrieval.');
    }
    value += current.chunk;
  }
  if (first.encoding === 'json') {
    try { return JSON.parse(value); } catch { throw new SecurityContractError('Security evidence JSON is malformed.'); }
  }
  return value;
}

export async function readSecurityDetail(section, id, {signal} = {}) {
  const idNames = {cases: 'case_id', incidents: 'incident_id', events: 'event_id',
    rules: 'ruleset_id', ipsets: 'ipset_id', recommendations: 'recommendation_id'};
  const idName = idNames[section];
  if (!idName || !validInteger(id, OBJECT_ID_LIMIT, 1)) throw new SecurityContractError();
  const report = await readSecurity([section], {signal, params: {[idName]: id}});
  const row = sectionRows(report.sections[section])[0];
  if (!row) throw new SecurityContractError('Security record is unavailable.');
  const expanded = {...row};
  for (const [field, value] of Object.entries(expanded)) {
    if (validChunk(value)) expanded[field] = await completeChunk(value, {signal});
  }
  return expanded;
}

export function actionSchemas(report) {
  const section = report?.sections?.schemas;
  const actions = section?.data?.actions;
  if (!actions || typeof actions !== 'object' || Array.isArray(actions)) {
    throw new SecurityContractError('Security action schemas are unavailable.');
  }
  validateSchemas(section.data, 'schemas');
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
  if (!Array.isArray(envelope?.data)) {
    throw new SecurityContractError('The Security row collection is malformed.');
  }
  return envelope.data;
}
