import {dashboardPage} from './page.js';

export default {
  id: 'dashboard', routes: ['dashboard'], style: 'assets/features/dashboard/styles.css',
  enabled: (ctx) => ctx.features?.dashboard?.enabled === true,
  navigation: () => [{route: 'dashboard', label: 'Dashboard', icon: 'home', section: 'Control plane'}],
  title: () => 'Dashboard',
  render: ({ctx}) => dashboardPage(ctx),
};
