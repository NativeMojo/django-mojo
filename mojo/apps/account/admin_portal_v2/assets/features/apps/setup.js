// "Set up deploys": the ways a first (or next) build actually reaches this app.
// GitHub Actions is the default because it is the one we can generate outright;
// the upload panel deploys a build straight from the browser, and the API panel
// names the three calls any CI can make.
//
// Ported from v1's webapps/page.js. Two things changed: the deep links land on
// v2's app route, and the storage-blocked message opens System Setup in the
// current Admin (v2 has no setup screen) instead of pointing at a route that is
// not there. The upload machine — hashing, signed PUTs, the completion poll —
// is v1's, unchanged.
import {api, h, icon} from '../../core.js';
import {routeHref} from '../../components/routes.js';
import {copyButton, loadInto, runAction} from '../../components/actions.js';
import {sectionTabs} from '../../components/views.js';
import {certState, httpsLink, keyDialog, v1Href} from './shared.js';
import {syncAppDeployment} from './operations.js';

// The server refuses more than this per release (its EDGE_RELEASE_MAX_FILES /
// EDGE_RELEASE_MAX_BYTES defaults); refusing here first saves the hashing.
const UPLOAD_LIMITS = {files: 5000, bytes: 1073741824};
const STORAGE_BLOCKED_COPY = 'The browser was blocked from uploading directly to storage — run the storage checkup in System Setup (bucket sharing rules), then try again.';
const DEPLOY_POLL_MS = 1800;

const SETUP_WAYS = [
  ['github', 'GitHub Actions'], ['upload', 'Upload a build'],
  ['api', 'Any other CI / API'],
];

function workflowPanel(webapp, keyAction = null) {
  const panel = h('div', {class: 'setup-block'});
  // The workflow file is generated server-side, so this tab always awaits
  // before it can paint: a loading state before, and an in-panel failure with
  // a retry after — never a blank block that might or might not be finished.
  loadInto(panel, async (current) => {
    const result = await api('/api/edge/webapp/onboarding/workflow', {method: 'POST', body: JSON.stringify({webapp: webapp.id})});
    if (!current()) return;
    // A <pre>, not a <textarea>: this is text to read and copy, never to edit.
    const text = h('pre', {class: 'code-block', tabindex: '0', text: result.yaml});
    panel.replaceChildren(
      h('p', {text: 'Two things set up deploys from GitHub: one secret, and one file.'}),
      h('ol', {class: 'setup-list'},
        h('li', {}, h('strong', {text: 'Add the secret. '}), 'In your repo’s Settings → Secrets, add ', h('code', {text: 'MOJO_DEPLOY_KEY'}), '. ', keyAction),
        h('li', {}, h('strong', {text: 'Add the file. '}), 'Save this as ', h('code', {text: result.filename}), ' and push:')),
      text,
      copyButton(() => text.textContent, {label: 'Copy file', className: 'button primary'}));
  }, {message: 'Building your workflow file…'});
  return panel;
}

