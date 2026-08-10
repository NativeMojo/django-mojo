import {activityPage} from './page.js';

export default {
  id: 'activity', routes: ['activity'], style: 'assets/features/activity/styles.css',
  enabled: (ctx) => ctx.features?.activity?.enabled === true,
  navigation: () => [{route: 'activity', label: 'Activity', icon: 'activity', section: 'Control plane'}],
  title: () => 'Activity',
  render: ({ctx, signal}) => activityPage(ctx, signal),
};
