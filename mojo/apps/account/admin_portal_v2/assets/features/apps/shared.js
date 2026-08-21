// The pieces more than one Apps screen needs: the certificate reading the list
// row and the detail header both use, the destructive flows the list and the
// Danger tab share, and the deploy-key lifecycle the key tab and the setup tab
// both offer.
//
// Ported from v1's webapps/page.js. It lived there because there was one file;
// here the list, the detail page and the setup tab are three, and two copies of
// "what does SSL pending mean" would be two different answers.
import {api, badge, formatDate, h, icon} from '../../core.js';
import {confirmAction, openModal} from '../../components/overlays.js';
import {announce, copyButton, runAction} from '../../components/actions.js';
import {startChangeAddress} from './wizard.js';
import {removeAppOperation, syncAppDeployment} from './operations.js';

/** A link into the current Admin, for the screens v2 has not built. */
export function v1Href(ctx, route) {
  return `${ctx.admin_path || '/admin/'}#/${route}`;
}

export function detailGrid(rows) {
  return h('dl', {class: 'detail-grid'}, ...rows.filter(Boolean).flatMap(([label, value]) => [
    h('dt', {text: label}), h('dd', {}, value instanceof Node ? value : String(value ?? '—')),
  ]));
}

// Only ever open the app's own origin as a real https link. The value is
// backend-built, but asserting the scheme keeps a stray javascript:/data: URL
// out of an href or window.open even if the source ever changed.
export function httpsLink(origin) {
  return typeof origin === 'string' && origin.startsWith('https://') ? origin : null;
}

// A destructive action fails with no panel of its own to fail into: the view it
// belonged to is gone, or about to be. Say so where the operator is looking
// instead of letting the scrim vanish onto an unchanged screen.
export function actionFailed(title, error) {
  const detail = error?.message || 'That did not work.';
  announce(detail);
  openModal({title, content: h('div', {class: 'callout warning'}, icon('alert'), h('p', {text: detail}))});
}

// The address is the health story. SSL states map to plain words; a row is
// green only when the app has an address, a live release, and a valid cert.
export function certState(certificate) {
  if (!certificate) return {label: 'SSL not issued yet', tone: 'warn'};
  const expired = certificate.not_after && new Date(certificate.not_after) < new Date();
  if (certificate.status === 'active' && !expired) return {label: 'SSL valid', tone: 'ok'};
  if (certificate.status === 'active') return {label: 'SSL expired', tone: 'danger'};
  if (['pending', 'issuing'].includes(certificate.status)) return {label: 'SSL issuing', tone: 'warn'};
  return {label: `SSL ${certificate.status}`, tone: 'danger'};
}

export function certBadge(certificate) {
  const state = certState(certificate);
  return badge(state.label, state.tone === 'ok' ? 'success' : state.tone === 'warn' ? 'warning' : 'danger');
}

// ---------------------------------------------------------------------------
// the deploy key
// ---------------------------------------------------------------------------

// The deploy key is shown once. When the dialog closes we scrub the node, the
// closure, and the response so the value cannot be read back from memory.
function oneTimeSecret(webapp, result, returnFocus) {
  let secret = result.token;
  const secretField = h('pre', {class: 'code-block is-inline', tabindex: '0', text: secret});
  const content = h('div', {},
    h('div', {class: 'callout warning'}, icon('alert'), h('div', {}, h('strong', {text: 'Copy this value now'}),
      h('p', {text: 'It can’t be shown again after you close this. If it’s lost, rotate the key to get a new one.'}))),
    // A div, not a label: a <label> with no labelable control inside labels
    // nothing. tabindex on the block keeps the value Tab-reachable, which the
    // textarea gave for free and a keyboard user needs to select and copy it.
    h('div', {class: 'field'}, h('span', {text: 'GitHub Actions secret: MOJO_DEPLOY_KEY'}), secretField),
    // A function, not a string: onClose scrubs `secret`, and the button must
    // never be able to hand back a value the dialog has already forgotten.
    copyButton(() => secret, {label: 'Copy secret', className: 'button primary'}),
    h('div', {class: 'command'}, h('code', {text: 'gh secret set MOJO_DEPLOY_KEY --repo YOUR_ORG/YOUR_REPO'})));
  openModal({title: `${webapp.slug} deployment key`, subtitle: 'The previous key is already inactive.', content, returnFocus, onClose: () => {
    secretField.textContent = ''; secret = ''; result.token = null;
  }});
}

