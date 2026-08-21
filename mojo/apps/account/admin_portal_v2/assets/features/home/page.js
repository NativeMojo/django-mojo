// Home — what actually needs you, and nothing else.
//
// Every fact on this page comes from an endpoint that already exists and is
// already read by v1's Dashboard, Apps list and Activity lane. Nothing here
// invents a number, softens a state, or fills a gap with a plausible-looking
// placeholder: a panel whose source failed says so, in that panel, and the
// headline is the WORST of what the sources actually reported.
//
// Endpoints consumed:
//   /api/account/admin/dashboard   the availability envelope + one envelope per
//                                  source (v1 Dashboard's own read)
//   /api/edge/webapp/summaries     the Apps tile (v1 Apps list's own read)
//   /api/logs | /api/incident/event  the newest few activity rows, capability-gated
//
// Each read fails on its own. One dead source never blanks the page.

import {api, apiEnvelope, h, icon} from '../../core.js';
import {runAction} from '../../components/actions.js';
import {routeHref} from '../../components/routes.js';
import {errorState, loadingState} from '../../components/views.js';
import {activityTabVisible} from './activity.js';
import {
  list as listOperations, remove as removeOperation,
  subscribe as subscribeOperations, upsert as upsertOperation,
} from '../../components/operations.js';

// ── vocabulary ─────────────────────────────────────────────────────────────

const DOT = {
  danger: 'dot danger', warn: 'dot warning', ok: 'dot success',
  accent: 'dot accent', muted: 'dot',
};

const SEVERITY = {danger: 0, warn: 1, ok: 2, accent: 2, muted: 3};

// A source whose status is one of these carries no evidence: the caller's role
// cannot read it, or the collector never answered. Neither is a blocker — an
// unreadable source is not a healthy one and not a broken one.
const UNREADABLE = new Set(['permission_denied', 'unknown']);

// Plain words for a failing node check, copied from v1's Dashboard so the same
// failure reads identically in both portals.
const SANITY_COPY = {
  'django apps': 'the app registry did not load',
  'database': 'the database did not answer',
  'migrations': 'database migrations are not applied',
  'redis': 'Redis did not answer',
  'local request': 'this node did not answer its own API',
};

// The platform's own deployment is the ONE long-running operation any endpoint
// reports without being handed an operation id first — see the note above
// syncDeploymentOperation().
const DEPLOY_ACTIVE = new Set(['requested', 'canary', 'fleet']);
const DEPLOY_OPERATION_ID = 'platform-deployment';
const POLL_INTERVAL = 30000;
const POLL_LIMIT = 60;

function dot(tone, extra = '') {
  return h('span', {class: `${DOT[tone] || DOT.muted} ${extra}`.trim()});
}

function tone(source) {
  const status = source?.status || 'unknown';
  if (status === 'healthy') return 'ok';
  if (status === 'unhealthy') return 'danger';
  if (status === 'degraded') return 'warn';
  return 'muted';
}

/** The source, or null when it carries no evidence to reason about. */
function reported(source) {
  if (!source || UNREADABLE.has(source.status)) return null;
  if (source.reason_detail?.iam_action) return null;
  return source;
}

function plural(count, one, many) {
  return count === 1 ? one : many;
}

function sentence(text) {
  const value = String(text || '').trim();
  if (!value) return '';
  return /[.!?]$/.test(value) ? value : `${value}.`;
}

function shortTime(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? String(value)
    : date.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
}

