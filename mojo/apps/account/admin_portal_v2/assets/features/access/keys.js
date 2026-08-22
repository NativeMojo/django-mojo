// Access ▸ Keys — every group API key, under the group it acts as.
//
// Ian's rule, and the model's: a group API key is part of a Group, not an
// independent object. So this tab does not invent a flat credential list — it
// reads the SAME endpoint v1's group inspector reads (`/api/group/apikey`,
// without the per-group filter) and puts each key under its owning group, with
// the permissions it carries and when it was last used.
//
// Deploy keys are the other kind of credential in this installation, and they
// belong to an app. They are NOT listed here: duplicating them would put two
// screens in charge of one credential. One line points at the app that owns
// them instead.
//
// The three key actions are v1's, unchanged — same endpoint, same confirm, same
// one-time reveal — and they are exported so the group inspector's own API Keys
// section stays one implementation rather than two.

import {apiEnvelope, badge, FormView, formatDate, h, icon} from '../../core.js';
import {loadInto, runAction} from '../../components/actions.js';
import {confirmAction, openModal} from '../../components/overlays.js';
import {routeHref} from '../../components/routes.js';
import {capabilities, oneTimeSecret, post} from './shared.js';

const ACTION_URL = '/api/account/admin/apikey/action';

// Whether v2's Apps destination is open to this caller. Same predicate as
// features/apps/feature.js, stated rather than imported for one boolean.
function appsEnabled(ctx) {
  return ctx.features?.webapps?.enabled === true
    || ctx.features?.platform?.capabilities?.view === true;
}

export function createApiKey(group, reload) {
  const form = new FormView({fields: [{name: 'name', label: 'Key name', required: true}],
    submitLabel: 'Create API key', onSubmit: async (values) => {
      const result = await post('/api/group/apikey',
        {group: group.id, name: values.name, permissions: {}});
      close();
      oneTimeSecret('New API key', 'API key token', result.token);
      await reload();
    }});
  const close = openModal({title: `New API key for ${group.name}`, content: form.render()});
}

// † Every one of these three actions ends in reload(), which rebuilds the block
// the button sits in, so there is no node to pin a pending state to. They are
// credential operations, so they run behind the busy scrim, keyed per
// key-and-action so one row's Rotate can never return another's promise.
function keyAction(row, action, task) {
  return runAction(null, task, {key: `apikey:${row.id}:${action}`,
    busy: {title: `${action} ${row.name}…`, detail: 'The credential is being changed.'}});
}

/** The three v1 controls for one key. `reload` repaints whatever listed it. */
export function apiKeyActions(row, reload) {
  return [
    h('button', {class: 'button compact', onclick: (event) => {
      event.stopPropagation();
      const trigger = event.currentTarget;
      return keyAction(row, 'Rotating', async () => {
        const result = await post(ACTION_URL, {api_key: row.id, action: 'rotate'});
        oneTimeSecret('Rotated API key', 'API key token', result.token, trigger);
        await reload();
      });
    }}, 'Rotate'),
    h('button', {class: 'button compact', onclick: (event) => {
      event.stopPropagation();
      return keyAction(row, row.is_active ? 'Deactivating' : 'Reactivating', async () => {
        await post(ACTION_URL,
          {api_key: row.id, action: row.is_active ? 'deactivate' : 'reactivate'});
        await reload();
      });
    }}, row.is_active ? 'Deactivate' : 'Reactivate'),
    // The confirm comes first and nothing paints while it is open.
    h('button', {class: 'button compact danger', onclick: (event) => {
      event.stopPropagation();
      return confirmAction({title: `Revoke ${row.name}?`,
        copy: 'The API key record and credential will be deleted.',
        confirmLabel: 'Revoke key', danger: true}).then((answer) => (answer.confirmed
        ? keyAction(row, 'Revoking', async () => {
          await post(ACTION_URL, {api_key: row.id, action: 'revoke'});
          await reload();
        })
        : undefined));
    }}, 'Revoke'),
  ];
}

