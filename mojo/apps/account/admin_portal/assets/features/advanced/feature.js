import {setupPage} from './page.js';

export default {
  id: 'advanced', routes: ['setup'], style: 'assets/features/advanced/styles.css',
  enabled: (ctx) => ctx.features?.advanced?.enabled === true,
  navigation: () => [{route: 'setup', label: 'System Setup', icon: 'settings', section: 'Control plane'}],
  title: () => 'System Setup',
  render: ({ctx}) => setupPage(ctx),
};
