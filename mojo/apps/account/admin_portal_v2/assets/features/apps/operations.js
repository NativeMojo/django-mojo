// What the Apps lane tells the global operation store.
//
// The store's rule is that a screen never claims more than an endpoint said, so
// every function here is fed by a real response:
//
//   - a deploy, rollback or promote reports its own `{status, terminal}`
//     payload (`webapp_deploy.payload`), and every later read of
//     `/api/edge/webapp/summary`/`summaries` re-reports the same deployment as
//     `latest_deployment` until it reaches a terminal status;
//   - an onboarding run reports `{status, cursor}` from
//     `/api/edge/webapp/onboarding/detail`, which the wizard already polls.
//
// Nothing here polls on its own. The list page, the detail page and the wizard
// each call in from a read they were already making, and the banner under the
// topbar renders whatever is left in the store.
//
// Scope: WEB-APP operations only. A platform (framework/API service) deploy is
// reported by Home from the dashboard's `last_deployment` source under the id
// 'platform-deployment'; reporting it again from here would put one operation
// on the banner twice under two ids.
import {remove as removeOperation, upsert as upsertOperation} from '../../components/operations.js';

// mojo/apps/edge/models/web_app_deployment.py ACTIVE_STATUSES.
const ACTIVE_DEPLOY_STATUSES = new Set(['queued', 'deploying', 'rolling_back']);
// Onboarding is running while the server is reconciling, or while a scheduled
// retry (certificate issuance, DNS propagation) is pending. A run parked on a
// question is NOT running — it is waiting for the person, and the wizard is
// where that gets answered.
const ONBOARDING_ID_PREFIX = 'webapp-onboarding:';

function deployOperationId(appId) { return `webapp-deploy:${appId}`; }

function appName(app) {
  return app?.display_name || app?.slug || `#${app?.id}`;
}

function appHref(appId, tab = 'deploys') {
  return `#/apps?tab=${encodeURIComponent(tab)}&webapp=${encodeURIComponent(appId)}`;
}

/** Forget everything this lane is reporting about one app. */
export function removeAppOperation(appId) {
  removeOperation(deployOperationId(appId));
}

/**
 * Report (or clear) an app's deploy from one deployment record.
 *
 * `deployment` is either a `webapp_deploy.payload` response or a summary row's
 * `latest_deployment` — both carry {id, status, created}. A terminal or absent
 * deployment removes the operation rather than leaving a finished one on the
 * banner.
 *
 * Returns true while the deploy is still running.
 */
export function syncAppDeployment(app, deployment, {title = ''} = {}) {
  const appId = app?.id;
  if (appId == null) return false;
  const status = String(deployment?.status || '');
  // `finished` is belt and braces: a status this build does not know about
  // must not keep an operation on the banner forever.
  const running = Boolean(deployment) && ACTIVE_DEPLOY_STATUSES.has(status)
    && !deployment.finished;
  if (!running) { removeAppOperation(appId); return false; }
  const version = deployment.version || deployment.release?.version || '';
  upsertOperation({
    id: deployOperationId(appId),
    title: title || (version
      ? `Deploying ${version} to ${appName(app)}`
      : `Deploying ${appName(app)}`),
    phase: `the fleet reports: ${status.replaceAll('_', ' ')}`,
    startedAt: Date.parse(deployment.created) || Date.now(),
    href: appHref(appId),
  });
  return true;
}

/**
 * The list page's whole-fleet sweep: report every app whose newest deployment
 * is running, and clear every app whose newest one is not.
 *
 * Both halves matter. Reporting only the running ones would leave a finished
 * deploy on the banner until something else happened to clear it.
 */
export function syncAppOperations(items) {
  let running = 0;
  for (const item of items || []) {
    const app = item?.webapp;
    if (!app || app.id == null) continue;
    if (syncAppDeployment(app, item.latest_deployment)) running += 1;
  }
  return running;
}

/**
 * Report an onboarding run.
 *
 * Keyed on the operation id, not the app: an onboarding run does not have an
 * app yet — creating one is what it is doing. The wizard drives this from the
 * same `detail` poll it already runs, and clears it when the run finishes,
 * fails, or parks on a question only a person can answer.
 */
export function syncOnboardingOperation(operation, {displayName = ''} = {}) {
  const operationId = operation?.operation_id;
  if (!operationId) return false;
  const id = `${ONBOARDING_ID_PREFIX}${operationId}`;
  const status = String(operation.status || '');
  const running = status === 'active'
    || (status === 'waiting' && Boolean(operation.next_attempt_at));
  if (!running) { removeOperation(id); return false; }
  const name = displayName || operation.profile?.display_name || 'a new app';
  const cursor = String(operation.cursor || '').replaceAll('_', ' ');
  upsertOperation({
    id,
    title: `Setting up ${name}`,
    phase: cursor ? `setup reports: ${cursor}` : '',
    startedAt: Date.parse(operation.created) || Date.now(),
    // The run lives in the wizard, which opens from the Apps list.
    href: '#/apps',
  });
  return true;
}

/** Forget an onboarding run — it finished, failed, or was cancelled. */
export function removeOnboardingOperation(operationId) {
  if (!operationId) return;
  removeOperation(`${ONBOARDING_ID_PREFIX}${operationId}`);
}