// ---------------------------------------------------------------------------
// Upload a build: pick or drop the built folder, hash every file right here
// (crypto.subtle sha256), PUT each one straight to storage with the exact
// x-amz-checksum-sha256 header its signed URL binds, mark the release
// complete, and watch the deploy land — the same three calls CI makes.
// ---------------------------------------------------------------------------

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return '0 B';
  if (bytes >= 1073741824) return `${(bytes / 1073741824).toFixed(1)} GB`;
  if (bytes >= 1048576) return `${(bytes / 1048576).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

// upload-YYYYMMDD-HHMMSS: unique enough per hand deploy, and within the
// server's version charset (letters, digits, ., _, -).
function defaultUploadVersion() {
  return `upload-${new Date().toISOString().slice(0, 19).replace(/[-:]/g, '').replace('T', '-')}`;
}

async function sha256Hex(file) {
  const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer());
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

// Manifest paths are relative to the picked root: backslashes normalized to
// forward slashes, leading ./ and / stripped.
function normalizeRelPath(raw) {
  let path = String(raw || '').replace(/\\/g, '/');
  while (path.startsWith('./')) path = path.slice(2);
  return path.replace(/^\/+/, '');
}

// A folder pick carries webkitRelativePath ("dist/assets/app.js"); the picked
// folder itself is the root, so its own name is not part of any path.
function pickedFiles(input) {
  return [...(input.files || [])].map((file) => {
    let path = normalizeRelPath(file.webkitRelativePath || file.name);
    if (file.webkitRelativePath && path.includes('/')) path = path.slice(path.indexOf('/') + 1);
    return {file, path};
  });
}

async function droppedFiles(dataTransfer) {
  const entries = [...(dataTransfer.items || [])]
    .map((item) => (item.webkitGetAsEntry ? item.webkitGetAsEntry() : null))
    .filter(Boolean);
  if (!entries.length) {
    return [...(dataTransfer.files || [])].map((file) => ({file, path: normalizeRelPath(file.name)}));
  }
  const out = [];
  async function walk(entry, base) {
    if (entry.isFile) {
      const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
      out.push({file, path: normalizeRelPath(base ? `${base}/${entry.name}` : entry.name)});
      return;
    }
    if (!entry.isDirectory) return;
    const reader = entry.createReader();
    let batch;
    do {
      batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
      for (const child of batch) await walk(child, base ? `${base}/${entry.name}` : entry.name);
    } while (batch.length);
  }
  for (const entry of entries) await walk(entry, '');
  // One dropped folder is the picked root — same rule as the folder picker.
  if (entries.length === 1 && entries[0].isDirectory) {
    const prefix = `${normalizeRelPath(entries[0].name)}/`;
    return out.map((row) => ({
      file: row.file,
      path: row.path.startsWith(prefix) ? row.path.slice(prefix.length) : row.path,
    }));
  }
  return out;
}

function uploadPanel(ctx, app, summary, reload) {
  const manage = ctx.capabilities.manage_webapps;
  const panel = h('div', {class: 'setup-block upload-build'});
  if (!manage) {
    panel.append(h('p', {class: 'muted', text: 'You don’t have permission to deploy this app.'}));
    return panel;
  }
  const state = {files: [], busy: false};
  const message = h('div', {class: 'form-message', role: 'alert'});
  const progress = h('p', {class: 'muted small', role: 'status'});
  const chosen = h('p', {class: 'muted small', text: 'Nothing picked yet.'});
  const version = h('input', {value: defaultUploadVersion(), autocomplete: 'off', spellcheck: 'false'});
  const deploy = h('button', {class: 'button primary', disabled: true}, icon('deploy'), 'Upload and deploy');

  const totalBytes = () => state.files.reduce((sum, row) => sum + row.file.size, 0);
  const overCaps = () => state.files.length > UPLOAD_LIMITS.files || totalBytes() > UPLOAD_LIMITS.bytes;
  const capsMessage = () =>
    `Too big to deploy: the server accepts at most ${UPLOAD_LIMITS.files.toLocaleString()} files and `
    + `${formatBytes(UPLOAD_LIMITS.bytes)} per release. This pick is `
    + `${state.files.length.toLocaleString()} files, ${formatBytes(totalBytes())}.`;
  const setProgress = (text) => { progress.textContent = text; };

  function fail(error) {
    state.busy = false;
    deploy.disabled = !state.files.length || overCaps();
    setProgress('');
    if (error?.storageBlocked || error?.name === 'TypeError') {
      message.replaceChildren(
        document.createTextNode(`${STORAGE_BLOCKED_COPY} `),
        h('a', {href: v1Href(ctx, 'setup'), text: 'Open System Setup'}),
        h('span', {class: 'apps-note', text: ' · opens the current Admin'}));
      return;
    }
    message.textContent = error?.message || 'Something went wrong. Try again.';
  }

  function takeFiles(rows) {
    if (state.busy) return;
    const seen = new Map();
    (rows || []).forEach((row) => { if (row.file && row.path) seen.set(row.path, row); });
    if (!seen.size) {
      message.textContent = 'Nothing usable was picked. Choose the folder your build produced.';
      return;
    }
    state.files = [...seen.values()];
    chosen.textContent = `${state.files.length.toLocaleString()} files, ${formatBytes(totalBytes())}.`;
    message.textContent = '';
    setProgress('');
    if (overCaps()) { message.textContent = capsMessage(); deploy.disabled = true; return; }
    deploy.disabled = false;
  }

  // Polls /api/edge/release/deployment/<id> the way the app pages poll a run:
  // a setTimeout loop that stops when the panel leaves the document.
  function watchDeployment(deploymentId, versionName) {
    return new Promise((resolve) => {
      const step = async () => {
        if (!panel.isConnected) { resolve(); return; }
        let status = null;
        try {
          status = await api(`/api/edge/release/deployment/${encodeURIComponent(deploymentId)}`);
        } catch (_) { /* keep the last progress line; the next tick retries */ }
        // Every tick is the endpoint reporting this deploy, so the global
        // banner is fed from the same read that drives the panel — and cleared
        // by it the moment the deploy is terminal.
        if (status) syncAppDeployment(app, status);
        if (status?.terminal) {
          state.busy = false;
          deploy.disabled = false;
          if (status.success) {
            setProgress('');
            progress.replaceChildren(
              document.createTextNode(`Deployed — ${status.version || versionName} is live. `),
              h('a', {href: routeHref('apps', {webapp: app.id, tab: 'deploys'}), text: 'See your deploys'}));
          } else {
            fail(new Error(status.detail || `The deploy finished as ${String(status.status).replace(/_/g, ' ')}. Try again, or roll back from the Deploys tab.`));
          }
          resolve();
          return;
        }
        if (status) setProgress(`Deploying to your fleet — ${String(status.status).replace(/_/g, ' ')}…`);
        setTimeout(step, DEPLOY_POLL_MS);
      };
      step();
    });
  }

  async function run() {
    if (state.busy || !state.files.length) return;
    message.textContent = '';
    const name = version.value.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(name)) {
      message.textContent = 'Version names use letters, digits, dots, dashes and underscores.';
      return;
    }
    if (overCaps()) { message.textContent = capsMessage(); return; }
    state.busy = true;
    deploy.disabled = true;
    try {
      const manifest = [];
      for (let i = 0; i < state.files.length; i += 1) {
        setProgress(`Reading file ${i + 1} of ${state.files.length}…`);
        const row = state.files[i];
        manifest.push({path: row.path, sha256: await sha256Hex(row.file), size: row.file.size});
      }
      setProgress('Registering the release…');
      const registered = await api('/api/edge/release', {
        method: 'POST', body: JSON.stringify({webapp: app.id, version: name, manifest})});
      // The register response carries one signed URL per file plus the exact
      // headers that URL was bound to (x-amz-checksum-sha256). An already
      // verified version returns no uploads and goes straight to complete.
      const uploads = registered.uploads || [];
      const byPath = new Map(state.files.map((row) => [row.path, row.file]));
      for (let i = 0; i < uploads.length; i += 1) {
        const upload = uploads[i];
        setProgress(`Uploading file ${i + 1} of ${uploads.length}…`);
        let response;
        try {
          response = await fetch(upload.url, {
            method: 'PUT', headers: upload.headers || {}, body: byPath.get(upload.path)});
        } catch (error) {
          const blocked = new Error(STORAGE_BLOCKED_COPY);
          blocked.storageBlocked = true;
          throw blocked;
        }
        if (!response.ok) {
          throw new Error(`Storage refused ${upload.path} (${response.status}). Try again.`);
        }
      }
      setProgress('Checking every file landed…');
      const completed = await api('/api/edge/release/complete', {
        method: 'POST', body: JSON.stringify({release: registered.release})});
      setProgress('Deploying to your fleet…');
      await watchDeployment(completed.deployment, name);
      await reload();
    } catch (error) {
      fail(error);
    }
  }

  const folderInput = h('input', {type: 'file', webkitdirectory: true, multiple: true, hidden: true,
    onchange: () => takeFiles(pickedFiles(folderInput))});
  const filesInput = h('input', {type: 'file', multiple: true, hidden: true,
    onchange: () => takeFiles(pickedFiles(filesInput))});
  const drop = h('div', {class: 'upload-drop', role: 'button', tabindex: '0',
    'aria-label': 'Drop your built site folder here, or press Enter to pick it',
    ondragover: (event) => { event.preventDefault(); drop.classList.add('active'); },
    ondragleave: () => drop.classList.remove('active'),
    ondrop: async (event) => {
      event.preventDefault();
      drop.classList.remove('active');
      if (!state.busy) takeFiles(await droppedFiles(event.dataTransfer));
    },
    onclick: () => { if (!state.busy) folderInput.click(); },
    onkeydown: (event) => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); if (!state.busy) folderInput.click(); }
    }},
  icon('deploy'),
  h('strong', {text: 'Drop your built site folder here'}),
  h('p', {class: 'muted small', text: 'or click to pick the folder'}));
  // The upload flow keeps its own progress machine — it has real per-file
  // progress to report, which a generic pending state would only hide. What it
  // gains here is the shared re-entry guard: `state.busy` is set after two
  // early returns, and a double click before then registered two deploys.
  deploy.addEventListener('click', () => runAction(deploy, run, {announceLabel: 'Uploading and deploying…'}));

  panel.append(
    h('p', {text: 'Deploy by hand: pick the folder your build produced, and it goes live the same way a CI deploy does.'}),
    drop, folderInput, filesInput,
    h('p', {class: 'muted small'}, 'Prefer files over a folder? ',
      h('a', {href: '#', onclick: (event) => { event.preventDefault(); if (!state.busy) filesInput.click(); }, text: 'Pick individual files'}), '.'),
    chosen,
    h('label', {class: 'field'}, h('span', {text: 'Version name'}), version),
    h('p', {class: 'muted small', text:
      `The server accepts at most ${UPLOAD_LIMITS.files.toLocaleString()} files and ${formatBytes(UPLOAD_LIMITS.bytes)} per release.`}),
    h('div', {class: 'row-actions'}, deploy),
    progress, message);
  return panel;
}

export function setupPanel(ctx, app, summary, reload) {
  const manage = ctx.capabilities.manage_webapps;
  const body = h('div', {class: 'setup-way'});
  let active = 'github';
  const keyButton = () => (manage
    ? h('button', {class: 'button compact', type: 'button', onclick: (event) => runAction(event.currentTarget,
      () => keyDialog(app, reload), {pendingLabel: 'Opening…'})}, icon('key'),
    summary.deployment_key?.linked ? 'Rotate key — shown once' : 'Create key — shown once')
    : null);
  function paint() {
    if (active === 'github') { body.replaceChildren(workflowPanel(app, keyButton())); return; }
    if (active === 'upload') {
      body.replaceChildren(uploadPanel(ctx, app, summary, reload));
      return;
    }
    body.replaceChildren(h('div', {class: 'setup-block'},
      h('p', {text: 'Deploy from any CI — or by hand — with one credential and three calls.'}),
      h('ol', {class: 'setup-list'},
        h('li', {}, h('strong', {text: 'Create a deploy key. '}), 'It’s shown once; keep it wherever your CI keeps secrets. ', keyButton()),
        h('li', {}, h('strong', {text: 'Register a release. '}), 'Send the manifest of files your build produced.'),
        h('li', {}, h('strong', {text: 'Upload and complete. '}), 'PUT each file, mark the release complete, and it deploys on its own.')),
      h('p', {class: 'muted small'}, 'Request-by-request details are in ',
        h('a', {href: '#', onclick: (e) => e.preventDefault(), title: 'docs/web_developer/edge/releases.md'}, 'the release guide'),
        ' — releases register against this app’s id (', h('code', {text: `#${app.id}`}), ').')));
  }
  const tabs = sectionTabs({items: SETUP_WAYS.map(([id, label]) => ({id, label})), active, label: 'Ways to deploy', onChange: (id) => {
    active = id;
    [...tabs.querySelectorAll('button')].forEach((button, index) => button.classList.toggle('active', SETUP_WAYS[index][0] === id));
    paint();
  }});
  paint();
  const ways = h('div', {class: 'setup-ways'}, tabs, body);
  if (summary.address?.hostname && !summary.current_release) {
    // Landing here from onboarding, the three ways to deploy read as three
    // ways to fix something — because nothing on the tab said the app was
    // already up. It is: the address, its certificate and a welcome page all
    // went live during setup, and deploying only replaces the page.
    const secure = certState(summary.address.certificate).tone === 'ok';
    const origin = httpsLink(summary.address.https_origin);
    ways.prepend(h('div', {class: 'result-state success'}, icon('check'),
      h('div', {},
        h('strong', {text: 'Your app is already live'}),
        h('p', {text: `${summary.address.hostname} is serving the welcome page it came with${secure ? ', over HTTPS' : ''}. Deploying replaces that page with your build — there is nothing to fix first.`}),
        secure ? null : h('p', {class: 'muted small', text: 'Its HTTPS certificate is still being issued — that finishes on its own.'}),
        origin ? h('a', {class: 'button ghost compact', href: origin, target: '_blank', rel: 'noopener'}, 'Open') : null)));
  }
  return ways;
}
