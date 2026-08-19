import {settingsPage} from './page.js';

export default {
  id: 'settings', routes: ['settings'], style: 'assets/features/settings/styles.css',
  enabled: (ctx) => ctx.features?.settings?.enabled === true,
  navigation: () => [{route: 'settings', label: 'Settings', icon: 'settings', section: 'Control plane'}],
  title: () => 'Settings',
  render: ({ctx, route, signal}) => settingsPage(ctx, route, signal),
};
