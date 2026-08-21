import {placeholderPage} from '../../components/placeholder.js';

export function settingsPage(ctx) {
  return placeholderPage(ctx, {
    eyebrow: 'Settings',
    title: 'Settings',
    copy: 'Installation settings, plus the integrations that send email and text messages.',
  });
}