export function keyDialog(webapp, reload) {
  return (async () => {
    const payload = await api(`/api/edge/webapp/key_status?webapp=${encodeURIComponent(webapp.id)}`);
    const status = payload.status;
    const action = status.linked ? 'rotate' : 'mint';
    const actionLabel = status.linked ? 'Rotate key' : status.last_action === 'revoke' ? 'Create new key' : 'Create key';
    const message = h('div', {class: 'form-message', role: 'alert'});
    const submit = h('button', {class: `button ${status.linked ? 'danger' : 'primary'}`}, icon('key'), actionLabel);
    const content = h('div', {},
      h('p', {class: 'modal-copy', text: status.linked
        ? 'Rotating turns the current key off right away. Update the secret in GitHub before your next deploy, or it will fail.'
        : 'This creates the one secret your deploy needs. It’s shown once — copy it into GitHub.'}),
      status.linked ? detailGrid([['Created', formatDate(status.created)], ['Last used', formatDate(status.last_used)]]) : null,
      message, h('div', {class: 'form-actions'},
        status.linked ? h('button', {class: 'button ghost', onclick: () => revokeKey(webapp, reload)}, 'Turn key off') : null, submit));
    const close = openModal({title: `${webapp.slug} deploy key`, subtitle: 'Shown once, and auditable.', content, danger: status.linked});
    // Success closes this modal, so nothing is restored onto a detached button;
    // a refusal keeps the dialog open with the server's sentence in `message`.
    submit.addEventListener('click', () => runAction(submit, async () => {
      const result = await api('/api/edge/webapp/link_key', {method: 'POST', body: JSON.stringify({webapp: webapp.id, action, operation_id: crypto.randomUUID()})});
      close(); await reload();
      if (result.token) oneTimeSecret(webapp, result);
      else openModal({title: `${webapp.slug} secret unavailable`, subtitle: 'The key was created but its value was already consumed.',
        content: h('div', {class: 'callout warning'}, icon('alert'), h('p', {text: 'The one-time value couldn’t be recovered. Rotate the key to receive a fresh secret.'}))});
    }, {
      pendingLabel: `${actionLabel}…`, restoreOnSuccess: false,
      onError: (error) => { message.textContent = error.message; },
    }));
  })();
}

// Confirm first — the dialog is human input, and nothing paints while a person
// is reading it. What follows takes the key out from under every running
// deploy, so it gets the scrim rather than an inline state on a control the
// reload is about to rebuild.
export function revokeKey(webapp, reload) {
  return confirmAction({title: `Turn off ${webapp.slug}’s deploy key?`, danger: true, confirmLabel: 'Turn key off',
    copy: 'Deploys will fail until you create a new key. Existing releases stay live.'}).then((answer) => {
    if (!answer.confirmed) return undefined;
    return runAction(null, async () => {
      await api('/api/edge/webapp/revoke_key', {method: 'POST', body: JSON.stringify({webapp: webapp.id, operation_id: crypto.randomUUID()})});
      await reload();
    }, {
      key: `webapp-revoke-key:${webapp.id}`,
      busy: {title: 'Turning the deploy key off…', detail: 'Deploys will fail until a new key is created.'},
      onError: (error) => actionFailed('The key was not turned off', error),
    });
  });
}

// ---------------------------------------------------------------------------
// destructive + release flows shared by the list rows and the Danger tab
// ---------------------------------------------------------------------------

