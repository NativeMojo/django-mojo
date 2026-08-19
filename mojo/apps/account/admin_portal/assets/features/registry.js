import advanced from './advanced/feature.js';
import activity from './activity/feature.js';
import dashboard from './dashboard/feature.js';
import people from './people/feature.js';
import platform from './platform/feature.js';
import webapps from './webapps/feature.js';
import settings from './settings/feature.js';
import sms from './sms/feature.js';

const DESCRIPTORS = Object.freeze([dashboard, webapps, advanced, people, activity, platform, settings, sms]);
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
  return candidate?.enabled(ctx) ? candidate : dashboard;
}

// Entries keep DESCRIPTORS order by default; an entry may declare a numeric
// `order` to sit later in the sidebar than the feature it belongs to. Array
// sort is stable, so everything unordered stays exactly where it was.
export function navigationFor(ctx) {
  return DESCRIPTORS.filter((feature) => feature.enabled(ctx))
    .flatMap((feature) => feature.navigation(ctx))
    .sort((left, right) => (left.order || 0) - (right.order || 0));
}

export function installFeatureStyles(ctx) {
  for (const feature of DESCRIPTORS.filter((item) => item.enabled(ctx))) {
    if (document.head.querySelector(`link[data-admin-feature="${feature.id}"]`)) continue;
    const link = document.createElement('link'); link.rel = 'stylesheet'; link.href = feature.style;
    link.dataset.adminFeature = feature.id; document.head.append(link);
  }
}

export const featureDescriptors = DESCRIPTORS;
