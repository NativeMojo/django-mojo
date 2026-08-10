import {setupPage} from './page.js';

export default {
  id: 'platform', routes: ['setup'], style: 'assets/features/platform/styles.css',
  enabled: (ctx) => ctx.features?.platform?.enabled === true,
  navigation: () => [{route: 'setup', label: 'System Setup', icon: 'settings', section: 'Control plane'}],
  title: () => 'System Setup',
  render: ({ctx}) => setupPage(ctx),
};