function permissionSummary(row) {
  const names = Object.keys(row.permissions || {}).filter((key) => row.permissions[key]);
  return names.length ? names.join(', ') : 'No extra permissions — the group’s own access';
}

function keyRow(row, groupName, reload, manage) {
  return h('div', {class: 'key-row'},
    h('div', {class: 'key-identity'},
      h('strong', {text: row.name || `Key ${row.id}`}),
      h('small', {text: [
        `acts as ${groupName}`,
        permissionSummary(row),
        row.last_used ? `last used ${formatDate(row.last_used)}` : 'never used',
      ].join(' · ')})),
    badge(row.is_active ? 'Active' : 'Inactive', row.is_active ? 'success' : 'danger'),
    manage ? h('div', {class: 'inline-actions'}, ...apiKeyActions(row, reload)) : null);
}

// One line, not a list: the app that owns a deploy key is the only screen that
// may change it.
function deployKeysNote(ctx) {
  const copy = 'Deploy keys belong to an app — manage them on the app’s Deploy '
    + 'key tab.';
  return h('section', {class: 'panel'},
    h('div', {class: 'panel-head'},
      h('div', {}, h('h2', {text: 'App deploy keys'}), h('p', {text: copy})),
      appsEnabled(ctx)
        ? h('a', {class: 'button compact', href: routeHref('apps')}, 'Open Apps')
        : null));
}

export function keysTab(ctx) {
  const caps = capabilities(ctx);
  const manage = caps.manage_api_keys === true;
  const body = h('div', {});
  let generation = 0;

  const load = async () => {
    const mine = ++generation;
    await loadInto(body, async (current) => {
      // Two reads, both already made elsewhere in this destination: every key
      // this caller may see, and (when they can read groups) the groups
      // themselves — so a group with no keys is shown as having none rather
      // than being absent.
      const keys = (await apiEnvelope('/api/group/apikey?size=200&sort=-last_used')).items;
      const groups = caps.groups
        ? (await apiEnvelope('/api/group?size=100&sort=name')).items : [];
      if (!current() || mine !== generation) return;
      const byGroup = new Map();
      const remember = (id, name) => {
        const key = String(id ?? '');
        if (!byGroup.has(key)) byGroup.set(key, {id, name, keys: []});
        return byGroup.get(key);
      };
      groups.forEach((group) => remember(group.id, group.name));
      keys.forEach((row) => {
        const group = row.group || {};
        remember(group.id, group.name || `Group ${group.id ?? '—'}`).keys.push(row);
      });
      const sections = [...byGroup.values()]
        .sort((left, right) => String(left.name).localeCompare(String(right.name)))
        .map((group) => h('section', {class: 'panel'},
          h('div', {class: 'panel-head'},
            h('div', {}, h('h2', {text: `${group.name} — group API keys`}),
              h('p', {text: group.keys.length
                ? 'Each key acts with this group’s permissions.'
                : 'No API keys exist for this group.'})),
            manage && group.id != null
              ? h('button', {class: 'button compact',
                onclick: () => createApiKey(group, load)}, icon('plus'), 'New key')
              : null),
          group.keys.length
            ? h('div', {class: 'key-list'},
              ...group.keys.map((row) => keyRow(row, group.name, load, manage)))
            : null));
      body.replaceChildren(
        sections.length
          ? h('div', {class: 'access-tab'}, ...sections)
          : h('section', {class: 'panel'}, h('div', {class: 'panel-body'},
            h('p', {text: 'No groups or API keys are visible with your current access.'}))),
        deployKeysNote(ctx));
    }, {message: 'Loading API keys…', retry: load});
  };

  load();
  return body;
}

// v1's gate, unchanged: its group inspector offered the API Keys section only
// to `manage_api_keys`. Promoting the list to a tab does not widen who may see
// live credentials, so the tab appears under exactly that capability.
export function keysTabVisible(ctx) {
  return capabilities(ctx).manage_api_keys === true;
}
