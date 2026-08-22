// Apps — everything this installation serves, in one list.
//
// v1 called this lane Deployments and merged two tiers into it: the platform
// software (the API service and the django-mojo build every node runs) and the
// web apps. That merge is the whole point of the page — one place that answers
// "what is running, and at what version" — so it is ported whole. What v2
// changes is the name, the chrome, and the route: `#/apps`, with `#/deployments`
// kept as an alias so v1 links and bookmarks still land here.
//
// `?webapp=<id>` is a page of its own (detail.js), not a drawer over this list.
import {api, formatDate, h, icon, pageHeader} from '../../core.js';
import {decodeRouteState, routeHref} from '../../components/routes.js';
import {rowSection, statusHeadline, statusRow} from '../../components/rows.js';
import {emptyState, errorState} from '../../components/views.js';
import {runAction} from '../../components/actions.js';
import {
  apiDeployFailure, apiServiceRow, applyFrameworkUpdate, FRAMEWORK_PATH, frameworkRow,
  openDeployHistory, openFrameworkDetail, PLATFORM_SECTIONS_PATH, retryApiDeploy,
} from './api.js';
import {webappDetailPage} from './detail.js';
import {syncAppOperations} from './operations.js';
import {certState, changeAddressFor, deleteWebApp} from './shared.js';
import {hasPendingWizard, resumeWizard, startWizard} from './wizard.js';

// How the live build arrived, in the operator's words. 'source not recorded' is
// the honest answer for a release registered before the platform started
// recording this, never a guess at which way it came.
const SOURCE_LABEL = {
  github: 'via GitHub push',
  api: 'via CLI or API',
  upload: 'via upload',
  unknown: 'source not recorded',
};

// An in-flight deploy is exactly when the operator is staring at this page; it
// must catch the outcome without a hand refresh.
const POLL_STATUSES = new Set(['requested', 'canary', 'fleet', 'verified', 'partial']);

// v1's resume banner, in v2's panel vocabulary (`panel-head`, not v1's
// `panel-heading`). Shown only while sessionStorage holds a submitted run, so
// an abandoned wizard is never lost behind a page reload.
function resumeBanner(ctx, reloadApps) {
  return h('section', {class: 'panel accent resume-banner'}, h('div', {class: 'panel-head'},
    h('div', {}, h('h2', {text: 'You have a setup in progress'}),
      h('p', {text: 'Pick up where you left off, or start over.'})),
    h('button', {class: 'button primary', onclick: () => resumeWizard(ctx, reloadApps)}, 'Resume setup')));
}