// The one delete flow, shared by the app page's Danger tab and the list row's
// inline Delete for an app whose setup never finished.
export function deleteWebApp(app, onDeleted) {
  return confirmAction({title: `Delete ${app.slug}?`, danger: true, confirmLabel: 'Delete app', requireReason: true, reasonLabel: 'Why?',
    copy: 'This removes the app, its address, its deploy key, and its deploy history for good. This cannot be undone.'}).then((answer) => {
    if (!answer.confirmed) return undefined;
    // Permanent, and it navigates away from the page the trigger lives on.
    return runAction(null, async () => {
      await api(`/api/edge/webapp/${encodeURIComponent(app.id)}`, {method: 'DELETE'});
      // An app that no longer exists cannot have an operation running on it.
      removeAppOperation(app.id);
      await onDeleted();
    }, {
      key: `webapp-delete:${app.id}`,
      busy: {title: `Deleting ${app.slug}…`, detail: 'Removing the app, its address, and its deploy history.'},
      onError: (error) => actionFailed('The app was not deleted', error),
    });
  });
}

/**
 * Make an earlier release live again.
 *
 * One endpoint, two truthful namings. A release that was live before is being
 * ROLLED BACK to; one that was uploaded and never served is being PROMOTED.
 * v1 called both "Roll back to this", which read as undoing something that had
 * never happened. The call, the confirm shape, the audited reason and the
 * capability gate are all v1's, unchanged.
 */
export function rollbackTo(app, release, reload, {promote = false} = {}) {
  const version = release.version || String(release.id);
  const title = promote ? `Make ${version} live?` : `Roll back to ${version}?`;
  const copy = promote
    ? `This makes ${version} live across your fleet. Visitors will see it within a few minutes, and the version serving now stays stored.`
    : `This makes ${version} live again across your fleet. Visitors will see that version within a few minutes, and the version serving now stays stored.`;
  return confirmAction({title, danger: true,
    confirmLabel: promote ? 'Make it live' : 'Roll back', requireReason: true,
    reasonLabel: promote ? 'Why are you promoting this?' : 'Why are you rolling back?',
    copy}).then((answer) => {
    if (!answer.confirmed) return undefined;
    // A fleet-wide change must not be interrupted, and the row holding the
    // button is rebuilt by reload().
    return runAction(null, async () => {
      const deployment = await api('/api/edge/webapp/rollback', {method: 'POST',
        body: JSON.stringify({webapp: app.id, release: release.id})});
      // The response IS the endpoint's report of the deployment it started —
      // status and all. Feed the banner from that, never from the fact that the
      // call returned; every later summary read re-reports the same deployment
      // and clears it when it lands.
      syncAppDeployment(app, deployment, {
        title: promote
          ? `Making ${version} live on ${app.display_name || app.slug}`
          : `Rolling ${app.display_name || app.slug} back to ${version}`,
      });
      await reload();
    }, {
      // Two releases are two actions: keying on the app alone made a rollback
      // to 1.2 return an in-flight rollback to 1.1 and never run.
      key: `webapps:rollback:${app.id}:${release.id}`,
      busy: {title: promote ? `Making ${version} live…` : `Rolling back to ${version}…`,
        detail: 'Visitors will see that version within a few minutes.'},
      onError: (error) => actionFailed(
        promote ? 'The release was not promoted' : 'The rollback did not start', error),
    });
  });
}

// The row-level "Set address", Overview's own offer, and the Serving tab all
// run the same wizard flow, seeded from the full record.
export async function changeAddressFor(ctx, app, reload) {
  const full = await api(`/api/edge/webapp/${encodeURIComponent(app.id)}`);
  startChangeAddress(ctx, reload, {
    group_id: full.group?.id, slug: full.slug, display_name: full.display_name,
    environment: full.environment, bucket: full.bucket, github_repository: full.github_repository,
    deployment_ref: full.deployment_ref, build_output: full.build_output,
  });
}
