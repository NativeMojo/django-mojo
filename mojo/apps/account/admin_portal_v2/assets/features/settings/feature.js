import {settingsPage} from './page.js';

// Settings owns the integration setup and test pages, so it opens for anyone
// v1 lets into settings OR into either messaging integration.
export default {
  id: 'settings',
  routes: ['settings'],
  style: 'assets/features/settings/styles.css',
  enabled: (ctx) => ctx.features?.settings?.enabled === true
    || ctx.features?.sms?.enabled === true
    || ctx.features?.email?.enabled === true,
  navigation: () => [{route: 'settings', label: 'Settings', icon: 'settings'}],
  title: () => 'Settings',
  render: ({ctx}) => settingsPage(ctx),
};
