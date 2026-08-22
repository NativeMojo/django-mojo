import {emailAvailable, settingsPage, smsAvailable} from './page.js';
import {smsPage} from './sms.js';
import {emailPage} from './email.js';
import {permissionDeniedState} from '../../components/views.js';

// Settings owns the integration setup and test pages, so it opens for anyone
// v1 lets into settings OR into either messaging integration. The two
// integrations are sub-pages rather than sidebar entries: v2 spends six nav
// slots and no more, and both pages are reached from the catalog row that
// describes them.
//
// A route whose block is missing is refused where it is asked for, rather than
// silently rendering the catalog — the address bar and the screen must not
// disagree about which page this is.
export default {
  id: 'settings',
  routes: ['settings', 'settings-sms', 'settings-email'],
  style: 'assets/features/settings/styles.css',
  enabled: (ctx) => ctx.features?.settings?.enabled === true
    || smsAvailable(ctx) || emailAvailable(ctx),
  // Text messages and Email are the two integration pages operators reach for
  // most — the test tools live there — so they get their own entries under
  // Messaging, each gated on its own bootstrap block, as in v1.
  navigation: (ctx) => [
    {route: 'settings', label: 'Settings', icon: 'settings', section: 'System', order: 70},
    ctx.features?.sms?.enabled === true
      ? {route: 'settings-sms', label: 'Text messages', icon: 'phone', section: 'Messaging', order: 60} : null,
    ctx.features?.email?.enabled === true
      ? {route: 'settings-email', label: 'Email', icon: 'mail', section: 'Messaging', order: 61} : null,
  ].filter(Boolean),
  title: (route) => (route === 'settings-sms' ? 'Text messages'
    : route === 'settings-email' ? 'Email' : 'Settings'),
  render: ({ctx, route, signal}) => {
    if (route === 'settings-sms') {
      return smsAvailable(ctx) ? smsPage(ctx) : permissionDeniedState(
        'Text messages needs the messaging_sms permission on this installation.');
    }
    if (route === 'settings-email') {
      return emailAvailable(ctx) ? emailPage(ctx) : permissionDeniedState(
        'Email needs the email permission on this installation.');
    }
    return settingsPage(ctx, route, signal);
  },
};