function agoText(value) {
  const at = Date.parse(value);
  if (!Number.isFinite(at)) return '';
  const minutes = Math.round((Date.now() - at) / 60000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} ${plural(hours, 'hr', 'hrs')} ago`;
  const days = Math.round(hours / 24);
  return days === 1 ? 'yesterday' : `${days} days ago`;
}

function elapsedText(startedAt) {
  const minutes = Math.max(0, Math.round((Date.now() - startedAt) / 60000));
  if (minutes < 1) return 'started just now';
  if (minutes < 60) return `started ${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  return `started ${hours} ${plural(hours, 'hr', 'hrs')} ago`;
}

// ── destinations ───────────────────────────────────────────────────────────

/**
 * A link into the portal that owns the fix. v2 owns six destinations; anything
 * whose screen has not been built here yet links into v1 instead, and says so
 * out loud rather than dropping the operator into different chrome unannounced.
 */
function v1Href(ctx, route) {
  return `${ctx.admin_path || '/admin/'}#/${route}`;
}

function v1Action(ctx, label, route) {
  return {label, href: v1Href(ctx, route), external: true};
}

function v2Action(ctx, label, route, fallback) {
  const enabled = {
    apps: ctx.features?.webapps?.enabled === true,
    // Infrastructure is three capability-gated tabs, so the block alone is not
    // enough: a caller with the platform block and none of the three grants
    // gets no Infrastructure entry, and must be sent to v1 like anyone else.
    // Same gate as features/infrastructure/feature.js.
    infrastructure: ctx.features?.platform?.enabled === true
      && ['capacity', 'metrics', 'maintenance'].some(
        (name) => ctx.features.platform.capabilities?.[name] === true),
    domains: ctx.features?.advanced?.enabled === true,
    access: ctx.features?.people?.enabled === true,
    settings: ctx.features?.settings?.enabled === true,
    // The two integration sub-pages of Settings. Each is gated on its OWN
    // bootstrap block, not on the settings block: they are the pages that own
    // the provider config and the test tools, and a caller may hold one
    // without the other.
    'settings-sms': ctx.features?.sms?.enabled === true,
    'settings-email': ctx.features?.email?.enabled === true,
  }[route] === true;
  if (enabled) return {label, href: `#/${route}`, external: false};
  return fallback ? v1Action(ctx, label, fallback) : null;
}

// Activity is Home's own sub-page, so it takes a tab rather than a route: the
// link opens the view that carries the evidence for the thing being reported.
// A tab this caller cannot read is not offered here — the link falls back to
// the current Admin, which refuses it in exactly the words it does today.
function activityAction(ctx, label, tab) {
  if (activityTabVisible(ctx, tab)) {
    return {label, href: routeHref('activity', {tab}), external: false};
  }
  return v1Action(ctx, label, `activity?tab=${tab}`);
}

// ── blockers ───────────────────────────────────────────────────────────────

// One entry per thing that is actually wrong, each derived from a single fact
// the dashboard endpoint reported. Order does not matter here — the list is
// sorted by severity before it is rendered.
function blockersFrom(ctx, report) {
  const sources = report?.sources || {};
  const out = [];
  const add = (entry) => { if (entry) out.push(entry); };

  const api_ = reported(sources.public_api);
  if (api_) {
    const data = api_.data || {};
    if (api_.status === 'unhealthy') {
      add({tone: 'danger', name: 'The public API did not answer',
        copy: 'The address customers and integrations use is not responding, '
          + 'even though this installation believes it is configured.',
        action: ctx.capabilities?.setup ? v1Action(ctx, 'Open System Setup', 'setup') : null});
    } else if (api_.status === 'unconfigured' || !data.configured) {
      add({tone: 'warn', name: 'The public API address is not set',
        copy: 'Invites, password resets and webhooks cannot build a working '
          + 'link until this installation knows its own address.',
        action: ctx.capabilities?.setup ? v1Action(ctx, 'Set it up', 'setup') : null});
    }
  }

  const sanity = reported(sources.sanity);
  const failing = (sanity?.data?.checks || []).filter((row) => !row.ok);
  if (failing.length) {
    const first = SANITY_COPY[failing[0].name] || failing[0].name;
    add({tone: 'danger',
      name: failing.length > 1
        ? `This node failed ${failing.length} core checks`
        : 'This node failed a core check',
      copy: `${sentence(first)} The balancer can still be sending it traffic `
        + 'while it is in this state.',
      action: ctx.features?.activity?.capabilities?.view_logs === true
        ? activityAction(ctx, 'See the logs', 'logs') : null});
  }

  const database = reported(sources.database);
  if (database) {
    const data = database.data || {};
    if (database.status === 'unhealthy' || data.reachable === false) {
      add({tone: 'danger', name: 'The database is unreachable',
        copy: 'Nothing that reads or writes data can work until it answers.',
        action: v2Action(ctx, 'Open Infrastructure', 'infrastructure', 'fleet')});
    } else if (data.drift?.available_major) {
      const deadline = data.drift.note ? ` — ${data.drift.note}` : '';
      add({tone: 'warn', name: `A major database upgrade is available (${data.drift.available_major})`,
        copy: `This installation runs ${[data.engine, data.version].filter(Boolean).join(' ') || 'an older major version'}`
          + `${deadline}. Upgrades of this size are planned, not rushed.`,
        action: v1Action(ctx, 'Open Maintenance', 'maintenance')});
    }
  }

  const cache = reported(sources.cache);
  if (cache && (cache.status === 'unhealthy' || cache.data?.reachable === false)) {
    add({tone: 'danger', name: 'The cache is unreachable',
      copy: 'Sessions, rate limits and cached reads fall through to the '
        + 'database or fail outright.',
      action: v2Action(ctx, 'Open Infrastructure', 'infrastructure', 'fleet')});
  }

  const compute = reported(sources.compute);
  const nodes = compute?.data || {};
  if (compute && nodes.total && (nodes.up || 0) < nodes.total) {
    const down = nodes.total - (nodes.up || 0);
    add({tone: nodes.up ? 'warn' : 'danger',
      name: `${down} of ${nodes.total} ${plural(nodes.total, 'node is', 'nodes are')} not healthy`,
      copy: nodes.up
        ? 'The fleet is carrying the same traffic on fewer nodes than it is meant to have.'
        : 'No node is reporting healthy — nothing is left to serve the traffic.',
      action: v2Action(ctx, 'Open Infrastructure', 'infrastructure', 'fleet')});
  }

  const balancer = reported(sources.load_balancer);
  if (balancer) {
    const data = balancer.data || {};
    if (data.configured === false) {
      add({tone: 'warn', name: 'No load balancer is configured',
        copy: 'Traffic reaches whichever node answers directly, and there is '
          + 'nothing in front to take an unhealthy node out of rotation.',
        action: v2Action(ctx, 'Open Infrastructure', 'infrastructure', 'fleet')});
    } else if (!data.registered) {
      add({tone: 'danger', name: 'The load balancer has no registered targets',
        copy: 'Nothing is behind the public address.',
        action: v2Action(ctx, 'Open Infrastructure', 'infrastructure', 'fleet')});
    } else if (data.elastic_ip_missing) {
      add({tone: 'warn', name: 'The load balancer has no elastic IP',
        copy: 'Its address changes if AWS replaces it, and anything pinned to '
          + 'that address stops working when it does.',
        action: v2Action(ctx, 'Open Infrastructure', 'infrastructure', 'fleet')});
    }
  }

  const certificates = reported(sources.certificates);
  const certs = certificates?.data || {};
  if (certificates && certs.total) {
    if (certs.failing) {
      add({tone: 'danger',
        name: `${certs.failing} ${plural(certs.failing, 'certificate is', 'certificates are')} not valid`,
        copy: 'Browsers refuse the addresses those certificates cover.',
        action: v2Action(ctx, 'Open Domains', 'domains', 'certificates')});
    } else if (certs.expiring_within_30_days) {
      const count = certs.expiring_within_30_days;
      add({tone: 'warn',
        name: `${count} ${plural(count, 'certificate expires', 'certificates expire')} within 30 days`,
        copy: 'Renewal is automatic while DNS still resolves the way it did at issue.',
        action: v2Action(ctx, 'Open Domains', 'domains', 'certificates')});
    }
  }

  const jobs = reported(sources.jobs);
  const queue = jobs?.data || {};
  const logsAction = ctx.features?.activity?.capabilities?.view_logs === true
    ? activityAction(ctx, 'See the logs', 'logs') : null;
  if (jobs && queue.jobs) {
    if (queue.scheduler_active === false) {
      add({tone: 'warn', name: 'The job scheduler is not running',
        copy: `Queued work is not being drained — ${queue.jobs.pending || 0} `
          + `${plural(queue.jobs.pending || 0, 'job is', 'jobs are')} waiting.`,
        action: logsAction});
    } else if (queue.failed_recent) {
      add({tone: 'warn',
        name: `${queue.failed_recent} ${plural(queue.failed_recent, 'job', 'jobs')} failed in the last hour`,
        copy: 'Something that was scheduled to run did not complete.',
        action: logsAction});
    }
  }

  const framework = reported(sources.framework);
  const build = framework?.data || {};
  if (framework && build.update_available && build.latest) {
    add({tone: 'warn', name: `django-mojo ${build.latest} is available`,
      copy: `This fleet runs ${build.installed || 'an older build'}; framework `
        + 'releases carry security fixes.',
      action: v1Action(ctx, 'Open Maintenance', 'maintenance')});
  }

  const email = reported(sources.email);
  const mail = email?.data || {};
  if (email && mail.configured) {
    if (mail.posture?.default_sender_conflict) {
      add({tone: 'danger', name: 'More than one mailbox claims the system default',
        copy: 'Which address this installation sends from is undefined until '
          + 'exactly one mailbox holds the default.',
        action: v2Action(ctx, 'Open Email', 'settings-email', 'messaging-email')});
    } else if (!mail.default_sender) {
      add({tone: 'danger', name: 'Email has no default sender',
        copy: 'Invites, alerts and password resets have no address to send '
          + 'from and will not be delivered.',
        action: v2Action(ctx, 'Verify a domain', 'settings-email', 'messaging-email')});
    } else if (mail.domains && !mail.sendable_domains) {
      add({tone: 'danger', name: 'No sender domain is ready to send',
        copy: 'Every domain on record is still unverified, so nothing this '
          + 'installation sends will be delivered.',
        action: v2Action(ctx, 'Verify a domain', 'settings-email', 'messaging-email')});
    }
  }

  const sms = reported(sources.sms);
  const messaging = sms?.data || {};
  if (sms && messaging.configured && messaging.verified?.ok === false) {
    add({tone: 'warn', name: 'The text-message provider failed its last connection test',
      copy: sentence(messaging.verified.message
        || 'The provider rejected the last connection test, so codes and alerts may not send.'),
      action: v2Action(ctx, 'Open Text messages', 'settings-sms', 'messaging-sms')});
  }

  const incidents = reported(sources.incidents);
  const open = incidents?.data?.open || 0;
  if (open) {
    const age = incidents.data.oldest_age_days;
    add({tone: 'warn', name: `${open} ${plural(open, 'incident needs', 'incidents need')} review`,
      copy: age == null
        ? 'Incidents stay open until somebody reads them and decides.'
        : `The oldest has been open for ${age} ${plural(age, 'day', 'days')}.`,
      action: activityAction(ctx, 'Review incidents', 'incidents')});
  }

  const tickets = reported(sources.tickets);
  const openTickets = tickets?.data?.open || 0;
  if (openTickets) {
    add({tone: 'warn',
      name: `${openTickets} ${plural(openTickets, 'ticket is', 'tickets are')} open`,
      copy: 'Work someone raised here is still waiting on an answer.',
      action: activityAction(ctx, 'Review tickets', 'tickets')});
  }

  // Not a dashboard source: the bootstrap payload's own flag, the same one v1's
  // sidebar badges System Setup with.
  if (ctx.capabilities?.setup === true && ctx.capabilities?.setup_attention === true) {
    add({tone: 'warn', name: 'System setup has unfinished checks',
      copy: 'Some installation checks have not passed yet. The setup journey '
        + 'lists what is left and fixes most of it in place.',
      action: v1Action(ctx, 'Open System Setup', 'setup')});
  }

  return out.sort((left, right) => SEVERITY[left.tone] - SEVERITY[right.tone]);
}

function blockerRow(entry, primary) {
  const action = entry.action;
  return h('div', {class: 'blocker'},
    dot(entry.tone),
    h('div', {class: 'blocker-body'},
      h('strong', {text: entry.name}),
      h('p', {text: entry.copy})),
    action ? h('div', {class: 'blocker-action'},
      h('a', {class: `button compact ${primary ? 'primary' : ''}`.trim(), href: action.href},
        action.label),
      action.external
        ? h('span', {class: 'blocker-note', text: 'opens the current Admin'})
        : null) : null);
}

// ── at a glance ────────────────────────────────────────────────────────────

// Six tiles, each from one section the dashboard endpoint (or the Apps list)
// actually returned. A section that is absent gets no tile; a section that
// answered "you may not read this" gets a muted tile that says so, because
// hiding it would read as "there is nothing there".
function tilesFrom(ctx, report, apps) {
  const sources = report?.sources || {};
  const out = [];
  const restricted = (label, source) => ({
    tone: 'muted', label, value: source?.reason_detail?.iam_action
      ? `Restricted — needs ${source.reason_detail.iam_action}` : 'Restricted',
    action: null,
  });

  if (apps) {
    out.push(appsTile(ctx, apps));
  }

  if (sources.public_api) {
    const source = reported(sources.public_api);
    out.push(source ? apiTile(ctx, source, reported(sources.sanity))
      : restricted('API', sources.public_api));
  }

  if (sources.compute) {
    const source = reported(sources.compute);
    if (!source) out.push(restricted('Nodes', sources.compute));
    else {
      const data = source.data || {};
      out.push({tone: tone(source), label: 'Nodes',
        value: data.total ? `${data.up || 0} of ${data.total} healthy` : 'None running',
        action: v2Action(ctx, 'Nodes', 'infrastructure', 'fleet')});
    }
  }

  if (sources.database) {
    const source = reported(sources.database);
    if (!source) out.push(restricted('Database', sources.database));
    else {
      const data = source.data || {};
      const engine = [data.engine, data.version].filter(Boolean).join(' ');
      const upgrade = data.drift?.available_major;
      out.push({
        tone: upgrade ? 'warn' : tone(source), label: 'Database',
        value: source.status === 'unhealthy' || data.reachable === false ? 'Unreachable'
          : [engine || 'Available', upgrade ? `${upgrade} upgrade available` : 'available']
            .filter(Boolean).join(' · '),
        action: v2Action(ctx, 'Database', 'infrastructure', 'fleet')});
    }
  }

  if (sources.cache) {
    const source = reported(sources.cache);
    if (!source) out.push(restricted('Cache', sources.cache));
    else {
      const data = source.data || {};
      const engine = [data.engine, data.version].filter(Boolean).join(' ');
      out.push({tone: tone(source), label: 'Cache',
        value: source.status === 'unhealthy' || data.reachable === false ? 'Unreachable'
          : [engine || 'Available', 'available', data.memory_used ? `${data.memory_used} used` : '']
            .filter(Boolean).join(' · '),
        action: v2Action(ctx, 'Cache', 'infrastructure', 'fleet')});
    }
  }

  if (sources.certificates) {
    const source = reported(sources.certificates);
    if (!source) out.push(restricted('Certificates', sources.certificates));
    else {
      const data = source.data || {};
      out.push({
        tone: data.failing ? 'danger' : data.total ? tone(source) : 'muted',
        label: 'Certificates',
        value: !data.total ? 'Not managed here'
          : data.failing ? `${data.active || 0} valid · ${data.failing} not valid`
            : `${data.active || data.total} valid`,
        action: v2Action(ctx, 'Certificates', 'domains', 'certificates')});
    }
  }

  return out;
}

function apiTile(ctx, source, sanity) {
  const data = source.data || {};
  const probed = data.probe?.version || '';
  const node = data.node_version || '';
  const label = `API${probed ? ` ${probed}` : ''}`;
  const setup = ctx.capabilities?.setup === true
    ? v1Action(ctx, 'API', 'setup') : null;
  if (source.status === 'unconfigured' || !data.configured) {
    return {tone: 'warn', label: 'API', value: 'No public address set', action: setup};
  }
  if (source.status === 'unhealthy') {
    return {tone: 'danger', label, value: 'the public address did not answer', action: setup};
  }
  const failing = (sanity?.data?.checks || []).filter((row) => !row.ok);
  if (failing.length) {
    return {tone: 'warn', label,
      value: SANITY_COPY[failing[0].name] || failing[0].name, action: setup};
  }
  if (probed && node && probed !== node) {
    return {tone: 'warn', label, value: `this node reports ${node}`, action: setup};
  }
  return {tone: tone(source), label, value: 'responding', action: setup};
}

// The Apps tile reuses v1's own reading of an app summary: an app with no
// address never finished setup, an app with an address but no release is
// serving the built-in welcome page, and a failed or rolled-back deploy is red.
function appsTile(ctx, apps) {
  const items = apps.items || [];
  if (!items.length) {
    return {tone: 'muted', label: 'Apps', value: 'None yet',
      action: v2Action(ctx, 'Apps', 'apps', 'deployments')};
  }
  let live = 0; let incomplete = 0; let broken = 0;
  for (const item of items) {
    const deployment = item.latest_deployment;
    if (!item.address) { incomplete += 1; continue; }
    if (deployment && ['failed', 'rolled_back'].includes(deployment.status)) { broken += 1; continue; }
    if (!item.current_release) { incomplete += 1; continue; }
    live += 1;
  }
  const parts = [`${live} live`];
  if (broken) parts.push(`${broken} ${plural(broken, 'deploy', 'deploys')} failed`);
  if (incomplete) parts.push(`${incomplete} setup incomplete`);
  return {
    tone: broken ? 'danger' : incomplete ? 'warn' : 'ok',
    label: 'Apps', value: parts.join(' · '),
    action: v2Action(ctx, 'Apps', 'apps', 'deployments')};
}

function tileNode(entry) {
  const body = h('div', {},
    h('strong', {text: entry.label}),
    h('small', {}, entry.value,
      entry.action?.external ? h('span', {class: 'leaves-v2', text: ' · opens the current Admin'}) : null));
  if (!entry.action) return h('div', {class: 'tile'}, dot(entry.tone), body);
  return h('a', {class: 'tile', href: entry.action.href}, dot(entry.tone), body, icon('chevron'));
}

// ── running operations ─────────────────────────────────────────────────────

/**
 * The one durable operation any endpoint reports without being handed an
 * operation id first.
 *
 * GAP, deliberately not papered over: capacity and maintenance changes
 * (`/api/aws/capacity/apply`, `/api/aws/maintenance/apply`) return an operation
 * id that only the browser session which started them holds, and
 * `/api/aws/capacity/status` REQUIRES that id — there is no endpoint that
 * enumerates operations in flight. So an AWS operation started in another tab,
 * by another operator, or before this page was opened is invisible here, and
 * Home renders nothing for it rather than guessing. Once the platform grows a
 * "list running operations" read, feed it in beside this and the strip and the
 * global banner both start reporting it with no other change.
 *
 * Platform deployments are different: the dashboard's last_deployment source
 * carries the newest attempt with its status, so a deployment that is still
 * requested/canary/fleet is a running operation the endpoint itself reported.
 *
 * Returns true while the operation is still running.
 */
function syncDeploymentOperation(ctx, report) {
  const item = (report?.sources?.last_deployment?.data?.items || [])[0];
  const running = Boolean(item) && DEPLOY_ACTIVE.has(String(item.status)) && !item.finished;
  if (!running) { removeOperation(DEPLOY_OPERATION_ID); return false; }
  const sha = String(item.sha || '').slice(0, 7);
  upsertOperation({
    id: DEPLOY_OPERATION_ID,
    title: sha ? `Deploying ${sha}` : 'Deploying the platform',
    phase: `the platform reports: ${item.status}`,
    startedAt: Date.parse(item.created) || Date.now(),
    href: v1Href(ctx, 'deployments'),
  });
  return true;
}

function operationsPanel(operations) {
  if (!operations.length) return null;
  return h('section', {class: 'panel accent'},
    ...operations.map((operation) => h('div', {class: 'op-strip'},
      h('span', {class: 'spin', 'aria-hidden': 'true'}),
      h('div', {},
        h('strong', {text: operation.title}),
        h('small', {text: [operation.phase, elapsedText(operation.startedAt),
          'safe to leave this page'].filter(Boolean).join(' · ')})),
      h('span', {class: 'badge accent',
        text: `${operations.length} ${plural(operations.length, 'operation', 'operations')} running`}),
      operation.href
        ? h('a', {class: 'button compact', href: operation.href}, 'Details') : null)));
}

// ── recent activity ────────────────────────────────────────────────────────

// The same endpoints v1's Activity lane reads, under the same capability gates,
// asking for four rows instead of twenty-five. No capability, no panel — an
// empty "Recent activity" heading would read as "nothing has happened".
function activityModel(ctx) {
  if (ctx.features?.activity?.enabled !== true) return null;
  const capabilities = ctx.features.activity.capabilities || {};
  if (capabilities.view_logs === true) {
    return {
      endpoint: '/api/logs', tab: 'logs',
      line: (row) => ({
        text: row.log || [row.method, row.path].filter(Boolean).join(' ')
          || row.kind || `Log ${row.id}`,
        who: row.username || row.ip || '',
      }),
    };
  }
  if (capabilities.view_security === true) {
    return {
      endpoint: '/api/incident/event', tab: 'events',
      line: (row) => ({
        text: row.title || row.category || `Event ${row.id}`,
        who: row.source_ip || row.hostname || '',
      }),
    };
  }
  return null;
}

function activityPanel(ctx, model, state) {
  if (!model) return null;
  // The panel's own tab: the preview reads whichever source this caller can
  // read, so "All activity" opens the full viewer on that same source rather
  // than on a tab they would be refused.
  const all = activityAction(ctx, 'All activity →', model.tab);
  const head = h('div', {class: 'panel-head'},
    h('h2', {text: 'Recent activity'}),
    h('a', {class: 'panel-link', href: all.href},
      all.label, all.external ? h('span', {class: 'leaves-v2', text: ' · current Admin'}) : null));
  if (state.activityError) {
    return h('section', {class: 'panel'}, head,
      h('div', {class: 'panel-body'}, errorState(state.activityError, state.retry)));
  }
  const rows = state.activity || [];
  if (!rows.length) {
    return h('section', {class: 'panel'}, head,
      h('div', {class: 'panel-body'},
        h('p', {text: 'Nothing has been recorded here yet.'})));
  }
  return h('section', {class: 'panel'}, head,
    ...rows.map((row) => {
      const line = model.line(row);
      return h('div', {class: 'activity-row'},
        icon('activity'),
        h('span', {class: 'activity-text', text: line.text}),
        h('span', {class: 'activity-when',
          text: [line.who, agoText(row.created)].filter(Boolean).join(' · ')}));
    }));
}

// ── headline ───────────────────────────────────────────────────────────────

// The one rule this page exists to keep: the headline is the WORST of what the
// sources actually reported, in this order —
//   1. a danger blocker, or availability.state === 'down'   -> danger
//   2. a warning blocker                                    -> warning
//   3. a running operation                                  -> accent
//   4. availability that is neither ok nor down (no evidence)-> muted
//   5. nothing above                                        -> success
// "Everything is running" is reachable ONLY from step 5. Degraded is never
// rounded up to healthy, and an unreadable source is never rounded down to one.
function headlineFor(state, operations) {
  if (!state.report) {
    return {kind: 'unreadable', tone: 'danger', text: 'Status could not be read',
      sub: sentence(state.reportError?.message
        || 'The dashboard source did not answer.')};
  }
  const availability = state.report.availability || {};
  const blockers = state.blockers || [];
  const worst = blockers.length ? blockers[0].tone : null;
  const count = blockers.length;
  if (count && (worst === 'danger' || availability.state === 'down')) {
    return {kind: 'blockers', tone: 'danger',
      text: `${count} ${plural(count, 'thing needs', 'things need')} your attention`};
  }
  if (count) {
    return {kind: 'blockers', tone: 'warn',
      text: `${count} ${plural(count, 'thing needs', 'things need')} your attention`};
  }
  if (availability.state === 'down') {
    return {kind: 'down', tone: 'danger',
      text: availability.message || 'Something customers depend on is down'};
  }
  if (operations.length) {
    return {kind: 'operations', tone: 'accent',
      text: `${operations.length === 1 ? '1 operation is' : `${operations.length} operations are`} running`};
  }
  if (availability.state !== 'ok') {
    return {kind: 'unknown', tone: 'muted',
      text: availability.message || 'Status unknown — no source is reporting'};
  }
  return {kind: 'clear', tone: 'ok', text: 'Everything is running'};
}

function subLineFor(state, headline, operations) {
  if (headline.kind === 'unreadable') return headline.sub;
  const parts = [];
  const availability = state.report?.availability || {};
  if (headline.kind === 'clear') {
    parts.push('Every source this installation reports is healthy.');
  } else if (headline.kind !== 'unknown' && headline.kind !== 'down') {
    parts.push(availability.state === 'ok'
      ? 'Core infrastructure is healthy.'
      : sentence(availability.message));
  }
  const attention = state.report?.attention?.message;
  if (attention && headline.kind !== 'clear') parts.push(sentence(attention));
  if (operations.length && headline.kind !== 'operations') {
    parts.push(operations.length === 1
      ? 'One operation is running.'
      : `${operations.length} operations are running.`);
  }
  return parts.filter(Boolean).join(' ');
}

// The eyebrow names WHERE you are before it says what is wrong there. Both
// halves are facts: the infrastructure mode comes from the bootstrap payload,
// the host from the address bar. The payload carries no environment name, so
// none is invented.
function eyebrowText(ctx) {
  const mode = ctx.infrastructure?.mode
    ? `${ctx.infrastructure.managed === true ? 'Managed' : 'External'} infrastructure`
    : '';
  return [mode, location.hostname].filter(Boolean).join(' · ');
}

// ── page ───────────────────────────────────────────────────────────────────

export async function homePage(ctx, signal = null) {
  const root = h('div', {class: 'page home-page'}, loadingState('Loading Home'));
  // Held across repaints so the operation subscription can update just the
  // strip: a store change must not blow away the panel the operator is reading.
  const opHost = h('div', {});
  const model = activityModel(ctx);
  const wantApps = ctx.features?.webapps?.enabled === true;
  const state = {
    report: null, reportError: null,
    apps: null, appsError: null,
    activity: null, activityError: null,
    blockers: [], retry: null,
  };
  let timer = null;
  let ticks = 0;

  function clearPoll() {
    if (timer) { clearTimeout(timer); timer = null; }
  }

  function paintOperations() {
    const panel = operationsPanel(listOperations());
    opHost.replaceChildren(...(panel ? [panel] : []));
  }

  async function load(refresh = false) {
    clearPoll();
    const reads = [];
    reads.push(api(`/api/account/admin/dashboard${refresh ? '?refresh=1' : ''}`, {signal})
      .then((value) => { state.report = value; state.reportError = null; })
      .catch((error) => {
        if (error?.name === 'AbortError') return;
        state.report = null; state.reportError = error;
      }));
    if (wantApps) {
      reads.push(api('/api/edge/webapp/summaries', {signal})
        .then((value) => { state.apps = value; state.appsError = null; })
        .catch((error) => {
          if (error?.name === 'AbortError') return;
          state.apps = null; state.appsError = error;
        }));
    }
    if (model) {
      reads.push(apiEnvelope(`${model.endpoint}?graph=activity&size=4&sort=-created`, {signal})
        .then((envelope) => { state.activity = envelope.items; state.activityError = null; })
        .catch((error) => {
          if (error?.name === 'AbortError') return;
          state.activity = null; state.activityError = error;
        }));
    }
    await Promise.all(reads);
    if (signal?.aborted) return;
    state.blockers = state.report ? blockersFrom(ctx, state.report) : [];
    // The store is global and outlives this page, so the deployment operation
    // is written to it on every read — including the read that finds it gone.
    const running = syncDeploymentOperation(ctx, state.report);
    paint();
    if (running && ticks < POLL_LIMIT) {
      ticks += 1;
      timer = setTimeout(() => { load().catch(() => {}); }, POLL_INTERVAL);
    }
  }

  state.retry = () => load();

  function header(headline, operations) {
    return h('header', {class: 'page-header'},
      h('div', {},
        h('div', {class: 'eyebrow', text: eyebrowText(ctx)}),
        h('div', {class: 'home-headline'},
          dot(headline.tone, 'large'),
          h('h1', {text: headline.text, tabindex: '-1'})),
        h('p', {class: 'home-sub', text: subLineFor(state, headline, operations)})),
      h('div', {class: 'home-asof'},
        state.report?.observed_at
          ? h('span', {text: `as of ${shortTime(state.report.observed_at)}`}) : null,
        h('button', {class: 'button ghost compact', type: 'button',
          onclick: (event) => runAction(event.currentTarget, () => load(true),
            {announceLabel: 'Refreshing…'})},
        icon('refresh'), 'Refresh')));
  }

  function blockersPanel() {
    const head = h('div', {class: 'panel-head'},
      h('h2', {text: 'Blockers'}),
      ctx.capabilities?.setup === true
        ? h('a', {class: 'panel-link', href: v1Href(ctx, 'setup')}, 'Setup journey →')
        : null);
    if (state.reportError) {
      return h('section', {class: 'panel'}, head,
        h('div', {class: 'panel-body'}, errorState(state.reportError, state.retry)));
    }
    if (!state.blockers.length) {
      return h('section', {class: 'panel'}, head,
        h('div', {class: 'panel-body'},
          h('p', {text: 'Nothing is blocked. Every source that answered is '
            + 'reporting a state you do not have to do anything about.'})));
    }
    return h('section', {class: 'panel'}, head,
      ...state.blockers.map((entry, index) => blockerRow(entry, index === 0)));
  }

  function tilesSection() {
    const tiles = tilesFrom(ctx, state.report, state.apps);
    const nodes = tiles.map(tileNode);
    if (state.appsError) {
      nodes.unshift(h('div', {class: 'tile'}, dot('muted'),
        h('div', {}, h('strong', {text: 'Apps'}),
          h('small', {text: `Could not be read — ${state.appsError.message}`}))));
    }
    if (!nodes.length) return null;
    return h('div', {class: 'tile-grid'}, ...nodes);
  }

  function paint() {
    const operations = listOperations();
    const headline = headlineFor(state, operations);
    paintOperations();
    root.replaceChildren(
      header(headline, operations),
      opHost,
      blockersPanel(),
      tilesSection(),
      activityPanel(ctx, model, state));
  }

  const unsubscribe = subscribeOperations(paintOperations);
  root.dispose = () => { clearPoll(); unsubscribe(); };
  signal?.addEventListener('abort', clearPoll, {once: true});
  await load();
  return root;
}
