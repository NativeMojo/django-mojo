import home from './home/feature.js';
import apps from './apps/feature.js';
import infrastructure from './infrastructure/feature.js';
import domains from './domains/feature.js';
import access from './access/feature.js';
import settings from './settings/feature.js';

// Six destinations, in sidebar order. Nothing is added here without removing
// something: a new capability becomes a sub-page or a tab of an existing
// destination, never a seventh entry.
const DESCRIPTORS = Object.freeze([home, apps, infrastructure, domains, access, settings]);
const ROUTES = new Map();

for (const feature of DESCRIPTORS) {
  if (!feature || typeof feature.id !== 'string' || !Array.isArray(feature.routes)
      || typeof feature.style !== 'string' || typeof feature.enabled !== 'function'
      || typeof feature.navigation !== 'function' || typeof feature.title !== 'function'
      || typeof feature.render !== 'function') throw new Error('Invalid Admin feature descriptor');
  for (const route of feature.routes) {
    if (typeof route !== 'string' || !route || ROUTES.has(route)) throw new Error(`Duplicate Admin route: ${route}`);
    ROUTES.set(route, feature);
  }
}

export function featureForRoute(route, ctx) {
  const candidate = ROUTES.get(route);
  return candidate?.enabled(ctx) ? candidate : home;
}

// v2's sidebar is one flat list with no section labels, so entries keep
// DESCRIPTORS order exactly. A feature whose block is missing from the
// bootstrap payload contributes no entry — the destination is hidden, not
// shown-and-refused.
export function navigationFor(ctx) {
  return DESCRIPTORS.filter((feature) => feature.enabled(ctx))
    .flatMap((feature) => feature.navigation(ctx));
}

export function installFeatureStyles(ctx) {
  for (const feature of DESCRIPTORS.filter((item) => item.enabled(ctx))) {
    if (document.head.querySelector(`link[data-admin-feature="${feature.id}"]`)) continue;
    const link = document.createElement('link'); link.rel = 'stylesheet'; link.href = feature.style;
    link.dataset.adminFeature = feature.id; document.head.append(link);
  }
}

export const featureDescriptors = DESCRIPTORS;
