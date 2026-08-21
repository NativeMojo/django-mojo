import {placeholderPage} from '../../components/placeholder.js';

export function accessPage(ctx) {
  return placeholderPage(ctx, {
    eyebrow: 'Access',
    title: 'Access',
    copy: 'People, groups, API keys, and who has been signing in.',
  });
}
