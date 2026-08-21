import {placeholderPage} from '../../components/placeholder.js';

export function infrastructurePage(ctx) {
  return placeholderPage(ctx, {
    eyebrow: 'Infrastructure',
    title: 'Infrastructure',
    copy: 'Fleet capacity, metrics, and maintenance for this installation.',
  });
}