// One row per app, keyed on what the summary proves: no address means setup
// never finished, an address with no release is serving the welcome page it
// came with, and a failed or rolled-back deploy is red whatever the release
// says.
function webappRow(ctx, item, {reload}) {
  const app = item.webapp || {};
  const name = app.display_name || app.slug || `#${app.id}`;
  const address = item.address;
  const release = item.current_release;
  const deployment = item.latest_deployment;
  const manage = ctx.capabilities.manage_webapps;
  // Every row opens the app's own page — Overview offers "Set address"
  // directly, and the Danger tab carries the destructive half.
  const openHref = routeHref('apps', {webapp: app.id});
  if (!address) {
    // Setup was abandoned before the app got an address. Say so plainly and
    // keep both ways out inline: finish it, or delete it.
    return statusRow({tone: 'warn', name,
      value: 'Setup never finished — not reachable',
      detailNode: manage ? h('span', {class: 'row-inline-actions'},
        h('button', {class: 'button ghost compact', type: 'button', onclick: (event) => runAction(event.currentTarget,
          () => changeAddressFor(ctx, app, reload), {pendingLabel: 'Opening…'})}, 'Finish setup'),
        h('button', {class: 'button ghost compact danger-text', type: 'button',
          onclick: () => deleteWebApp(app, reload)}, 'Delete')) : null,
      action: {label: 'Open', href: openHref}});
  }
  if (!release) {
    // The address serves the built-in welcome page until a first deploy lands.
    return statusRow({tone: 'warn', name,
      value: `${address.hostname} · live with a welcome page — nothing deployed yet`,
      detailNode: h('a', {class: 'row-link', href: routeHref('apps', {webapp: app.id, tab: 'setup'})}, 'Deploy something'),
      action: {label: 'Open', href: openHref}});
  }
  const ssl = certState(address.certificate);
  const deployedAt = formatDate(deployment?.finished || release.created);
  const arrival = SOURCE_LABEL[release.source] || SOURCE_LABEL.unknown;
  let tone = ssl.tone;
  let value = `${address.hostname} · ${ssl.label} · deployed ${deployedAt} · ${arrival}`;
  // `rolled_back` is the NORMAL terminal state of a failed deploy — the fleet
  // was put back. Reading only `failed` would render a cheerful "deployed
  // <date>" over exactly the outcome the operator most needs to see.
  if (deployment && (deployment.status === 'failed' || deployment.status === 'rolled_back')) {
    tone = 'danger';
    value = `${address.hostname} · ${ssl.label} · last deploy failed ${formatDate(deployment.created)}`;
  }
  const version = release.version || String(release.id);
  return statusRow({tone, name, value,
    detailNode: h('span', {class: 'row-detail mono', text: String(version).slice(0, 10)}),
    action: {label: 'Open', href: openHref}});
}

/**
 * The Apps destination.
 *
 * Three pages behind one route: the list, the app detail (`?webapp=<id>`), and
 * v1's `#/deployments`, which canonicalizes here with its query state intact.
 */
