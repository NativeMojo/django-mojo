import {infrastructurePage} from './page.js';

// Infrastructure will carry v1's Fleet Scaling, Metrics and Maintenance as
// tabs. All three live behind the platform block today, so that is the gate.
export default {
  id: 'infrastructure',
  routes: ['infrastructure'],
  style: 'assets/features/infrastructure/styles.css',
  enabled: (ctx) => ctx.features?.platform?.enabled === true,
  navigation: () => [{route: 'infrastructure', label: 'Infrastructure', icon: 'server'}],
  title: () => 'Infrastructure',
  render: ({ctx}) => infrastructurePage(ctx),
};
