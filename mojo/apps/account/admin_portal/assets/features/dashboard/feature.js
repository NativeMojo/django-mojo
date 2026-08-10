import {dashboardPage} from './page.js';

export default {
  id: 'dashboard', routes: ['system'], style: 'assets/features/dashboard/styles.css',
  enabled: (ctx) => ctx.features?.dashboard?.enabled === true,
  navigation: () => [{route: 'system', label: 'System', icon: 'home', section: 'Control plane'}],
  title: () => 'System overview',
  render: ({ctx}) => dashboardPage(ctx),
};