export async function appsPage(ctx, route = 'apps', navigate = null, signal = null) {
  // replaceState fires no hashchange, so this render is the only one.
  if (route === 'deployments') {
    history.replaceState({}, '', routeHref('apps', decodeRouteState().state));
  }
  const wantPlatform = ctx.features?.platform?.capabilities?.view === true;
  const wantApps = ctx.features?.webapps?.enabled === true;
  // Legacy `?inspector=<id>` deep links for apps (WebApp pks are ints; platform
  // deployment pks are UUIDs) redirect to the app's own page.
  const linkedState = decodeRouteState().state;
  const linkedWebapp = linkedState.webapp
    || (/^\d+$/.test(String(linkedState.inspector || '')) ? linkedState.inspector : null);
  if (wantApps && linkedWebapp) {
    if (!linkedState.webapp) {
      history.replaceState({}, '', routeHref('apps', {
        webapp: linkedWebapp, tab: linkedState.tab, return: linkedState.return}));
    }
    return webappDetailPage(ctx, linkedWebapp, signal);
  }
  const root = h('div', {class: 'page apps-page'});
  const state = {
    report: null, reportError: null,
    framework: null,
    apps: null, appsError: null,
    observedAt: null,
  };
  let linkedRecordOpened = false;
  // Bounded auto-refresh while a deploy is in flight: 10s ticks, capped,
  // hash-guarded, abort-cleared.
  let pollTicks = 0;
  let pollTimer = null;
  const clearPoll = () => { if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; } };
  signal?.addEventListener('abort', clearPoll);

  async function load(refresh = false) {
    clearPoll();
    const reads = [];
    if (wantPlatform) {
      reads.push(api(PLATFORM_SECTIONS_PATH, {signal})
        .then((value) => { state.report = value; state.reportError = null; })
        .catch((error) => { if (error?.name !== 'AbortError') { state.report = null; state.reportError = error; } }));
      reads.push(api(`${FRAMEWORK_PATH}${refresh ? '?refresh=1' : ''}`, {signal})
        .then((value) => { state.framework = value; })
        .catch(() => { state.framework = null; }));
    }
    if (wantApps) {
      reads.push(api('/api/edge/webapp/summaries', {signal})
        .then((value) => { state.apps = value; state.appsError = null; })
        .catch((error) => { if (error?.name !== 'AbortError') { state.apps = null; state.appsError = error; } }));
    }
    await Promise.all(reads);
    // The framework GET carries no `resolved`; the deployments section's pin
    // block does. Merge it so the held row can say what it resolves to.
    if (state.framework && state.report) {
      state.framework.resolved_pin =
        state.report.sections?.deployments?.data?.framework_pin?.resolved || null;
    }
    // Every summary row re-reports its newest deployment until that deployment
    // is terminal, so this read is also what the global banner is fed from —
    // and what clears it.
    syncAppOperations(state.apps?.items);
    state.observedAt = new Date().toISOString();
    paint();
  }

  // Every callback that finishes work re-renders THROUGH a refetch — the
  // wizard, drill-in and row callbacks all rely on it.
  const render = () => load();

  function deploymentsSection() {
    return state.report?.sections?.deployments || null;
  }

  function apiSection() {
    return state.report?.sections?.api || null;
  }

  function schedulePoll() {
    const data = deploymentsSection()?.data || null;
    const active = Boolean(data?.coordination?.state)
      || POLL_STATUSES.has((data?.items || [])[0]?.status);
    if (!active || pollTicks >= 36 || !location.hash.startsWith('#/apps')) return;
    pollTicks += 1;
    pollTimer = setTimeout(() => load().catch(() => {}), 10000);
  }

  function apiRows() {
    if (!wantPlatform || state.reportError) return [];
    return [
      apiServiceRow(ctx, deploymentsSection(), {
        onOpen: () => openDeployHistory(ctx, deploymentsSection(), render, {apiSection: apiSection()}),
      }),
      frameworkRow(ctx, state.framework, {
        onOpen: () => openFrameworkDetail(ctx, state.framework, render),
        onUpdate: () => applyFrameworkUpdate(ctx, state.framework, render),
      }),
    ].filter(Boolean);
  }

  function appRows() {
    const items = state.apps?.items || [];
    return items.map((item) => webappRow(ctx, item, {reload: render}));
  }

  // WHICH thing is unhappy, in priority order — most specific evidence first.
  //
  // 1. The API service's own failed deploy: real node counts and a retry.
  // 2. An app whose latest deploy failed: named by the payload.
  // 3/4. Anything else red, then anything amber — an expired certificate is not
  //      a deploy failure, and its row already says so precisely. These two read
  //      the row's OWN name and value, so the banner can never contradict the
  //      row it is pointing at.
  function failureDescriptor(rowNodes) {
    const failure = apiDeployFailure(deploymentsSection());
    if (failure) return {kind: 'api', ...failure};
    const items = state.apps?.items || [];
    const broken = items.find((item) => ['failed', 'rolled_back']
      .includes(item.latest_deployment?.status));
    if (broken) {
      const app = broken.webapp || {};
      return {
        kind: 'webapp',
        name: app.display_name || app.slug || `#${app.id}`,
        id: app.id,
        status: broken.latest_deployment.status,
        release: broken.current_release || null,
      };
    }
    const pick = (tone) => rowNodes.find((row) => row.dataset?.tone === tone);
    const node = pick('danger') || pick('warn');
    if (!node) return null;
    return {
      kind: 'row',
      tone: node.dataset.tone,
      name: node.querySelector('.row-name')?.textContent || 'Something',
      value: node.querySelector('.row-value')?.textContent || '',
    };
  }

  // "…and everything else is fine" is the second thing an operator wants to
  // know, and the only honest way to say it is to count the rest.
  function othersClause(rowNodes) {
    const unhappy = rowNodes.filter(
      (row) => row.dataset?.tone === 'danger' || row.dataset?.tone === 'warn');
    const others = Math.max(unhappy.length - 1, 0);
    if (!others) return 'everything else is healthy';
    return `${others} other thing${others === 1 ? '' : 's'} need${others === 1 ? 's' : ''} a look`;
  }

  function servingSub(serving) {
    if (!serving?.sha) return 'No converged deployment on record — nothing is proven to be serving.';
    return `Still serving ${String(serving.sha).slice(0, 10)}`
      + ` · django-mojo ${serving.framework_version || 'unknown'}`
      + ` · converged ${formatDate(serving.converged_at)}`;
  }

  // The headline's own actions. Built here, not in rows.js: they are wired to
  // this page's reload, and statusHeadline only ever renders nodes.
  function bannerAction(label, run) {
    const button = h('button', {class: 'button compact', type: 'button'}, label);
    button.addEventListener('click', () => runAction(null, () => run(), {key: button}));
    return button;
  }

  function headline(apiRowNodes, appRowNodes) {
    const rowNodes = [...apiRowNodes, ...appRowNodes];
    const tones = rowNodes.map((row) => row.dataset?.tone).filter(Boolean);
    let tone = 'ok';
    let message = 'Everything running is current';
    let sub = '';
    let actions = [];
    if (!tones.length) {
      return statusHeadline({tone: 'muted', message: 'Nothing is deployed yet',
        observedAt: state.observedAt, onRefresh: () => load(true)});
    }
    const failure = failureDescriptor(rowNodes);
    if (failure?.kind === 'api') {
      const {build, proven, expected} = failure;
      const manage = ctx.features?.platform?.capabilities?.manage === true;
      tone = 'danger';
      message = `The API service failed to deploy — build ${build} `
        + `${proven === 0 ? 'never reached the fleet' : `reached ${proven} of ${expected} nodes`}`
        + ` · ${proven} of ${expected} nodes updated · ${othersClause(rowNodes)}`;
      sub = servingSub(failure.serving);
      actions = [
        bannerAction('See what failed',
          () => openDeployHistory(ctx, deploymentsSection(), render, {apiSection: apiSection()})),
        // The retry is the platform's own deploy/retry verb, with the same
        // capability gate the drill-in applies — surfaced, not reimplemented.
        manage ? bannerAction('Retry same SHA',
          () => retryApiDeploy(failure.deploymentId, render)) : null,
      ].filter(Boolean);
    } else if (failure?.kind === 'webapp') {
      const serving = failure.release?.version || failure.release?.id;
      tone = 'danger';
      // No node counts here. A web-app deployment records {runner, job} targets
      // — not a fleet roster anyone measured — so "2 of 3" built from those
      // would be a number nobody actually took.
      message = failure.status === 'rolled_back'
        ? `${failure.name} failed to deploy — the fleet was put back on ${serving || 'the previous version'}`
        : `${failure.name} failed to deploy — and the rollback did not finish, so the fleet may be mixed`;
      sub = serving
        ? `Still serving ${serving} · ${othersClause(rowNodes)}`
        : `Nothing is recorded as serving · ${othersClause(rowNodes)}`;
      // Rolling back is the only forward path here, and it needs fresh auth and
      // a written reason — neither of which can ride a banner button. It already
      // lives one click away, on the tab this link opens.
      actions = [h('a', {class: 'button compact',
        href: routeHref('apps', {webapp: failure.id, tab: 'deploys'})}, 'See what failed')];
    } else if (failure?.kind === 'row') {
      tone = failure.tone;
      message = `${failure.name} needs attention — ${failure.value}`;
    }
    return statusHeadline({tone, message, sub, actions,
      observedAt: state.observedAt,
      onRefresh: () => load(true)});
  }

  // One sentence about the apps as a whole: how many are live, where they
  // answer, and what secures them. Every value is server-scoped to the rows in
  // this response — see the endpoint's `_fleet`.
  function appsSubhead() {
    if (state.apps?.truncated === true) {
      // A truncated list cannot claim to describe the fleet's domains or its
      // certificates — it is not looking at all of them. What it CAN say is
      // that it is showing a slice.
      return `Showing the first ${state.apps.limit} apps by name — the fleet has more.`;
    }
    const fleet = state.apps?.fleet;
    if (!fleet) return '';
    const parts = [`${fleet.live} live`];
    const domains = fleet.domains || [];
    if (domains.length === 1) {
      parts.push(fleet.certificate?.wildcard ? `on *.${domains[0]}` : `on ${domains[0]}`);
    } else if (domains.length > 1) {
      parts.push(`across ${domains.length} domains`);
    }
    if (fleet.certificate) {
      const renews = fleet.certificate.renew_after || fleet.certificate.not_after;
      parts.push(`one ${fleet.certificate.wildcard ? 'wildcard ' : ''}certificate`
        + (renews ? `, renews ${formatDate(renews)}` : ''));
    } else if (fleet.certificate_count > 1) {
      parts.push(`${fleet.certificate_count} certificates`);
    }
    return parts.join(' · ');
  }

  function paint() {
    const apiRowNodes = apiRows();
    const appRowNodes = appRows();
    const children = [
      pageHeader('Apps', 'Everything you serve',
        'Each app carries its address, version, deploys, routes and certificate in one place.', [
          ctx.capabilities.manage_webapps
            ? h('button', {class: 'button primary', onclick: () => startWizard(ctx, render)},
              icon('plus'), 'New app') : null,
        ].filter(Boolean)),
      hasPendingWizard() ? resumeBanner(ctx, render) : null,
      headline(apiRowNodes, appRowNodes),
      // Sections fail independently: a platform outage never hides the apps,
      // and vice versa.
      state.reportError ? h('section', {class: 'row-section'},
        h('h2', {class: 'row-section-label', text: 'Platform software'}),
        errorState(state.reportError, () => load())) : rowSection('Platform software', apiRowNodes),
      state.appsError ? h('section', {class: 'row-section'},
        h('h2', {class: 'row-section-label', text: 'Your apps'}),
        errorState(state.appsError, () => load())) : null,
      wantApps && !state.appsError && state.apps && !appRowNodes.length
        ? h('section', {class: 'row-section'},
          h('h2', {class: 'row-section-label', text: 'Your apps'}),
          emptyState('No apps yet', 'Choose “New app” to put your first one online.'))
        : rowSection('Your apps', appRowNodes, {sub: appsSubhead()}),
    ].filter(Boolean);
    root.replaceChildren(h('div', {class: 'row-page apps-body'}, ...children));
    openLinkedDeployHistory();
    schedulePoll();
  }

  // Legacy platform deep links: PlatformDeployment pks are UUIDs, so a
  // non-numeric `inspector` (or the reserved `deployment` key) opens the API
  // deploy-history modal. App links were dispatched to their own page above.
  //
  // Closing it drops the key that opened it, so what stays in the address bar
  // is the page the operator is actually looking at. Nothing else in the query
  // is touched, and replaceState fires no hashchange, so nothing re-renders.
  //
  // Guarded on the address still naming Apps: leaving the destination closes
  // every overlay, and a rewrite from a closing modal would otherwise stamp
  // the route the operator just left over the one they just asked for.
  function clearLinkedDeployKey() {
    const current = decodeRouteState();
    if (current.route !== 'apps') return;
    if (!current.state.deployment && !current.state.inspector) return;
    history.replaceState({}, '', routeHref('apps', {
      ...current.state, deployment: '', inspector: ''}));
  }

  function openLinkedDeployHistory() {
    if (linkedRecordOpened) return;
    const routeState = decodeRouteState().state;
    const deployKey = routeState.deployment
      || (routeState.inspector && !/^\d+$/.test(String(routeState.inspector)) ? routeState.inspector : null);
    if (deployKey && state.report) {
      linkedRecordOpened = true;
      openDeployHistory(ctx, deploymentsSection(), render,
        {apiSection: apiSection(), onClose: clearLinkedDeployKey});
    }
  }

  await load();
  root.dispose = clearPoll;
  return root;
}
